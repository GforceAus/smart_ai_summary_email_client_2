-- ============================================================
-- field_ops.v_supplier_email_summary
--
-- Parameterised via PostgreSQL session variables.
-- Call pattern from Python:
--
--   conn.execute("SET app.supplier_name = %s", [supplier_name])
--   conn.execute("SET app.date_from = %s", [date_from])
--   conn.execute("SET app.date_to = %s", [date_to])
--   row = conn.execute("SELECT summary, tasks FROM field_ops.v_supplier_email_summary").fetchone()
--
-- Returns:
--   summary  JSONB  -- aggregate counts + metadata (~300 tokens)
--   tasks    JSONB  -- exception rows sorted worst-first, capped at 60 (~800-1200 tokens)
--
-- Total LLM input budget: ~1,500 tokens (well under 4096 ctx)
-- ============================================================

CREATE OR REPLACE VIEW field_ops.v_supplier_email_summary AS
WITH
-- ── 1. BASE FILTER ────────────────────────────────────────────────────────
base AS (
    SELECT
        t.id                        AS task_uuid,   -- UUID, join key for task_questions
        t.task_id,                                  -- string task identifier for output
        t.task_date,
        t.task_name,
        t.task_type,
        t.task_status,
        t.store_id,
        t.store_name,
        t.retailer_name,
        t.supplier_name,
        t.supplier_id,
        t.full_company_name,
        COALESCE(
            NULLIF(TRIM(t.cover_rep_first_name || ' ' || t.cover_rep_last_name), ''),
            NULLIF(TRIM(t.senior_rep_first_name || ' ' || t.senior_rep_last_name), '')
        )                           AS rep_name,
        t.comments_from_rep,
        t.cannot_complete_reason,
        t.cannot_complete_comments,
        t.week_start_date,
        t.recurring,
        t.one_off
    FROM field_ops.tasks t
    WHERE t.supplier_name  = current_setting('app.supplier_name')
      AND t.task_date     >= current_setting('app.date_from')::date
      AND t.task_date     <= current_setting('app.date_to')::date
      AND t.task_status   IN ('done', 'in_progress')
),
-- ── 2. QUESTIONS JOIN ─────────────────────────────────────────────────────
-- CRITICAL: partition join must include task_date on both sides.
questions AS (
    SELECT
        tq.task_uuid,
        tq.task_date,
        tq.question,
        tq.answer_from_rep,
        CASE
            WHEN tq.answer_from_rep ILIKE '%NO %'
              OR tq.answer_from_rep ILIKE '%NOT %'
              OR tq.answer_from_rep ILIKE '%MISSING%'
              OR tq.answer_from_rep ILIKE '%NONE%'
              OR tq.answer_from_rep ILIKE 'NO'
            THEN TRUE
            ELSE FALSE
        END AS is_negative_answer
    FROM field_ops.task_questions tq
    WHERE tq.task_date >= current_setting('app.date_from')::date
      AND tq.task_date <= current_setting('app.date_to')::date
      AND NOT (
          (tq.question IS NULL OR tq.question = '')
          AND tq.answer_from_rep IS NULL
      )
      AND tq.answers != ARRAY['']::character varying[]
),
-- ── 3. PER-TASK QA COLLAPSE ───────────────────────────────────────────────
task_qa AS (
    SELECT
        b.task_uuid,
        b.task_id,
        b.task_date,
        b.task_name,
        b.task_type,
        b.task_status,
        b.store_id,
        b.store_name,
        b.retailer_name,
        b.supplier_name,
        b.rep_name,
        b.comments_from_rep,
        b.cannot_complete_reason,
        b.cannot_complete_comments,
        b.week_start_date,
        b.recurring,
        b.one_off,
        COUNT(q.task_uuid)
            FILTER (WHERE q.is_negative_answer)             AS negative_answer_count,
        COUNT(q.task_uuid)
            FILTER (WHERE q.answer_from_rep IS NOT NULL)    AS answered_count,
        JSON_AGG(
            JSON_BUILD_OBJECT('q', q.question, 'a', q.answer_from_rep)
            ORDER BY q.is_negative_answer DESC
        ) FILTER (WHERE q.answer_from_rep IS NOT NULL)      AS qa_pairs,
        BOOL_OR(q.is_negative_answer)                       AS has_issue
    FROM base b
    LEFT JOIN questions q
        ON  q.task_uuid = b.task_uuid
        AND q.task_date = b.task_date
    GROUP BY
        b.task_uuid, b.task_id, b.task_date, b.task_name, b.task_type, b.task_status,
        b.store_id, b.store_name, b.retailer_name, b.supplier_name,
        b.rep_name, b.comments_from_rep, b.cannot_complete_reason,
        b.cannot_complete_comments, b.week_start_date, b.recurring, b.one_off
),
-- ── 4. TRIAGE FILTER ──────────────────────────────────────────────────────
triaged AS (
    SELECT *
    FROM task_qa
    WHERE answered_count           > 0
       OR comments_from_rep        IS NOT NULL
       OR cannot_complete_comments IS NOT NULL
),
-- ── 5. ISSUE SCORE ────────────────────────────────────────────────────────
-- Scoring weights:
--   cannot_complete note  = +2  (rep hit a blocker)
--   each negative answer  = +2  (explicit NO/MISSING response)
--   rep comment present   = +1  (general observation)
--   task in_progress      = +1  (incomplete)
scored AS (
    SELECT
        *,
        (
            negative_answer_count * 2
            + CASE WHEN cannot_complete_comments IS NOT NULL THEN 2 ELSE 0 END
            + CASE WHEN comments_from_rep        IS NOT NULL THEN 1 ELSE 0 END
            + CASE WHEN task_status = 'in_progress'          THEN 1 ELSE 0 END
        ) AS issue_score
    FROM triaged
),
-- ── 6. SUMMARY COUNTS ─────────────────────────────────────────────────────
summary_agg AS (
    SELECT
        supplier_name,
        current_setting('app.date_from')::date              AS date_from,
        current_setting('app.date_to')::date                AS date_to,
        COUNT(*)                                             AS total_tasks,
        COUNT(*) FILTER (WHERE task_status = 'done')        AS done_tasks,
        COUNT(*) FILTER (WHERE task_status = 'in_progress') AS in_progress_tasks,
        ROUND(
            COUNT(*) FILTER (WHERE task_status = 'done')::numeric
            / NULLIF(COUNT(*), 0) * 100, 1
        )                                                    AS completion_pct,
        COUNT(DISTINCT store_id)                             AS stores_visited,
        COUNT(DISTINCT store_id) FILTER (WHERE has_issue)   AS stores_with_issues,
        COUNT(DISTINCT rep_name)                             AS reps_active,
        COUNT(*) FILTER (WHERE has_issue)                   AS tasks_with_issues,
        COUNT(*) FILTER (WHERE recurring)                   AS recurring_tasks,
        COUNT(*) FILTER (WHERE one_off)                     AS one_off_tasks
    FROM scored
    GROUP BY supplier_name
),
-- ── 7. EXCEPTION ROWS (capped at 60, worst-first) ─────────────────────────
exception_rows AS (
    SELECT
        JSON_AGG(
            JSON_BUILD_OBJECT(
                'task_uuid',        task_uuid,
                'task_id',          task_id,
                'store_id',         store_id,
                'store',            store_name,
                'state',            retailer_name,
                'task',             task_name,
                'status',           task_status,
                'rep',              rep_name,
                'date',             task_date,
                'qa',               qa_pairs,
                'comment',          comments_from_rep,
                'cannot_complete',  cannot_complete_comments,
                'score',            issue_score
            )
            ORDER BY issue_score DESC, task_date DESC
        ) AS tasks_json
    FROM (
        SELECT * FROM scored
        ORDER BY issue_score DESC, task_date DESC
        LIMIT 60
    ) capped
)
-- ── 8. FINAL OUTPUT ───────────────────────────────────────────────────────
SELECT
    JSON_BUILD_OBJECT(
        'supplier',           s.supplier_name,
        'date_from',          s.date_from,
        'date_to',            s.date_to,
        'total_tasks',        s.total_tasks,
        'done_tasks',         s.done_tasks,
        'in_progress_tasks',  s.in_progress_tasks,
        'completion_pct',     s.completion_pct,
        'stores_visited',     s.stores_visited,
        'stores_with_issues', s.stores_with_issues,
        'reps_active',        s.reps_active,
        'tasks_with_issues',  s.tasks_with_issues,
        'recurring_tasks',    s.recurring_tasks,
        'one_off_tasks',      s.one_off_tasks
    )::jsonb                                        AS summary,
    COALESCE(e.tasks_json::jsonb, '[]'::jsonb)      AS tasks
FROM summary_agg s
CROSS JOIN exception_rows e;


-- ============================================================
-- VALIDATION QUERY (run immediately after CREATE)
-- ============================================================
--
-- SET app.supplier_name = 'OSRAM';
-- SET app.date_from     = '2023-07-01';
-- SET app.date_to       = '2023-09-30';
-- SELECT
--     summary->>'total_tasks'               AS total_tasks,
--     summary->>'completion_pct'            AS completion_pct,
--     summary->>'stores_with_issues'        AS stores_with_issues,
--     jsonb_array_length(tasks)             AS exception_row_count,
--     tasks->0->>'task_uuid'                AS first_task_uuid,
--     tasks->0->>'task_id'                  AS first_task_id,
--     tasks->0->>'store_id'                 AS first_store_id,
--     (length(summary::text)
--      + length(tasks::text)) / 4           AS approx_tokens
-- FROM field_ops.v_supplier_email_summary;

-- field_ops.v_supplier_email_summary
-- Regenerated 2026-09-23.
--
-- Change: the tasks the summary is built from move from ('done','in_progress')
-- to ('done','approved').
--   * 'approved' is done-and-signed-off and carries answered questions at the
--     same density as 'done'; excluding it dropped completed findings.
--   * 'in_progress' is unfinished work, which these emails no longer report.
--
-- Knock-on edits, both required by that change:
--   * completion_pct and in_progress_tasks are removed. With only completed
--     statuses in scope, completion_pct was always 100 and in_progress_tasks
--     always 0. approved_tasks replaces them so the split stays visible.
--   * the issue_score term that added 1 for an in_progress task is dropped;
--     it can no longer fire.
--
-- NOTE: the LIMIT 60 on the tasks payload is left as-is. The generator now
-- bypasses it by reading field_ops directly, so raising it here is optional.

CREATE OR REPLACE VIEW field_ops.v_supplier_email_summary AS
 WITH base AS (
         SELECT t.id AS task_uuid,
            t.task_id,
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
            COALESCE(NULLIF(TRIM(BOTH FROM (t.cover_rep_first_name::text || ' '::text) || t.cover_rep_last_name::text), ''::text), NULLIF(TRIM(BOTH FROM (t.senior_rep_first_name::text || ' '::text) || t.senior_rep_last_name::text), ''::text)) AS rep_name,
            t.comments_from_rep,
            t.cannot_complete_reason,
            t.cannot_complete_comments,
            t.week_start_date,
            t.recurring,
            t.one_off
           FROM field_ops.tasks t
          WHERE t.supplier_name::text = current_setting('app.supplier_name'::text) AND t.task_date >= current_setting('app.date_from'::text)::date AND t.task_date <= current_setting('app.date_to'::text)::date AND (t.task_status::text = ANY (ARRAY['done'::character varying, 'approved'::character varying]::text[]))
        ), questions AS (
         SELECT tq.task_uuid,
            tq.task_date,
            tq.question,
            tq.answer_from_rep,
                CASE
                    WHEN tq.answer_from_rep::text ~~* '%NO %'::text OR tq.answer_from_rep::text ~~* '%NOT %'::text OR tq.answer_from_rep::text ~~* '%MISSING%'::text OR tq.answer_from_rep::text ~~* '%NONE%'::text OR tq.answer_from_rep::text ~~* 'NO'::text THEN true
                    ELSE false
                END AS is_negative_answer
           FROM field_ops.task_questions tq
          WHERE tq.task_date >= current_setting('app.date_from'::text)::date AND tq.task_date <= current_setting('app.date_to'::text)::date AND NOT ((tq.question IS NULL OR tq.question::text = ''::text) AND tq.answer_from_rep IS NULL) AND tq.answers <> ARRAY[''::character varying]
        ), task_qa AS (
         SELECT b.task_uuid,
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
            count(q.task_uuid) FILTER (WHERE q.is_negative_answer) AS negative_answer_count,
            count(q.task_uuid) FILTER (WHERE q.answer_from_rep IS NOT NULL) AS answered_count,
            json_agg(json_build_object('q', q.question, 'a', q.answer_from_rep) ORDER BY q.is_negative_answer DESC) FILTER (WHERE q.answer_from_rep IS NOT NULL) AS qa_pairs,
            bool_or(q.is_negative_answer) AS has_issue
           FROM base b
             LEFT JOIN questions q ON q.task_uuid::text = b.task_uuid::text AND q.task_date = b.task_date
          GROUP BY b.task_uuid, b.task_id, b.task_date, b.task_name, b.task_type, b.task_status, b.store_id, b.store_name, b.retailer_name, b.supplier_name, b.rep_name, b.comments_from_rep, b.cannot_complete_reason, b.cannot_complete_comments, b.week_start_date, b.recurring, b.one_off
        ), triaged AS (
         SELECT task_qa.task_uuid,
            task_qa.task_id,
            task_qa.task_date,
            task_qa.task_name,
            task_qa.task_type,
            task_qa.task_status,
            task_qa.store_id,
            task_qa.store_name,
            task_qa.retailer_name,
            task_qa.supplier_name,
            task_qa.rep_name,
            task_qa.comments_from_rep,
            task_qa.cannot_complete_reason,
            task_qa.cannot_complete_comments,
            task_qa.week_start_date,
            task_qa.recurring,
            task_qa.one_off,
            task_qa.negative_answer_count,
            task_qa.answered_count,
            task_qa.qa_pairs,
            task_qa.has_issue
           FROM task_qa
          WHERE task_qa.answered_count > 0 OR task_qa.comments_from_rep IS NOT NULL OR task_qa.cannot_complete_comments IS NOT NULL
        ), scored AS (
         SELECT triaged.task_uuid,
            triaged.task_id,
            triaged.task_date,
            triaged.task_name,
            triaged.task_type,
            triaged.task_status,
            triaged.store_id,
            triaged.store_name,
            triaged.retailer_name,
            triaged.supplier_name,
            triaged.rep_name,
            triaged.comments_from_rep,
            triaged.cannot_complete_reason,
            triaged.cannot_complete_comments,
            triaged.week_start_date,
            triaged.recurring,
            triaged.one_off,
            triaged.negative_answer_count,
            triaged.answered_count,
            triaged.qa_pairs,
            triaged.has_issue,
            triaged.negative_answer_count * 2 +
                CASE
                    WHEN triaged.cannot_complete_comments IS NOT NULL THEN 2
                    ELSE 0
                END +
                CASE
                    WHEN triaged.comments_from_rep IS NOT NULL THEN 1
                    ELSE 0
                END AS issue_score
           FROM triaged
        ), summary_agg AS (
         SELECT scored.supplier_name,
            current_setting('app.date_from'::text)::date AS date_from,
            current_setting('app.date_to'::text)::date AS date_to,
            count(*) AS total_tasks,
            count(*) FILTER (WHERE scored.task_status::text = 'done'::text) AS done_tasks,
            count(*) FILTER (WHERE scored.task_status::text = 'approved'::text) AS approved_tasks,
            count(DISTINCT scored.store_id) AS stores_visited,
            count(DISTINCT scored.store_id) FILTER (WHERE scored.has_issue) AS stores_with_issues,
            count(DISTINCT scored.rep_name) AS reps_active,
            count(*) FILTER (WHERE scored.has_issue) AS tasks_with_issues,
            count(*) FILTER (WHERE scored.recurring) AS recurring_tasks,
            count(*) FILTER (WHERE scored.one_off) AS one_off_tasks
           FROM scored
          GROUP BY scored.supplier_name
        ), exception_rows AS (
         SELECT json_agg(json_build_object('task_uuid', capped.task_uuid, 'task_id', capped.task_id, 'store_id', capped.store_id, 'store', capped.store_name, 'state', capped.retailer_name, 'task', capped.task_name, 'status', capped.task_status, 'rep', capped.rep_name, 'date', capped.task_date, 'qa', capped.qa_pairs, 'comment', capped.comments_from_rep, 'cannot_complete', capped.cannot_complete_comments, 'score', capped.issue_score) ORDER BY capped.issue_score DESC, capped.task_date DESC) AS tasks_json
           FROM ( SELECT scored.task_uuid,
                    scored.task_id,
                    scored.task_date,
                    scored.task_name,
                    scored.task_type,
                    scored.task_status,
                    scored.store_id,
                    scored.store_name,
                    scored.retailer_name,
                    scored.supplier_name,
                    scored.rep_name,
                    scored.comments_from_rep,
                    scored.cannot_complete_reason,
                    scored.cannot_complete_comments,
                    scored.week_start_date,
                    scored.recurring,
                    scored.one_off,
                    scored.negative_answer_count,
                    scored.answered_count,
                    scored.qa_pairs,
                    scored.has_issue,
                    scored.issue_score
                   FROM scored
                  ORDER BY scored.issue_score DESC, scored.task_date DESC
                 LIMIT 60) capped
        )
 SELECT json_build_object('supplier', s.supplier_name, 'date_from', s.date_from, 'date_to', s.date_to, 'total_tasks', s.total_tasks, 'done_tasks', s.done_tasks, 'in_progress_tasks', s.in_progress_tasks, 'completion_pct', s.completion_pct, 'stores_visited', s.stores_visited, 'stores_with_issues', s.stores_with_issues, 'reps_active', s.reps_active, 'tasks_with_issues', s.tasks_with_issues, 'recurring_tasks', s.recurring_tasks, 'one_off_tasks', s.one_off_tasks)::jsonb AS summary,
    COALESCE(e.tasks_json::jsonb, '[]'::jsonb) AS tasks
   FROM summary_agg s
     CROSS JOIN exception_rows e;

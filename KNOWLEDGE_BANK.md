# smart_ai_summary_email_client_2 — Knowledge Bank
> For local LLM (Qwen2.5 7B) and developer context. Last updated: 2026-06-09.

---

## What This Project Does

Automates the generation of supplier activity summary emails for GForce Category Solutions.
Currently these are written manually by CRM staff (Sandro, Danielle) who export field task
data to CSV, paste into an LLM, and send to supplier contacts.

This pipeline replaces that with:
- PostgreSQL view pulling structured field data
- Python aggregation to reduce token load
- Local Ollama LLM (Qwen2.5 7B Q4_K_M) generating the email body
- Few-shot examples from approved past emails to maintain tone
- M365 SMTP dispatch (next phase)

---

## Stack

| Layer | Tool | Notes |
|-------|------|-------|
| Runtime | Python 3.12, uv | `uv run` for all commands |
| Task runner | just | see `justfile` |
| Local DB | DuckDB | supplier map + email training data |
| Production DB | PostgreSQL (DigitalOcean managed) | field_ops schema |
| LLM | Ollama, Qwen2.5 7B Q4_K_M | CPU-only on corycia (4–8GB RAM) |
| Email dispatch | M365 SMTP | next phase |

---

## Critical Architecture Decisions

### 1. SQL does the analysis, LLM writes the prose
The view pre-aggregates counts, completion rates, exception flags.
The LLM receives a compact JSON object — it does NOT scan raw rows.

### 2. num_ctx = 4096 — DO NOT INCREASE
On CPU-only inference, doubling num_ctx doubles KV cache RAM and prefill time.
The aggregated prompt is ~1,500-1,700 tokens. 4096 provides optimal headroom for generation.
8192 caused 180s+ timeouts. Current config: 240s timeout, temperature 0.3 for consistency.

### 3. aggregate_tasks() is the primary token reduction step
60 raw exception rows → 5 grouped rows by collapsing same task+answer across stores.
This runs in Python before building the prompt. The view cap is 60 rows; the
aggregated cap is MAX_AGGREGATED_ROWS = 15.

### 4. Few-shot examples trimmed to 1800 chars each
Full email bodies are 4,000–6,000 chars. Trim to 1800 to control budget.
Max 2 examples injected. Cross-supplier fallback fires when no supplier-specific
examples exist yet (most suppliers currently).

### 5. DuckDB is local cache only
`supplier_map.duckdb` and `training_approved_emails.duckdb` are generated artifacts.
Source of truth is always PostgreSQL (DO). Run `just datasync_supplier` to refresh.

---

## Project Structure

```
smart_ai_summary_email_client_2/
├── data/
│   ├── processed/
│   │   ├── supplier_map.duckdb          # generated — sync from PG public.clients
│   │   └── training_approved_emails.duckdb  # email_examples table
│   └── raw/
│       └── email_dot_eml_format/        # 15 approved .eml files
├── src/
│   ├── database/
│   │   ├── connection.py                # PostgreSQLConnection context manager
│   │   └── sync_suppliers.py            # PG → supplier_map.duckdb sync
│   ├── processors/
│   │   └── ingest_eml.py               # .eml parser → email_examples table
│   ├── generators/
│   │   └── email_generator.py          # main pipeline: fetch→aggregate→LLM→validate
│   └── cli/
│       ├── summary.py                  # get_summary() — reused by generator
│       └── tasks.py                    # tabular task inspector
├── justfile
├── pyproject.toml
└── .env                                # PG_DSN, OLLAMA_URL, OLLAMA_MODEL
```

---

## Key Functions

### `src/cli/summary.py → get_summary(supplier, frequency)`
Sets PostgreSQL session vars, queries `field_ops.v_supplier_email_summary`,
returns `{"summary": {...}, "tasks": [...]}`.
Reused directly by `email_generator.py` — do not duplicate this logic.

### `src/generators/email_generator.py → aggregate_tasks(tasks)`
Groups raw exception rows by `(task_name, top_answer)`.
Returns list of dicts: `{task, question, answer, store_count, affected_stores, score, rep_comments?}`.
Strips `task_uuid`, `store_id`, `task_id` — LLM does not need these.

### `src/generators/email_generator.py → generate_email(supplier, frequency, dry_run)`
Full pipeline entry point. Fetches data, aggregates tasks, builds prompt with few-shot examples, 
calls Ollama LLM, validates output. Returns email body string or None.

### `src/processors/ingest_eml.py`
Parses `.eml` files: extracts plain text body, strips signature/disclaimer/opener,
extracts Dropbox links as JSON, detects supplier via DuckDB lookup with
alphanumeric lookaround regex `(?<![a-zA-Z0-9])keyword(?![a-zA-Z0-9])`.

---

## PostgreSQL View: `field_ops.v_supplier_email_summary`

**Location:** DigitalOcean managed PostgreSQL, `field_ops` schema.

**Parameters (session vars):**
```sql
SET app.supplier_name = 'NULON';
SET app.date_from     = '2026-05-01';
SET app.date_to       = '2026-05-31';
SELECT summary, tasks FROM field_ops.v_supplier_email_summary;
```

**Returns:**
- `summary` JSONB: counts (total_tasks, done_tasks, completion_pct, stores_visited,
  stores_with_issues, reps_active, tasks_with_issues, recurring_tasks)
- `tasks` JSONB array: up to 60 exception rows, sorted by `issue_score` DESC.
  Each row: `{task_uuid, task_id, store_id, store, state, task, status, rep, date, qa, comment, cannot_complete, score}`

**Issue scoring weights:**
- `cannot_complete` note = +2
- Each negative answer (NO/NOT/MISSING/NONE) = +2
- Rep comment present = +1
- Status `in_progress` = +1

**Key join rule:** `task_questions` is partitioned — join MUST include `task_date` on both sides:
```sql
LEFT JOIN questions q ON q.task_uuid = b.task_uuid AND q.task_date = b.task_date
```

---

## DuckDB Schema: `email_examples`

```sql
-- in data/processed/training_approved_emails.duckdb
CREATE TABLE email_examples (
    id              INTEGER PRIMARY KEY,
    supplier_name   VARCHAR NOT NULL,
    supplier_id     VARCHAR,
    subject         VARCHAR,
    sent_by         VARCHAR,
    sent_to         VARCHAR,
    sent_at         TIMESTAMP,
    email_body      VARCHAR NOT NULL,
    dropbox_links   JSON,
    source          VARCHAR DEFAULT 'manual',  -- manual | approved | edited
    rating          INTEGER DEFAULT 3,          -- 1=bad 2=ok 3=good
    body_hash       VARCHAR UNIQUE,
    token_estimate  INTEGER,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

Current records: 1 (ACOL). 15 .eml files available to ingest.
Run `just email_ingestion_run` to ingest all.

---

## field_ops Schema Reference

### `field_ops.tasks` (partitioned by task_date)
Key columns used: `id` (UUID, PK part), `task_date` (PK part), `task_id` (string "T-XXXXX"),
`task_name`, `task_status` (enum: done/in_progress), `store_id`, `store_name`,
`retailer_name` (state/region), `supplier_name`, `supplier_id`,
`cover_rep_first_name/last_name`, `senior_rep_first_name/last_name`,
`comments_from_rep`, `cannot_complete_reason`, `cannot_complete_comments`,
`recurring` (bool), `one_off` (bool).

### `field_ops.task_questions` (partitioned by task_date)
Key columns: `task_uuid` (FK → tasks.id), `task_date`, `question`, `answers` (varchar[]),
`answer_from_rep`. Noise rows: `answers = '{""}'` — always filter these out.

### Supplier: OSRAM (OSR001)
- Weekly tasks: ~748 total, ~481 done (64.3%), 228 stores, 104 reps
- Primary issue pattern this week: "01-06-26 GIMBLE DOWNLIGHTS / NO NOT RANGED" at 42 stores
- Known: `retailer_name` = 'BUNNINGS' for most rows (not a state — it's the retailer)

---

## Supplier Map

Key supplier IDs (from `public.clients` / `field_ops.suppliers`):

| supplier_name | user_id | Notes |
|--------------|---------|-------|
| ACOL | ACO014 | acol skylights |
| NULON | NUL001 | AU; NUL002 = NZ |
| OSRAM | OSR001 | AU; ONZ001 = NZ |
| KINCROME | KIN071 | AU; KIN001 = NZ |
| GALINTEL | GAL001 | |
| DINDAS | DIN001 | TAS |
| QEP | QEP002 | AU; QEP001 = NZ |
| TIMEPET | TIM116 | |
| RING | RIN007 | AU; RNZ077 = NZ |
| AHE | AHE001 | short_name: AHE-SEPARATED-HALF-DAY |
| SHEFFIELD | SHE001 | short_name: SHEFFIELD-GROUP |

Collision risk: AU keyword matches inside NZ variant (e.g. RING inside RING-NZ).
Detection uses length-sorted keywords — longer wins.

---

## Environment Variables (.env)

```
PG_DSN=postgresql://user:pass@host:port/dbname
OLLAMA_URL=http://localhost:11434
OLLAMA_MODEL=qwen2.5:7b-instruct-q4_K_M
```

---

## justfile Commands

```
just sync                              # uv sync
just datasync_supplier                 # refresh supplier_map.duckdb from PG
just email_ingestion_test              # dry-run .eml parse (no DB write)
just email_ingestion_run               # ingest all .eml files into email_examples
just supplier_tasks OSRAM weekly 10    # tabular task inspector
just generate OSRAM weekly             # full pipeline: fetch→aggregate→LLM→print
just generate_dry OSRAM weekly         # show prompt only, skip LLM call
```

---

## Current State (2026-06-09)

- ✅ Email Generator Complete: Full production-ready `email_generator.py` with comprehensive Ollama integration
- ✅ Token Management: Intelligent prompt sizing with 2,800 token warning and 4,000 token hard limits
- ✅ Task Aggregation: Advanced grouping logic reducing 60 raw rows to 5 grouped entries (75% token reduction)
- ✅ Professional Output: Structured email format with mandatory sections and business-appropriate tone
- ✅ Error Handling: Robust timeout (240s), connection, and validation error handling for production use
- ✅ Few-shot Learning: DuckDB training integration with supplier-specific examples and cross-supplier fallback
- ✅ CLI Integration: Full command-line interface with dry-run mode and output file saving
- ✅ Output Validation: Automatic checking for required sections (Overview, Issues & Flags, Summary)
- ⏳ M365 SMTP dispatch — next phase for automated email delivery

---

## Known Issues / Watch Points

1. **`retailer_name` = 'BUNNINGS'** for OSRAM rows — not a state. If state grouping
   is needed in the email, a store→state lookup table is required. Currently not in the view.

2. **`task_id` is a template ID** (T-80530 repeated across stores) — not a unique row key.
   Use `task_uuid` (tasks.id UUID) for unique row identification.

3. **Cross-supplier few-shot fallback** — until more emails are ingested per supplier,
   the fallback fires and injects unrelated supplier context. Ingest all 15 .eml files
   to improve supplier-specific matching.

4. **`cannot_complete` and `comment` fields return empty strings `""`** from the view,
   not NULL. The aggregation correctly handles this (strips empty strings before appending).

5. **No M365 dispatch yet** — the generated email body is printed to stdout only.
   Next phase: wire `generate_email()` output into M365 SMTP via existing relay.

6. **Ollama Model Configuration** — using Qwen 2.5 7B Q4_K_M quantization for optimal
   CPU inference speed vs quality balance. Model loads in ~4-8GB RAM.

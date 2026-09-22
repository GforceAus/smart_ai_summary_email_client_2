"""
src/generators/email_generator.py
----------------------------------
Generates supplier activity summary emails using a local Ollama LLM.

Pipeline:
    1. Fetch summary + tasks from field_ops.v_supplier_email_summary
    2. Aggregate repetitive task rows (same task+answer → store list)
    3. Strip LLM-irrelevant metadata fields (UUIDs, store_id, etc.)
    4. Fetch 1-2 few-shot examples from email_examples DuckDB
    5. Build prompt with token guard
    6. POST to Ollama /api/chat
    7. Validate output sections present
    8. Return email body

Usage:
    uv run -m src.generators.email_generator --supplier OSRAM --frequency weekly
    uv run -m src.generators.email_generator --supplier OSRAM --frequency weekly --dry-run
"""

import argparse
import html
import json
import re
import logging
import os
import sys
import time
from collections import defaultdict

import duckdb
from google import genai
from google.genai import types
import requests
from dotenv import load_dotenv

from src.cli.summary import get_summary

load_dotenv()

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

# ── Config ─────────────────────────────────────────────────────────────────

LLM_PROVIDER      = os.environ.get("LLM_PROVIDER",    "ollama")   # "ollama" | "gemini"
OLLAMA_URL        = os.environ.get("OLLAMA_URL",      "http://localhost:11434")
OLLAMA_MODEL      = os.environ.get("OLLAMA_MODEL",    "qwen2.5:7b-instruct-q4_K_M")
GEMINI_MODEL      = os.environ.get("GEMINI_MODEL",    "gemini-2.0-flash")
GOOGLE_API_KEY    = os.environ.get("GOOGLE_API_KEY",  "")
EMAIL_EXAMPLES_DB = "data/processed/training_approved_emails.duckdb"

OLLAMA_TIMEOUT       = 240   # seconds — CPU inference on 7B is slow
MAX_EXAMPLES         = 2     # few-shot cap

# Budgets are provider-dependent. Ollama runs a 7B on CPU, so it stays tight.
# Gemini 2.5 Flash has a ~1M-token input window — the old 4k cap was throttling
# it to ~0.4% of capacity and was the reason whole task groups were dropped.
_IS_GEMINI = LLM_PROVIDER == "gemini"

MAX_AGGREGATED_ROWS  = 400   if _IS_GEMINI else 15
TOKEN_WARN_LIMIT     = 200_000 if _IS_GEMINI else 2_800
TOKEN_HARD_LIMIT     = 800_000 if _IS_GEMINI else 4_000
MAX_OUTPUT_TOKENS    = 8_192   # Sandro-length emails need real output headroom
EXAMPLE_CHAR_LIMIT   = 8_000 if _IS_GEMINI else 1_800
MAX_DESC_CHARS       = 1_200   # per task_description, after HTML stripping

# ── System Prompt ──────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a professional field operations reporting assistant for GForce Category Solutions.
Your job is to write concise, factual supplier activity summary emails based on structured field data.

RULES:
- Write in a professional but direct business tone
- Never invent data — only use what is provided in the JSON payload
- Do not include greetings, sign-off, or signature blocks
- Completion percentages and store counts must match the summary JSON exactly
- Flag issues clearly but without alarm — these are routine operational summaries
- When many stores share the same issue, summarise as a group (e.g. "25 stores reported stock not ranged")

SECTIONING:
- The Task Definitions block tells you what each task covers. Derive the
  report's issue sections from those definitions and the questions asked —
  e.g. a task whose description lists SAFETY / RACK MAINTENANCE /
  DEFECTIVE-DAMAGED STOCK should produce a section per theme, not one flat list.
- Name every affected store in its section. Do not write "several stores".
- Report a question with no issues as a positive confirmation rather than
  omitting it.
SEVERITY (highest priority rule):
- Rows carry a severity of "critical" or "high" where present. Anything
  involving injury, a near miss, or a safety hazard outranks every volume-based
  finding, however many stores the latter affects.
- Keep sections named after the THEME (e.g. "Rack Maintenance", "Safety",
  "Stock Handling"). Never name a section after a severity level — do not
  produce "Critical Issues" or "High Severity Issues" buckets. Severity
  controls the ORDER of the themed sections, not their names.
- Order those themed sections so the one carrying the critical item comes
  first, then those carrying high severity, then the rest.
- A critical item leads its section: name the store, state what happened
  plainly, and give the follow-up required to close it out.
- The Summary must open with the most severe item and its follow-up — not the
  finding with the largest store count.

COUNTING (strict):
- Every store_count is already a DISTINCT store count. Never add store counts
  together: the same store can answer more than one way on the same question,
  so sums double-count. 8 + 4 is not 12 if two stores appear in both.
- For any figure spanning more than one answer, use distinct_stores_total from
  the Question Rollups block, or count the unique names in distinct_stores.
- Any number you state must be either copied from the payload or equal to the
  count of store names you list in that same sentence.
- To combine answers, take the union of their store lists and count the names —
  never add the counts. all_stores_answering counts every store that answered
  the question including clean results, so it is never the figure for a problem.
- In the Summary, do not state any combined store figure at all. Quote a single
  group's count verbatim, or describe the theme without a number. A combined
  count with no store list cannot be checked and has been wrong before.

- still_in_progress_stores lists stores where the task is not yet closed —
  typically rolled over to the next visit. Describe these as outstanding or
  awaiting follow-up, not as completed.

OUTPUT FORMAT:
## Overview
2-3 sentences covering visit volume, completion rate, and general network health.

## Completed Activity
Paragraph summarising what was done across stores. Include states if notable patterns exist.

## Issues & Flags
One "### <Theme>" subsection per theme derived from the Task Definitions.
Under each, bullet the affected stores by name with the finding.
If a theme has no issues, state that in one line.
If nothing at all: "No significant issues identified this period."

## Rep Comments
Bullet list of notable rep observations with store context.
Omit this section entirely if no comments exist.

## Summary
1-2 sentences: the single most important takeaway and any recommended follow-up."""


# ── Task Aggregation ───────────────────────────────────────────────────────

def normalise_task_name(name: str) -> str:
    """
    Strip the leading DD-MM-YY / D-M-YY prefix GFM prepends to task names.

    Without this the same recurring task forks into a new group every week
    ('07-09-26 RECURRING TASK 1' vs '14-09-26 RECURRING TASK 1'), which
    silently splits groups across any multi-week window.
    """
    return re.sub(r"^\s*\d{1,2}-\d{1,2}-\d{2,4}\s*", "", (name or "")).strip()


def strip_html(raw: str) -> str:
    """Flatten the HTML task_description into plain text for the LLM."""
    if not raw:
        return ""
    text = re.sub(r"<br\s*/?>|</p>|</li>", "\n", raw, flags=re.I)
    text = re.sub(r"<li[^>]*>", "- ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


# Field-ops severity vocabulary. Deliberately generic: any client's safety
# question and any rep describing an injury uses this language, so nothing here
# is specific to one supplier's task set.
SEVERITY_PATTERNS: list[tuple[str, str]] = [
    ("critical", r"injur|struck|narrowly missed|missed (?:my|their|his|her) head"
                 r"|fell on|falling|collaps|electrocut|trapped|crush"),
    ("high",     r"\bsafety\b|hazard|unsafe|rack damag|bsafe|\brisk\b"),
]
SEVERITY_RANK = {"critical": 2, "high": 1}

# Answers that mean "nothing found". Note this is an exact-match list on
# purpose: 'NO  MUST COMMENT' on the brochures task IS a finding, so a plain
# startswith('NO') test would wrongly discard it.
NON_ISSUE_ANSWERS = {"NO", "NONE", "NONE REQUIRED", "NO NONE FOUND", ""}


def is_non_issue(answer: str) -> bool:
    return " ".join((answer or "").upper().split()) in NON_ISSUE_ANSWERS


def classify_severity(*texts: str) -> str | None:
    """
    Return the highest severity matched across the given texts.

    Applied to the question, the task description and the rep's own comment,
    because a near-miss is often recorded as free text on a question whose
    dropdown answer is 'NO'.
    """
    blob = " ".join(t for t in texts if t).lower()
    for name, pattern in SEVERITY_PATTERNS:
        if re.search(pattern, blob):
            return name
    return None


def aggregate_tasks(tasks: list[dict], descriptions: dict | None = None) -> list[dict]:
    """
    Collapse repetitive rows into grouped records keyed by
    (normalised task name, question, answer).

    Every QA pair on a task contributes to its own group. The previous version
    keyed on (task_name, qa[0].answer) and therefore discarded every question
    after the first — on PORTA-TIMBER's RECURRING TASK 1 that silently dropped
    the SAFETY and DEFECTIVE-DAMAGED STOCK questions on every run.
    """
    descriptions = descriptions or {}
    groups = defaultdict(lambda: {
        "stores": [],
        "score": 0,
        "comments": [],
        "cannot_complete": [],
        "outstanding": [],   # stores where the task is still in_progress
    })

    for t in tasks:
        task_name = normalise_task_name(t.get("task", ""))
        store     = t.get("store")
        qa_list   = t.get("qa") or []

        for qa in qa_list:
            q = (qa.get("q") or "").strip()
            a = (qa.get("a") or "").strip()
            if not q:
                continue

            g = groups[(task_name, q, a)]
            g["task"]     = task_name
            g["question"] = q
            g["answer"]   = a
            g["score"]    = max(g["score"], t.get("score", 0))
            if store:
                g["stores"].append(store)
                # in_progress is a meaningful state, not noise: rolled-over
                # tasks (e.g. brochures not yet delivered) stay in_progress by
                # design, so surface it rather than filtering these rows out.
                if t.get("status") == "in_progress":
                    g["outstanding"].append(store)

        # Comments/cannot-complete belong to the task, not one question — attach
        # them to the task's first question group so they are not duplicated
        # once per question.
        # Attach the comment to a question this task actually flagged, rather
        # than blindly the first one. Without this a rep's near-miss report
        # lands under a question whose answer was 'NO' — i.e. filed as a
        # non-issue.
        answered = [((qa.get("q") or "").strip(), (qa.get("a") or "").strip())
                    for qa in qa_list if (qa.get("q") or "").strip()]
        target = None
        comment_text = (t.get("comment") or "") + " " + (t.get("cannot_complete") or "")
        comment_sev = classify_severity(comment_text)
        if comment_sev:
            # Prefer a question matching the comment's severity theme.
            target = next((qa for qa in answered
                           if classify_severity(qa[0]) == comment_sev), None)
        if target is None:
            target = next((qa for qa in answered if not is_non_issue(qa[1])), None)
        if target is None and answered:
            target = answered[0]
        if target is not None:
            first_q, first_a = target
            g = groups[(task_name, first_q, first_a)]
            comment = t.get("comment")
            if comment and comment.strip():
                g["comments"].append(f"{store}: {comment.strip()}")
            cc = t.get("cannot_complete")
            if cc and cc.strip():
                g["cannot_complete"].append(f"{store}: {cc.strip()}")

    result = []
    for (task_name, q, a), g in groups.items():
        row = {
            "task":            task_name,
            "question":        q,
            "answer":          a,
            # Deduplicate: a store recurs across dates within the window, so
            # len(stores) counts row occurrences, not distinct stores.
            "store_count":     len(set(g["stores"])),
            "affected_stores": sorted(set(g["stores"])),
            "score":           g["score"],
        }
        # Description is emitted once in the Task Definitions legend, not
        # repeated on every row.
        # A rep's comment can raise severity even on a 'NO' row (a near miss
        # is often written up against a question whose dropdown says NO), but
        # the question text alone must not: 50 stores answering NO to a safety
        # question is a clean result, not a safety incident.
        severity = classify_severity(" ".join(g["comments"]))
        if severity is None and not is_non_issue(a):
            severity = classify_severity(q, descriptions.get(task_name, ""))
        if severity:
            row["severity"] = severity
        outstanding = sorted(set(g["outstanding"]))
        if outstanding:
            row["still_in_progress_stores"] = outstanding
            row["still_in_progress_count"]  = len(outstanding)
        if g["comments"]:
            row["rep_comments"] = g["comments"]
        if g["cannot_complete"]:
            row["cannot_complete"] = g["cannot_complete"]
        result.append(row)

    def rank(row: dict) -> tuple:
        # A group whose answer means "nothing found" keeps its severity label
        # (the comment is still worth showing) but must not outrank real
        # findings — otherwise 48 stores reporting NO sorts above an actual
        # hazard because one attached comment mentions safety.
        sev = 0 if is_non_issue(row["answer"]) else \
            SEVERITY_RANK.get(row.get("severity"), 0)
        return (sev, row["score"], row["store_count"])

    result.sort(key=rank, reverse=True)
    return result[:MAX_AGGREGATED_ROWS]


def build_question_rollups(aggregated: list[dict]) -> dict:
    """
    Precompute distinct-store unions per question.

    The LLM cannot be trusted to union overlapping store sets: given
    'NO MUST COMMENT' (8 stores) and 'OTHER MUST COMMENT' (4 stores) that
    share 2 stores, it reports 12 rather than 10. Every cross-answer total is
    computed here instead, so the model never has to add counts together.
    """
    by_q: dict = {}
    for row in aggregated:
        q = row["question"]
        entry = by_q.setdefault(q, {
            "task": row["task"],
            "answers": {},
            "_union": set(),
        })
        stores = row.get("affected_stores") or []
        entry["answers"][row["answer"]] = {
            "store_count": len(set(stores)),
            "stores": sorted(set(stores)),
        }
        entry["_union"].update(stores)

    rollups = {}
    for q, entry in by_q.items():
        union = sorted(entry.pop("_union"))
        # Every store that answered this question, whatever the answer.
        # Answer polarity is question-dependent ('YES photos attached' is a
        # good result for brochures, 'YES MISSING CLIPS' is a bad one for rack
        # maintenance), so an "issues only" union cannot be derived here — the
        # per-answer store lists are the reliable basis for any combination.
        entry["all_stores_answering"] = len(union)
        entry["distinct_stores"] = union
        rollups[q] = entry
    return rollups


def fetch_task_context(supplier: str, date_from: str, date_to: str) -> tuple[list[dict], dict]:
    """
    Fetch the full task set straight from field_ops, bypassing the
    LIMIT 60 baked into v_supplier_email_summary, and return the
    task_description for each task name.

    The view caps its tasks payload at the 60 highest-scoring rows. For
    PORTA-TIMBER that discarded ~200 of 262 tasks-with-issues before the
    generator ever saw them. The view is left untouched — it still supplies
    the summary metrics.

    Returns (tasks, descriptions) where tasks matches the view's row shape.
    """
    from src.database.connection import PostgreSQLConnection

    rows_by_task: dict = {}
    descriptions: dict = {}

    with PostgreSQLConnection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT t.task_id, t.id::text AS task_uuid, t.task_date, t.task_name,
                       t.task_description, t.task_status, t.store_id, t.store_name,
                       s.state,
                       COALESCE(
                           NULLIF(TRIM(t.cover_rep_first_name || ' ' || t.cover_rep_last_name), ''),
                           NULLIF(TRIM(t.senior_rep_first_name || ' ' || t.senior_rep_last_name), '')
                       ) AS rep_name,
                       t.comments_from_rep, t.cannot_complete_comments,
                       tq.question, tq.answer_from_rep
                FROM field_ops.tasks t
                LEFT JOIN field_ops.stores s ON s.store_id = t.store_id
                LEFT JOIN field_ops.task_questions tq
                       ON tq.task_uuid = t.id
                      AND tq.answer_from_rep IS NOT NULL
                      AND tq.answer_from_rep <> ''
                WHERE t.supplier_name = %s
                  AND t.task_date >= %s::date
                  AND t.task_date <= %s::date
                  AND t.task_status IN ('done', 'in_progress')
                ORDER BY t.task_name, t.store_name
            """, (supplier, date_from, date_to))

            for r in cur.fetchall():
                (task_id, task_uuid, task_date, task_name, task_desc, status,
                 store_id, store, state, rep, comment, cannot, question, answer) = r

                norm = normalise_task_name(task_name)
                if task_desc and norm not in descriptions:
                    descriptions[norm] = strip_html(task_desc)

                key = (task_id, store_id)
                row = rows_by_task.get(key)
                if row is None:
                    row = rows_by_task[key] = {
                        "task_id": task_id, "task_uuid": task_uuid,
                        "date": task_date, "task": task_name, "store": store,
                        "store_id": store_id, "state": state, "rep": rep,
                        "status": status, "comment": comment or "",
                        "cannot_complete": cannot or "", "qa": [], "score": 0,
                    }
                if question:
                    row["qa"].append({"q": question, "a": answer})
                    # Negative answers carry the signal; score drives ordering.
                    if (answer or "").strip().upper() not in ("NO", "N/A", ""):
                        row["score"] += 1

    return list(rows_by_task.values()), descriptions


def strip_llm_metadata(summary: dict) -> dict:
    """Remove fields that are only useful for Python, not the LLM."""
    drop = {"one_off_tasks"}  # low-signal field for email narrative
    return {k: v for k, v in summary.items() if k not in drop}


# ── Few-shot Fetch ─────────────────────────────────────────────────────────

def fetch_examples(supplier_name: str) -> list[str]:
    """
    Fetch up to MAX_EXAMPLES approved email bodies from DuckDB.
    Prefers supplier-specific; falls back to cross-supplier.
    Trims each example to EXAMPLE_CHAR_LIMIT chars to control token budget.
    """
    try:
        con = duckdb.connect(EMAIL_EXAMPLES_DB, read_only=True)

        rows = con.execute("""
            SELECT email_body
            FROM email_examples
            WHERE supplier_name = ?
              AND source IN ('manual', 'approved', 'edited')
              AND rating >= 2
            ORDER BY created_at DESC
            LIMIT ?
        """, [supplier_name, MAX_EXAMPLES]).fetchall()

        if not rows:
            logger.info(f"No examples for {supplier_name} — using cross-supplier fallback")
            rows = con.execute("""
                SELECT email_body
                FROM email_examples
                WHERE source IN ('manual', 'approved', 'edited')
                  AND rating >= 2
                ORDER BY created_at DESC
                LIMIT ?
            """, [MAX_EXAMPLES]).fetchall()

        con.close()
        # Trim to control token budget — 1800 chars ≈ 450 tokens each
        return [r[0][:EXAMPLE_CHAR_LIMIT] for r in rows]

    except Exception as e:
        logger.warning(f"Could not fetch few-shot examples: {e}")
        return []


# ── Prompt Builder ─────────────────────────────────────────────────────────

def build_prompt(
    summary: dict,
    tasks: list[dict],
    examples: list[str],
    supplier: str,
    frequency: str,
    descriptions: dict | None = None,
) -> tuple[str, int]:
    """Build user message. Returns (prompt_text, token_estimate)."""
    parts = []

    # Few-shot block
    for i, body in enumerate(examples, 1):
        parts.append(f"## Example Email {i}\n{body}")

    if examples:
        parts.append("---")

    # Aggregate + strip metadata before serialising
    aggregated = aggregate_tasks(tasks, descriptions)
    clean_summary = strip_llm_metadata(summary)

    logger.info(
        f"Task aggregation: {len(tasks)} raw rows → {len(aggregated)} grouped rows"
    )

    parts.append(f"## Data Payload — {supplier} ({frequency})")
    parts.append("### Summary Metrics")
    parts.append(json.dumps(clean_summary, indent=2, default=str))
    if descriptions:
        parts.append("### Task Definitions")
        parts.append(
            "What each task actually asks the rep to do. Use these to decide "
            "how to group and title the sections of the email."
        )
        parts.append(json.dumps(
            {k: v[:MAX_DESC_CHARS] for k, v in descriptions.items()},
            indent=2, default=str,
        ))
    parts.append("### Question Rollups (precomputed — use these for any total)")
    parts.append(json.dumps(build_question_rollups(aggregated), indent=2, default=str))
    parts.append("### Aggregated Exception Rows (sorted worst-first)")
    parts.append(json.dumps(aggregated, indent=2, default=str))
    parts.append("---")
    parts.append(
        f"Write the supplier activity summary email for {supplier} "
        f"covering the {frequency} period from "
        f"{summary.get('date_from')} to {summary.get('date_to')}."
    )

    prompt = "\n\n".join(parts)
    token_estimate = len(prompt) // 4

    return prompt, token_estimate


# ── Ollama Call ────────────────────────────────────────────────────────────

def _call_ollama(system_prompt: str, user_prompt: str) -> str:
    payload = {
        "model":  OLLAMA_MODEL,
        "stream": False,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_prompt},
        ],
        "options": {
            "temperature":    0.3,
            "num_ctx":        4096,  # DO NOT increase — higher = more RAM + slower prefill on CPU
            "num_predict":    2048,  # cap output tokens — emails never exceed ~700 tokens
            "repeat_penalty": 1.3,   # penalise repeated phrases/tokens
        }
    }
    url = f"{OLLAMA_URL}/api/chat"
    logger.info(f"Calling Ollama — model: {OLLAMA_MODEL}, timeout: {OLLAMA_TIMEOUT}s")
    t0 = time.time()
    resp = requests.post(url, json=payload, timeout=OLLAMA_TIMEOUT)
    resp.raise_for_status()
    logger.info(f"Ollama responded in {time.time() - t0:.1f}s")
    return resp.json()["message"]["content"]


# Cumulative token usage across all Gemini calls in this process
_token_usage: dict[str, int] = {"input": 0, "output": 0}


def get_token_usage() -> dict[str, int]:
    return dict(_token_usage)


def _call_gemini(system_prompt: str, user_prompt: str) -> str:
    client = genai.Client(api_key=GOOGLE_API_KEY)
    logger.info(f"Calling Gemini — model: {GEMINI_MODEL}")
    t0 = time.time()
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=user_prompt,
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=0.3,
            max_output_tokens=MAX_OUTPUT_TOKENS,
        ),
    )
    elapsed = time.time() - t0
    usage = response.usage_metadata
    if usage:
        _token_usage["input"]  += usage.prompt_token_count or 0
        _token_usage["output"] += usage.candidates_token_count or 0
        logger.info(
            f"Gemini responded in {elapsed:.1f}s — "
            f"tokens: {usage.prompt_token_count} in / {usage.candidates_token_count} out"
        )
    else:
        logger.info(f"Gemini responded in {elapsed:.1f}s")
    return response.text


def call_llm(system_prompt: str, user_prompt: str) -> str:
    if LLM_PROVIDER == "gemini":
        return _call_gemini(system_prompt, user_prompt)
    return _call_ollama(system_prompt, user_prompt)


# ── Output Validation ──────────────────────────────────────────────────────

REQUIRED_SECTIONS = ["## Overview", "## Issues & Flags", "## Summary"]


def check_store_counts(text: str) -> list[str]:
    """
    Flag any "N stores" claim where N does not match the store names listed
    alongside it.

    The model reliably miscounts unions of overlapping store sets (reporting
    8 + 4 as 12 when two stores appear in both). The rollups in the payload
    prevent most of it; this catches what leaks through.

    The list may appear either after the count ("8 stores: A, B, C") or before
    it ("**A, B, C**: 8 stores reported ..."), so both sides are considered and
    the nearest list wins. Claims with no list alongside them — a bare "12
    stores" in the Summary — cannot be checked this way.
    """
    NAME = r"[A-Z][A-Z0-9 '&./\-]{2,}"

    def names_in(segment: str) -> list[str]:
        out = []
        for part in segment.split(","):
            # Strip list bullets and bold markers from both ends, e.g.
            # "*   **ALICE SPRINGS" -> "ALICE SPRINGS".
            p = re.sub(r"^[\s\*\-•]+", "", part)
            p = re.sub(r"[\s\*]+$", "", p)
            if re.fullmatch(NAME, p):
                out.append(p)
        return out

    problems = []
    for i, line in enumerate(text.splitlines(), 1):
        for m in re.finditer(r"(\d+)\s*\**\s*stores?\b", line, re.I):
            claimed = int(m.group(1))
            before, after = line[:m.start()], line[m.end():]

            # A list immediately preceding the count, e.g. "**A, B, C**: 8 stores"
            lead = re.search(r"([^:]+):\s*\**\s*$", before)
            lead_names = names_in(lead.group(1)) if lead else []

            # Otherwise the first parenthesised or colon-introduced list after.
            paren = re.search(r"\(([^)]+)\)", after)
            colon = re.search(r":\s*([^.]+)", after)
            if paren and (not colon or paren.start() < colon.start()):
                trail_names = names_in(paren.group(1))
            elif colon:
                trail_names = names_in(colon.group(1))
            else:
                trail_names = []

            names = lead_names if len(lead_names) >= 2 else trail_names
            if len(names) >= 2 and claimed != len(names):
                problems.append(
                    f"line {i}: claims {claimed} stores but lists {len(names)}"
                    f" ({', '.join(names)})"
                )
    return problems


def validate_output(text: str) -> list[str]:
    return [s for s in REQUIRED_SECTIONS if s not in text]


# ── Main Generator ─────────────────────────────────────────────────────────

def generate_email(
    supplier: str,
    frequency: str,
    dry_run: bool = False,
) -> str | None:

    # 1. Fetch from PostgreSQL view
    logger.info(f"Fetching view data for {supplier} ({frequency})")
    result = get_summary(supplier, frequency)

    if not result:
        logger.warning(f"No data returned for {supplier} ({frequency})")
        return None

    summary = result["summary"]
    tasks   = result["tasks"]

    # The view caps its tasks payload at 60 rows. Re-fetch the full set plus
    # task_description; fall back to the view's rows if that query fails.
    descriptions: dict = {}
    try:
        full_tasks, descriptions = fetch_task_context(
            supplier, str(summary.get("date_from")), str(summary.get("date_to"))
        )
        if full_tasks:
            logger.info(
                f"Full fetch: {len(tasks)} view rows (capped) -> "
                f"{len(full_tasks)} rows, {len(descriptions)} task descriptions"
            )
            tasks = full_tasks
    except Exception as e:
        logger.warning(f"Full task fetch failed, using capped view rows: {e}")

    logger.info(
        f"View returned: {summary.get('total_tasks')} tasks, "
        f"{len(tasks)} raw exception rows, "
        f"{summary.get('completion_pct')}% completion"
    )

    # 2. Fetch few-shot examples
    examples = fetch_examples(supplier)
    logger.info(f"Few-shot examples loaded: {len(examples)}")

    # 3. Build prompt (aggregation + strip happens inside)
    user_prompt, token_estimate = build_prompt(
        summary, tasks, examples, supplier, frequency, descriptions
    )

    logger.info(f"Prompt token estimate: ~{token_estimate}")

    if token_estimate > TOKEN_HARD_LIMIT:
        logger.error(
            f"Prompt too large ({token_estimate} tokens, hard limit {TOKEN_HARD_LIMIT}). "
            f"Reduce MAX_AGGREGATED_ROWS or example length."
        )
        return None

    if token_estimate > TOKEN_WARN_LIMIT:
        logger.warning(f"Prompt is large ({token_estimate} tokens) — expect slow inference on CPU")

    if dry_run:
        print(f"\n{'='*60}")
        print(f"SYSTEM PROMPT ({len(SYSTEM_PROMPT)} chars)")
        print("="*60)
        print(SYSTEM_PROMPT)
        print(f"\n{'='*60}")
        print(f"USER PROMPT (~{token_estimate} tokens, {len(user_prompt)} chars)")
        print("="*60)
        print(user_prompt)
        return None

    # 4. Call LLM
    try:
        output = call_llm(SYSTEM_PROMPT, user_prompt)
    except requests.exceptions.Timeout:
        logger.error(
            f"Ollama timed out after {OLLAMA_TIMEOUT}s. "
            f"Prompt was ~{token_estimate} tokens. "
            f"Try reducing MAX_AGGREGATED_ROWS (currently {MAX_AGGREGATED_ROWS})."
        )
        return None
    except requests.exceptions.ConnectionError:
        logger.error(f"Cannot reach Ollama at {OLLAMA_URL} — is it running?")
        return None
    except Exception as e:
        logger.error(f"Ollama call failed: {e}")
        return None

    # 5. Validate
    missing = validate_output(output)
    if missing:
        logger.warning(f"Output missing sections: {missing}")

    count_problems = check_store_counts(output)
    for problem in count_problems:
        logger.error(f"STORE COUNT MISMATCH — {problem}")
    if count_problems:
        logger.error(
            f"{len(count_problems)} store-count mismatch(es) detected. "
            f"Review before sending to a client."
        )

    logger.info(f"Generated email: {len(output)} chars (~{len(output)//4} tokens)")
    return output


# ── CLI ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Generate supplier summary email via local LLM")
    parser.add_argument("--supplier",  required=True)
    parser.add_argument("--frequency", choices=["weekly", "fortnightly", "monthly"],
                        default="weekly")
    parser.add_argument("--dry-run",   action="store_true",
                        help="Print prompt only, skip Ollama call")
    parser.add_argument("--output",    help="Save generated email body to file")
    args = parser.parse_args()

    email_body = generate_email(
        supplier=args.supplier,
        frequency=args.frequency,
        dry_run=args.dry_run,
    )

    if email_body:
        print(f"\n{'='*60}")
        print(f"GENERATED EMAIL — {args.supplier} ({args.frequency})")
        print("="*60)
        print(email_body)

        if args.output:
            with open(args.output, "w") as f:
                f.write(email_body)
            logger.info(f"Saved to {args.output}")


if __name__ == "__main__":
    main()

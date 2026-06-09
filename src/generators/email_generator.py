"""
src/generators/email_generator.py
----------------------------------
Generates supplier activity summary emails using a local Ollama LLM.

Pipeline:
    1. Fetch summary + tasks from field_ops.v_supplier_email_summary (via existing CLI logic)
    2. Fetch 1-2 approved few-shot examples from email_examples (DuckDB)
    3. Build prompt: system instructions + few-shot examples + JSON payload
    4. POST to Ollama /api/chat
    5. Validate output contains expected sections
    6. Return generated email body

Usage (standalone):
    uv run -m src.generators.email_generator --supplier NULON --frequency weekly
    uv run -m src.generators.email_generator --supplier NULON --frequency weekly --dry-run
"""

import argparse
import json
import logging
import os
import sys
import time
from collections import defaultdict
from datetime import datetime

import duckdb
import requests
from dotenv import load_dotenv

# Reuse existing fetch logic — don't duplicate the SET + view query
from src.cli.summary import get_summary

load_dotenv()

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

# ── Config ────────────────────────────────────────────────────────────────────

OLLAMA_URL   = os.environ.get("OLLAMA_URL",   "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b-instruct-q4_K_M")
SUPPLIER_MAP_DB = "data/processed/supplier_map.duckdb"
EMAIL_EXAMPLES_DB = "data/processed/training_approved_emails.duckdb"

OLLAMA_TIMEOUT    = 180   # seconds — CPU inference is slow
MAX_EXAMPLES      = 2     # few-shot cap: quality gain inverts above 2
MAX_PROMPT_TASKS  = 25    # only send top N tasks to LLM to stay in context
TOKEN_WARN_LIMIT  = 6_000 # warn if prompt approaches ctx window
TOKEN_HARD_LIMIT  = 7_500 # abort before sending — something is wrong

# ── System Prompt ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a professional field operations reporting assistant for GForce Category Solutions.
Your job is to write concise, factual supplier activity summary emails based on structured field data.

RULES:
- Write in a professional but direct business tone
- Never invent data — only use what is provided in the JSON payload
- Do not include greetings like "I hope you're well" or filler phrases
- Do not include sign-off or signature blocks
- Completion percentages and store counts must match the summary JSON exactly
- Flag issues clearly but without alarm — these are routine operational summaries

OUTPUT FORMAT (use these exact section headers):
## Overview
2-3 sentences covering visit volume, completion rate, and general network health.

## Completed Activity
Paragraph summarising what was done across stores. Include states if notable patterns exist.

## Issues & Flags
Bullet list. Format each as: Store Name — Task — what the rep found or could not complete.
If no issues: write "No significant issues identified this period."

## Rep Comments
Bullet list of notable rep observations. Include store context.
If no comments: omit this section entirely.

## Summary
1-2 sentences: the single most important takeaway and any recommended follow-up action."""


# ── Few-shot Fetch ────────────────────────────────────────────────────────────

def fetch_examples(supplier_name: str) -> list[str]:
    """
    Fetch up to MAX_EXAMPLES approved email bodies from DuckDB for this supplier.
    Falls back to any supplier if none exist for this one specifically.
    Returns list of email body strings.
    """
    try:
        con = duckdb.connect(EMAIL_EXAMPLES_DB, read_only=True)

        # Try supplier-specific first
        rows = con.execute("""
            SELECT email_body
            FROM email_examples
            WHERE supplier_name = ?
              AND source IN ('manual', 'approved', 'edited')
              AND rating >= 2
            ORDER BY created_at DESC
            LIMIT ?
        """, [supplier_name, MAX_EXAMPLES]).fetchall()

        # Fall back to any supplier if nothing found
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
        return [r[0] for r in rows]

    except Exception as e:
        logger.warning(f"Could not fetch few-shot examples: {e}")
        return []


# ── Prompt Builder ────────────────────────────────────────────────────────────

def build_prompt(
    summary: dict,
    tasks: list[dict],
    examples: list[str],
    supplier: str,
    frequency: str,
) -> tuple[str, int]:
    """
    Build the user message content.
    Returns (prompt_text, token_estimate).
    """
    parts = []

    # Few-shot block
    for i, body in enumerate(examples, 1):
        # Trim example to 500 tokens max (2000 chars) to control budget
        trimmed = body[:2000] if len(body) > 2000 else body
        parts.append(f"## Example Email {i}\n{trimmed}")

    if examples:
        parts.append("---")

    # Data payload
    parts.append(f"## Data Payload — {supplier} ({frequency})")
    parts.append("### Summary Metrics")
    parts.append(json.dumps(summary, indent=2, default=str))

    # Cap tasks to stay within token limits
    prompt_tasks = tasks[:MAX_PROMPT_TASKS]
    parts.append(f"### Top {len(prompt_tasks)} Task Exception Rows (sorted worst-first)")
    aggregated = aggregate_tasks(tasks)
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


# ── Ollama Call ───────────────────────────────────────────────────────────────

def call_ollama(system_prompt: str, user_prompt: str) -> str:
    """
    POST to Ollama /api/chat.
    Returns the assistant message content.
    Raises on timeout or non-200.
    """
    payload = {
        "model":  OLLAMA_MODEL,
        "stream": False,
        "messages": [
            {"role": "system",  "content": system_prompt},
            {"role": "user",    "content": user_prompt},
        ],
        "options": {
            "temperature": 0.3,   # low temp: consistent, factual output
            "num_ctx":     8192,
        }
    }

    url = f"{OLLAMA_URL}/api/chat"
    logger.info(f"Calling Ollama at {url} — model: {OLLAMA_MODEL}")
    t0 = time.time()

    resp = requests.post(url, json=payload, timeout=OLLAMA_TIMEOUT)
    resp.raise_for_status()

    elapsed = time.time() - t0
    logger.info(f"Ollama responded in {elapsed:.1f}s")

    data = resp.json()
    return data["message"]["content"]

def aggregate_tasks(tasks: list[dict]) -> list[dict]:
    """
    Collapse repeated task+answer combinations into grouped rows.
    Converts 25 identical "NO NOT RANGED" rows into 1 row with store_count + store list.
    """
    groups = defaultdict(lambda: {
        "stores": [], "score": 0, "sample_comment": None
    })

    for t in tasks:
        # Group key: task name + top QA answer
        top_qa = (t.get("qa") or [{}])[0]
        key = (t.get("task", ""), top_qa.get("a", ""))

        g = groups[key]
        g["task"]           = t.get("task")
        g["task_id"]        = t.get("task_id")
        g["question"]       = top_qa.get("q")
        g["answer"]         = top_qa.get("a")
        g["score"]          = max(g["score"], t.get("score", 0))
        g["stores"].append(t.get("store", ""))
        if t.get("comment") and not g["sample_comment"]:
            g["sample_comment"] = t.get("comment")

    result = []
    for (task_name, answer), g in groups.items():
        result.append({
            "task":           g["task"],
            "task_id":        g["task_id"],
            "question":       g["question"],
            "answer":         g["answer"],
            "store_count":    len(g["stores"]),
            "affected_stores": g["stores"],
            "score":          g["score"],
            "comment":        g["sample_comment"],
        })

    return sorted(result, key=lambda x: x["score"], reverse=True)


# ── Output Validation ─────────────────────────────────────────────────────────

REQUIRED_SECTIONS = ["## Overview", "## Issues & Flags", "## Summary"]

def validate_output(text: str) -> list[str]:
    """Returns list of missing required sections. Empty = valid."""
    return [s for s in REQUIRED_SECTIONS if s not in text]


# ── Main Generator ────────────────────────────────────────────────────────────

def generate_email(
    supplier: str,
    frequency: str,
    dry_run: bool = False,
) -> str | None:
    """
    Full pipeline: fetch → build prompt → call LLM → validate → return body.
    Returns generated email body string, or None if no data found.
    """

    # 1. Fetch from PostgreSQL view
    logger.info(f"Fetching view data for {supplier} ({frequency})")
    result = get_summary(supplier, frequency)

    if not result:
        logger.warning(f"No data returned for {supplier} ({frequency})")
        return None

    summary = result["summary"]
    tasks   = result["tasks"]

    logger.info(
        f"View returned: {summary.get('total_tasks')} tasks, "
        f"{len(tasks)} exception rows, "
        f"{summary.get('completion_pct')}% completion"
    )

    # 2. Fetch few-shot examples
    examples = fetch_examples(supplier)
    logger.info(f"Few-shot examples loaded: {len(examples)}")

    # 3. Build prompt
    user_prompt, token_estimate = build_prompt(
        summary, tasks, examples, supplier, frequency
    )

    logger.info(f"Prompt token estimate: ~{token_estimate}")

    if token_estimate > TOKEN_HARD_LIMIT:
        logger.error(
            f"Prompt too large ({token_estimate} tokens) — "
            f"hard limit is {TOKEN_HARD_LIMIT}. Aborting."
        )
        return None

    if token_estimate > TOKEN_WARN_LIMIT:
        logger.warning(
            f"Prompt approaching context limit ({token_estimate} tokens). "
            f"Consider reducing task cap in view."
        )

    if dry_run:
        logger.info("DRY RUN — skipping Ollama call")
        print(f"\n{'='*60}")
        print(f"SYSTEM PROMPT ({len(SYSTEM_PROMPT)} chars)")
        print('='*60)
        print(SYSTEM_PROMPT)
        print(f"\n{'='*60}")
        print(f"USER PROMPT (~{token_estimate} tokens, {len(user_prompt)} chars)")
        print('='*60)
        print(user_prompt)
        return None

    # 4. Call Ollama
    try:
        output = call_ollama(SYSTEM_PROMPT, user_prompt)
    except requests.exceptions.Timeout:
        logger.error(f"Ollama timed out after {OLLAMA_TIMEOUT}s")
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
        logger.warning("Returning output anyway — check quality manually")

    logger.info(f"Generated email: {len(output)} chars, ~{len(output)//4} tokens")
    return output


# ── CLI Entry Point ───────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Generate supplier summary email via local LLM")
    parser.add_argument("--supplier",  required=True,
                        help="Supplier name matching field_ops (e.g. NULON)")
    parser.add_argument("--frequency", choices=["weekly", "fortnightly", "monthly"],
                        default="weekly")
    parser.add_argument("--dry-run",   action="store_true",
                        help="Print prompt only — do not call Ollama")
    parser.add_argument("--output",    help="Save generated email to this file path")
    args = parser.parse_args()

    email_body = generate_email(
        supplier=args.supplier,
        frequency=args.frequency,
        dry_run=args.dry_run,
    )

    if email_body:
        print(f"\n{'='*60}")
        print(f"GENERATED EMAIL — {args.supplier} ({args.frequency})")
        print('='*60)
        print(email_body)

        if args.output:
            with open(args.output, "w") as f:
                f.write(email_body)
            logger.info(f"Saved to {args.output}")


if __name__ == "__main__":
    main()

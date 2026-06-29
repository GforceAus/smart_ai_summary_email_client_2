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
import json
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
MAX_AGGREGATED_ROWS  = 15    # hard cap on grouped exception rows sent to LLM
TOKEN_WARN_LIMIT     = 2_800
TOKEN_HARD_LIMIT     = 4_000

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

OUTPUT FORMAT (use these exact section headers):
## Overview
2-3 sentences covering visit volume, completion rate, and general network health.

## Completed Activity
Paragraph summarising what was done across stores. Include states if notable patterns exist.

## Issues & Flags
Bullet list. Format: Store group or individual store — Task — finding.
If no issues: write "No significant issues identified this period."

## Rep Comments
Bullet list of notable rep observations with store context.
Omit this section entirely if no comments exist.

## Summary
1-2 sentences: the single most important takeaway and any recommended follow-up."""


# ── Task Aggregation ───────────────────────────────────────────────────────

def aggregate_tasks(tasks: list[dict]) -> list[dict]:
    """
    Collapse repetitive task+answer rows into grouped records.

    Example: 25 rows of "GIMBLE DOWNLIGHTS / NO NOT RANGED" at different stores
    becomes 1 row: {task, question, answer, store_count: 25, affected_stores: [...]}

    This is the primary token reduction step for the LLM payload.
    """
    groups = defaultdict(lambda: {
        "stores": [],
        "score": 0,
        "comments": [],
        "cannot_complete": [],
    })

    for t in tasks:
        qa_list = t.get("qa") or []
        # Use the first (highest-scored) QA pair as the group key
        top_qa = qa_list[0] if qa_list else {}
        key = (
            t.get("task", ""),
            top_qa.get("a", ""),
        )

        g = groups[key]
        g["task"]     = t.get("task", "")
        g["task_id"]  = t.get("task_id", "")
        g["question"] = top_qa.get("q", "")
        g["answer"]   = top_qa.get("a", "")
        g["score"]    = max(g["score"], t.get("score", 0))

        store = t.get("store")
        if store:
            g["stores"].append(store)

        comment = t.get("comment")
        if comment and comment.strip():
            g["comments"].append(f"{store}: {comment.strip()}")

        cc = t.get("cannot_complete")
        if cc and cc.strip():
            g["cannot_complete"].append(f"{store}: {cc.strip()}")

    result = []
    for g in groups.values():
        row = {
            "task":            g["task"],
            "question":        g["question"],
            "answer":          g["answer"],
            "store_count":     len(g["stores"]),
            "affected_stores": g["stores"],
            "score":           g["score"],
        }
        # Only include comment fields if they have content
        if g["comments"]:
            row["rep_comments"] = g["comments"]
        if g["cannot_complete"]:
            row["cannot_complete"] = g["cannot_complete"]

        result.append(row)

    # Sort worst-first, cap at MAX_AGGREGATED_ROWS
    result.sort(key=lambda x: x["score"], reverse=True)
    return result[:MAX_AGGREGATED_ROWS]


def strip_llm_metadata(summary: dict) -> dict:
    """Remove fields that are only useful for Python, not the LLM."""
    drop = {"one_off_tasks"}  # low-signal field for email narrative
    return {k: v for k, v in summary.items() if k not in drop}


# ── Few-shot Fetch ─────────────────────────────────────────────────────────

def fetch_examples(supplier_name: str) -> list[str]:
    """
    Fetch up to MAX_EXAMPLES approved email bodies from DuckDB.
    Prefers supplier-specific; falls back to cross-supplier.
    Trims each example to 1800 chars to control token budget.
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
        return [r[0][:1800] for r in rows]

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
) -> tuple[str, int]:
    """Build user message. Returns (prompt_text, token_estimate)."""
    parts = []

    # Few-shot block
    for i, body in enumerate(examples, 1):
        parts.append(f"## Example Email {i}\n{body}")

    if examples:
        parts.append("---")

    # Aggregate + strip metadata before serialising
    aggregated = aggregate_tasks(tasks)
    clean_summary = strip_llm_metadata(summary)

    logger.info(
        f"Task aggregation: {len(tasks)} raw rows → {len(aggregated)} grouped rows"
    )

    parts.append(f"## Data Payload — {supplier} ({frequency})")
    parts.append("### Summary Metrics")
    parts.append(json.dumps(clean_summary, indent=2, default=str))
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
        summary, tasks, examples, supplier, frequency
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

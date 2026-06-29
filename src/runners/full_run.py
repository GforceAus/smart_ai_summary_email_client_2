"""
src/runners/full_run.py
-----------------------
Generates and emails supplier summaries for all active suppliers.

Reads suppliers + frequencies from DuckDB, calls generate_email() for each,
then sends FROM support@gforceaus.com TO recipients in CRM_EMAIL.

Prints a timing/memory table at the end.
"""
import argparse
import csv
import io
import os
import re
import time
import tracemalloc
import resource
import logging
import duckdb
from dotenv import load_dotenv

from src.generators.email_generator import generate_email, get_token_usage
from src.cli.tasks import get_all_tasks_for_report
from src.utils.graph_email import send_email

load_dotenv()

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

DB_PATH       = "data/processed/supplier_map.duckdb"
CRM_EMAIL_RAW = os.environ.get("CRM_EMAIL", "")


def _parse_emails(raw: str) -> list[str]:
    return [e.strip().strip('"\'') for e in re.split(r'[,{}\s]+', raw) if "@" in e]


def _tasks_to_csv(tasks: list[dict]) -> str:
    cols = ["Task ID", "Date", "Store", "State", "Task", "Status", "Rep", "Question", "Answer", "Rep Comment", "Cannot Complete"]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=cols)
    writer.writeheader()
    for t in tasks:
        writer.writerow({
            "Task ID":         t.get("task_id", ""),
            "Date":            str(t.get("task_date", "")),
            "Store":           t.get("store_name", ""),
            "State":           t.get("state", ""),
            "Task":            t.get("task_name", ""),
            "Status":          t.get("task_status", ""),
            "Rep":             t.get("rep_name", "") or "",
            "Question":        t.get("question", "") or "",
            "Answer":          t.get("answer_from_rep", "") or "",
            "Rep Comment":     t.get("comments_from_rep", "") or "",
            "Cannot Complete": t.get("cannot_complete_comments", "") or "",
        })
    return buf.getvalue()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Generate emails but do not send")
    parser.add_argument("--supplier", help="Run a single supplier only")
    parser.add_argument("--frequency", choices=["weekly", "fortnightly", "monthly"], help="Frequency for single-supplier run")
    args = parser.parse_args()
    dry_run = args.dry_run

    if dry_run:
        logger.info("DRY RUN — emails will be generated but NOT sent")

    tracemalloc.start()
    t_total = time.time()

    if args.supplier:
        con = duckdb.connect(DB_PATH, read_only=True)
        row = con.execute(
            "SELECT supplier_name, frequency FROM reporting_frequency "
            "WHERE active = true AND supplier_name = ?",
            [args.supplier]
        ).fetchone()
        con.close()
        if not row:
            logger.error(f"Supplier '{args.supplier}' not found or not active in reporting_frequency")
            return
        freq = args.frequency or row[1]
        suppliers = [(row[0], freq)]
    else:
        con = duckdb.connect(DB_PATH, read_only=True)
        suppliers = con.execute(
            "SELECT supplier_name, frequency FROM reporting_frequency "
            "WHERE active = true ORDER BY frequency, supplier_name"
        ).fetchall()
        con.close()

    recipients = _parse_emails(CRM_EMAIL_RAW)
    logger.info(f"Recipients: {recipients}")
    logger.info(f"Running {len(suppliers)} suppliers...")

    results = []
    for supplier, frequency in suppliers:
        t0 = time.time()
        mem_before, _ = tracemalloc.get_traced_memory()
        status = "ok"

        try:
            body = generate_email(supplier, frequency)
            if not body:
                status = "no_data"
            elif dry_run:
                status = "generated"
            elif recipients:
                subject = f"[GForce] {supplier} {frequency} summary"
                tasks = get_all_tasks_for_report(supplier, frequency)
                attachment = None
                if tasks:
                    csv_content = _tasks_to_csv(tasks)
                    filename = f"{supplier}_{frequency}_tasks_{time.strftime('%Y-%m-%d')}.csv"
                    attachment = (filename, csv_content)
                send_email(recipients, subject, body, attachment=attachment)
                status = "sent"
            else:
                status = "generated"
        except Exception as e:
            status = f"ERR: {e}"
            logger.error(f"{supplier}: {e}")

        elapsed = time.time() - t0
        mem_after, _ = tracemalloc.get_traced_memory()
        mem_delta_mb = (mem_after - mem_before) / 1_048_576

        results.append({
            "supplier":  supplier,
            "frequency": frequency,
            "status":    status,
            "time_s":    round(elapsed, 1),
            "mem_mb":    round(mem_delta_mb, 2),
        })
        logger.info(f"{supplier:<40} {status:<12} {elapsed:.1f}s")

    total_s = time.time() - t_total
    peak_heap_mb = tracemalloc.get_traced_memory()[1] / 1_048_576
    peak_rss_mb  = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    tracemalloc.stop()

    # Summary table
    print(f"\n{'='*72}")
    print(f"FULL RUN COMPLETE — {len(suppliers)} suppliers in {total_s:.1f}s")
    print(f"Peak heap: {peak_heap_mb:.1f} MB | Peak RSS: {peak_rss_mb:.1f} MB")
    print(f"{'='*72}")
    print(f"{'Supplier':<38} {'Freq':<14} {'Status':<14} {'Time':>7}  {'Mem':>7}")
    print("-"*72)
    for r in results:
        print(
            f"{r['supplier']:<38} {r['frequency']:<14} {r['status']:<14} "
            f"{r['time_s']:>6.1f}s  {r['mem_mb']:>6.2f}MB"
        )
    print("-"*72)
    sent     = sum(1 for r in results if r["status"] == "sent")
    gen      = sum(1 for r in results if r["status"] == "generated")
    no_data  = sum(1 for r in results if r["status"] == "no_data")
    errors   = sum(1 for r in results if r["status"].startswith("ERR"))
    print(f"Sent: {sent}  Generated(no send): {gen}  No data: {no_data}  Errors: {errors}")

    # Gemini cost estimate (gemini-2.5-flash non-thinking pricing)
    usage = get_token_usage()
    input_cost  = usage["input"]  / 1_000_000 * 0.15
    output_cost = usage["output"] / 1_000_000 * 0.60
    total_cost  = input_cost + output_cost
    print(f"\nGemini usage: {usage['input']:,} input tokens / {usage['output']:,} output tokens")
    print(f"Estimated cost: ${input_cost:.4f} input + ${output_cost:.4f} output = ${total_cost:.4f} USD")
    print(f"{'='*72}\n")


if __name__ == "__main__":
    main()

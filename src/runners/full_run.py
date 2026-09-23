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
import datetime
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
from src.utils.markdown_email import render_email_html

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



def schedule_due(freq: str, today: datetime.date | None = None) -> tuple[bool, str]:
    """
    Is this cohort due today?

    weekly      — every Monday
    fortnightly — Mondays of even ISO weeks (FORTNIGHTLY_PARITY=odd to flip)
    monthly     — the first Monday of the month
    """
    today = today or datetime.date.today()
    iso_week = today.isocalendar()[1]
    is_monday = today.isoweekday() == 1
    parity = os.environ.get("FORTNIGHTLY_PARITY", "even")
    week_matches = (iso_week % 2 == 1) if parity == "odd" else (iso_week % 2 == 0)
    is_first_monday = is_monday and today.day <= 7

    if freq == "weekly":
        return (is_monday, "Monday" if is_monday else f"not Monday ({today:%A})")
    if freq == "fortnightly":
        if not is_monday:
            return (False, f"not Monday ({today:%A})")
        return (week_matches,
                f"ISO week {iso_week} is {'even' if iso_week % 2 == 0 else 'odd'}, parity={parity}")
    if freq == "monthly":
        if not is_monday:
            return (False, f"not Monday ({today:%A})")
        return (is_first_monday,
                "first Monday of the month" if is_first_monday
                else f"not the first Monday (day {today.day})")
    return (True, "no schedule gate")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Generate emails but do not send")
    parser.add_argument("--ignore-schedule", action="store_true",
                        help="Run even if this cohort is not due today")
    parser.add_argument("--supplier", help="Run a single supplier only")
    parser.add_argument("--frequency", choices=["weekly", "fortnightly", "monthly"], help="Frequency for single-supplier run")
    parser.add_argument("--run-for", choices=["weekly", "fortnightly", "monthly", "all"], default="all",
                        help="Run only suppliers of this reporting frequency")
    parser.add_argument("--check-only", action="store_true",
                        help="Print what would run without calling Gemini or sending email")
    args = parser.parse_args()
    dry_run = args.dry_run

    if dry_run:
        logger.info("DRY RUN — emails will be generated but NOT sent")

    # Schedule gates. These apply to real runs, not just --check-only: the
    # systemd timer is no longer the only thing deciding whether a cohort is
    # due, so a manual or mistimed invocation cannot send an off-schedule batch.
    # Use --ignore-schedule to override deliberately.
    if args.run_for in ("weekly", "fortnightly", "monthly") and not args.ignore_schedule:
        due, reason = schedule_due(args.run_for)
        if not due:
            logger.info(f"Skipping {args.run_for} run — {reason}")
            return
        logger.info(f"{args.run_for.capitalize()} run is due — {reason}")

    tracemalloc.start()
    t_total = time.time()

    crm_emails = _parse_emails(CRM_EMAIL_RAW)

    if args.supplier:
        con = duckdb.connect(DB_PATH, read_only=True)
        row = con.execute(
            "SELECT supplier_name, frequency, account_manager, COALESCE(show_completion_stats, true) FROM reporting_frequency "
            "WHERE active = true AND supplier_name = ?",
            [args.supplier]
        ).fetchone()
        con.close()
        if not row:
            logger.error(f"Supplier '{args.supplier}' not found or not active in reporting_frequency")
            return
        freq = args.frequency or row[1]
        suppliers = [(row[0], freq, row[2], row[3])]
    else:
        con = duckdb.connect(DB_PATH, read_only=True)
        if args.run_for == "all":
            suppliers = con.execute(
                "SELECT supplier_name, frequency, account_manager, COALESCE(show_completion_stats, true) FROM reporting_frequency "
                "WHERE active = true ORDER BY frequency, supplier_name"
            ).fetchall()
        else:
            suppliers = con.execute(
                "SELECT supplier_name, frequency, account_manager, COALESCE(show_completion_stats, true) FROM reporting_frequency "
                "WHERE active = true AND frequency = ? ORDER BY supplier_name",
                [args.run_for]
            ).fetchall()
        con.close()

    if args.check_only:
        today = datetime.date.today()
        iso_week = today.isocalendar()[1]
        parity = os.environ.get("FORTNIGHTLY_PARITY", "even")

        print(f"\n{'='*65}")
        print(f"SCHEDULE CHECK - {today}  ({today:%A}, ISO week {iso_week} "
              f"{'even' if iso_week % 2 == 0 else 'odd'}, fortnightly parity={parity})")
        print(f"{'='*65}")

        for freq in ["weekly", "fortnightly", "monthly"]:
            con = duckdb.connect(DB_PATH, read_only=True)
            rows = con.execute(
                "SELECT supplier_name, account_manager, COALESCE(show_completion_stats, true) "
                "FROM reporting_frequency WHERE active = true AND frequency = ? ORDER BY supplier_name",
                [freq]
            ).fetchall()
            con.close()
            # Same helper the real run uses, so the check cannot drift from it.
            due, reason = schedule_due(freq, today)
            label = "WOULD RUN" if due else f"SKIP - {reason}"
            print(f"\n{freq.upper()} - {len(rows)} suppliers - {label}")
            if due and args.run_for in ("all", freq):
                for name, manager, _show in rows:
                    print(f"  {name:<35} -> {manager or '(no manager)'}")

        print(f"\n{'='*65}\n")
        return

    logger.info(f"Running {len(suppliers)} suppliers...")

    results = []
    for supplier, frequency, account_manager, show_completion in suppliers:
        t0 = time.time()
        mem_before, _ = tracemalloc.get_traced_memory()
        status = "ok"

        # Build recipient list: account manager first, then CRM (deduped)
        recipients: list[str] = []
        if account_manager:
            recipients.append(account_manager)
        for e in crm_emails:
            if e not in recipients:
                recipients.append(e)

        try:
            body = generate_email(supplier, frequency, show_completion=show_completion)
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
                # The generator emits Markdown; mail clients show it literally,
                # so render to HTML before sending.
                send_email(
                    recipients, subject, render_email_html(body),
                    attachment=attachment, html=True,
                )
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
            "manager":   account_manager or "",
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

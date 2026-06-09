"""CLI tool to fetch detailed supplier tasks from PostgreSQL."""

import argparse
import json
import sys
from src.database.connection import PostgreSQLConnection


def get_tasks(supplier: str, frequency: str) -> list:
    """Fetch tasks from Postgres based on supplier and frequency."""

    interval_map = {
        "weekly":      "7 days",
        "fortnightly": "14 days",
        "monthly":     "1 month",
    }

    if frequency not in interval_map:
        print(f"Error: Invalid frequency '{frequency}'. Use weekly, fortnightly, or monthly.")
        sys.exit(1)

    interval = interval_map[frequency]

    try:
        with PostgreSQLConnection() as conn:
            with conn.cursor() as cur:
                print(f"Fetching {frequency} tasks for {supplier}...")

                cur.execute("SET app.supplier_name = %s;", (supplier,))

                cur.execute(
                    f"SELECT (CURRENT_DATE)::text, (CURRENT_DATE - INTERVAL '{interval}')::text"
                )
                date_to, date_from = cur.fetchone()

                cur.execute("SELECT set_config('app.date_to', %s, false)", (date_to,))
                cur.execute("SELECT set_config('app.date_from', %s, false)", (date_from,))

                cur.execute("SELECT tasks FROM field_ops.v_supplier_email_summary;")
                row = cur.fetchone()
                return row[0] if row else []

    except Exception as e:
        print(f"Error connecting to database: {e}")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Fetch detailed supplier tasks")
    parser.add_argument("--supplier",  required=True, help="Supplier name (e.g. OSRAM)")
    parser.add_argument("--frequency", choices=["weekly", "fortnightly", "monthly"],
                        default="weekly")
    parser.add_argument("--output",    help="Output JSON file path")
    parser.add_argument("--limit",     type=int, default=10,
                        help="Number of tasks to display in table")

    args = parser.parse_args()
    tasks = get_tasks(args.supplier, args.frequency)

    if not tasks:
        print(f"No tasks found for {args.supplier} in the last {args.frequency} period.")
        return

    if args.output:
        with open(args.output, "w") as f:
            json.dump(tasks, f, indent=2)
        print(f"Tasks saved to {args.output}")
        return

    print(f"\n=== TASKS: {args.supplier} ({args.frequency}) ===")

    # task_uuid = unique per-store row identifier (t.id UUID)
    # task_id   = template identifier shared across stores (T-XXXXX)
    header = f"{'UUID':<38} | {'TASK_ID':<10} | {'DATE':<12} | {'SCORE':<6} | {'STORE':<22} | TASK NAME"
    print(header)
    print("-" * len(header))

    for task in tasks[:args.limit]:
        uuid   = str(task.get("task_uuid") or "N/A")[:36]
        t_id   = str(task.get("task_id")   or "N/A")[:10]
        date   = str(task.get("date",      "N/A"))
        score  = str(task.get("score",     "0"))
        store  = str(task.get("store",     "N/A"))[:22]
        name   = str(task.get("task",      "N/A"))
        print(f"{uuid:<38} | {t_id:<10} | {date:<12} | {score:<6} | {store:<22} | {name}")

    if len(tasks) > args.limit:
        print(f"\n... and {len(tasks) - args.limit} more tasks. Use --limit to show more.")


if __name__ == "__main__":
    main()



# """CLI tool to fetch detailed supplier tasks from PostgreSQL."""
#
# import argparse
# import json
# import sys
# from src.database.connection import PostgreSQLConnection
#
# def get_tasks(supplier: str, frequency: str):
#     """Fetch tasks from Postgres based on supplier and frequency."""
#
#     interval_map = {
#         "weekly": "7 days",
#         "fortnightly": "14 days",
#         "monthly": "1 month"
#     }
#
#     if frequency not in interval_map:
#         print(f"Error: Invalid frequency '{frequency}'. Use weekly, fortnightly, or monthly.")
#         sys.exit(1)
#
#     interval = interval_map[frequency]
#
#     try:
#         with PostgreSQLConnection() as conn:
#             with conn.cursor() as cur:
#                 print(f"Fetching {frequency} tasks for {supplier}...")
#
#                 cur.execute("SET app.supplier_name = %s;", (supplier,))
#
#                 cur.execute(f"SELECT (CURRENT_DATE)::text, (CURRENT_DATE - INTERVAL '{interval}')::text")
#                 date_to, date_from = cur.fetchone()
#
#                 cur.execute("SELECT set_config('app.date_to', %s, false)", (date_to,))
#                 cur.execute("SELECT set_config('app.date_from', %s, false)", (date_from,))
#
#                 cur.execute("SELECT tasks FROM field_ops.v_supplier_email_summary;")
#                 row = cur.fetchone()
#
#                 return row[0] if row else []
#
#     except Exception as e:
#         print(f"Error connecting to database: {e}")
#         sys.exit(1)
#
# def main():
#     parser = argparse.ArgumentParser(description="Fetch detailed supplier tasks")
#     parser.add_argument("--supplier", required=True, help="Supplier name")
#     parser.add_argument("--frequency", choices=["weekly", "fortnightly", "monthly"],
#                         default="weekly", help="Summary frequency")
#     parser.add_argument("--output", help="Output JSON file path")
#     parser.add_argument("--limit", type=int, default=10, help="Number of tasks to display")
#
#     args = parser.parse_args()
#
#     tasks = get_tasks(args.supplier, args.frequency)
#
#     if tasks:
#         if args.output:
#             with open(args.output, 'w') as f:
#                 json.dump(tasks, f, indent=2)
#             print(f"Tasks saved to {args.output}")
#         else:
#             print(f"\n=== TASKS: {args.supplier} ({args.frequency}) ===")
#
#             # Table Header
#             header = f"{'ID':<10} | {'DATE':<12} | {'STORE':<20} | {'TASK NAME'}"
#             print(header)
#             print("-" * len(header))
#
#             for task in tasks[:args.limit]:
#                 # The view uses 'id' for task_id if updated, otherwise fallback
#                 t_id = str(task.get('id') or task.get('task_id') or "N/A")
#                 t_date = str(task.get('date', "N/A"))
#                 t_store = str(task.get('store', "N/A"))[:20]
#                 t_name = str(task.get('task', "N/A"))
#
#                 print(f"{t_id:<10} | {t_date:<12} | {t_store:<20} | {t_name}")
#
#             if len(tasks) > args.limit:
#                 print(f"\n... and {len(tasks) - args.limit} more tasks. Use --limit to show more.")
#
#             print("\nNote: If 'ID' shows N/A, please update the database view to include 'capped.task_id'.")
#     else:
#         print(f"No tasks found for {args.supplier} in the last {args.frequency} period.")
#
# if __name__ == "__main__":
#     main()

"""CLI tool to fetch supplier email summaries from PostgreSQL."""

import argparse
import json
import sys
from datetime import datetime
from src.database.connection import PostgreSQLConnection

def get_summary(supplier: str, frequency: str):
    """Fetch summary from Postgres based on supplier and frequency."""
    
    # Map frequency to Postgres intervals
    interval_map = {
        "weekly": "7 days",
        "fortnightly": "14 days",
        "monthly": "1 month"
    }
    
    if frequency not in interval_map:
        print(f"Error: Invalid frequency '{frequency}'. Use weekly, fortnightly, or monthly.")
        sys.exit(1)
        
    interval = interval_map[frequency]
    
    try:
        with PostgreSQLConnection() as conn:
            with conn.cursor() as cur:
                # Set session parameters
                print(f"Fetching {frequency} summary for {supplier}...")
                
                cur.execute("SET app.supplier_name = %s;", (supplier,))
                
                # Execute a query to get the dates and set them
                cur.execute(f"SELECT (CURRENT_DATE)::text, (CURRENT_DATE - INTERVAL '{interval}')::text")
                date_to, date_from = cur.fetchone()
                
                cur.execute("SELECT set_config('app.date_to', %s, false)", (date_to,))
                cur.execute("SELECT set_config('app.date_from', %s, false)", (date_from,))
                
                # Fetch summary and tasks
                cur.execute("SELECT summary, tasks FROM field_ops.v_supplier_email_summary;")
                row = cur.fetchone()
                
                if row:
                    summary, tasks = row
                    return {
                        "summary": summary,
                        "tasks": tasks
                    }
                else:
                    return None
                    
    except Exception as e:
        print(f"Error connecting to database: {e}")
        sys.exit(1)

def main():
    parser = argparse.ArgumentParser(description="Fetch supplier email summaries")
    parser.add_argument("--supplier", required=True, help="Supplier name (e.g., NULON)")
    parser.add_argument("--frequency", choices=["weekly", "fortnightly", "monthly"], 
                        default="weekly", help="Summary frequency")
    parser.add_argument("--output", help="Output JSON file path")
    
    args = parser.parse_args()
    
    result = get_summary(args.supplier, args.frequency)
    
    if result:
        if args.output:
            with open(args.output, 'w') as f:
                json.dump(result, f, indent=2)
            print(f"Summary saved to {args.output}")
        else:
            print("\n=== SUMMARY ===")
            print(json.dumps(result["summary"], indent=2))
            print(f"\nTasks returned: {len(result['tasks'])}")
    else:
        print(f"No data found for {args.supplier} in the last {args.frequency} period.")

if __name__ == "__main__":
    main()

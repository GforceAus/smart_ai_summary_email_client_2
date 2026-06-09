import json
from src.database.connection import PostgreSQLConnection

def run_query():
    try:
        with PostgreSQLConnection() as conn:
            with conn.cursor() as cur:
                print("Setting ALL session parameters...")
                cur.execute("SET app.supplier_name = 'NULON';")
                cur.execute("SET app.date_from = '2026-05-01';")
                cur.execute("SET app.date_to = '2026-05-31';")
                
                print("Executing SELECT summary, tasks FROM field_ops.v_supplier_email_summary...")
                cur.execute("SELECT summary, tasks FROM field_ops.v_supplier_email_summary;")
                
                row = cur.fetchone()
                if row:
                    summary, tasks = row
                    print("\n--- SUMMARY ---")
                    print(json.dumps(summary, indent=2))
                    print(f"\nTotal tasks in JSON: {len(tasks)}")
                else:
                    print("No results found.")
    except Exception as e:
        print(f"Error executing query: {e}")

if __name__ == "__main__":
    run_query()

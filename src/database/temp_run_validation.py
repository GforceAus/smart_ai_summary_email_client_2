import json
from src.database.connection import PostgreSQLConnection

def run_validation():
    try:
        with PostgreSQLConnection() as conn:
            with conn.cursor() as cur:
                print("Setting session parameters for ACCENT-TOOLS-NZ...")
                cur.execute("SET app.supplier_name = 'ACCENT-TOOLS-NZ';")
                cur.execute("SET app.date_from = '2026-05-01';")
                cur.execute("SET app.date_to = '2026-05-31';")
                
                print("Executing Validation Query...")
                query = """
                SELECT
                    summary->>'total_tasks'        AS total,
                    summary->>'completion_pct'     AS pct,
                    summary->>'stores_with_issues' AS issue_stores,
                    jsonb_array_length(tasks)      AS exception_row_count,
                    length(summary::text) + length(tasks::text) AS approx_chars,
                    (length(summary::text) + length(tasks::text)) / 4 AS approx_tokens
                FROM field_ops.v_supplier_email_summary;
                """
                cur.execute(query)
                
                row = cur.fetchone()
                if row:
                    cols = [desc[0] for desc in cur.description]
                    result = dict(zip(cols, row))
                    print("\n--- VALIDATION RESULT ---")
                    print(json.dumps(result, indent=2))
                else:
                    print("No results found.")
    except Exception as e:
        print(f"Error executing validation query: {e}")

if __name__ == "__main__":
    run_validation()

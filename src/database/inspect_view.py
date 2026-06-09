import json
from src.database.connection import PostgreSQLConnection

def inspect():
    try:
        with PostgreSQLConnection() as conn:
            with conn.cursor() as cur:
                # 1. Get View Definition
                print("\n=== 1. View Definition ===")
                cur.execute("SELECT pg_get_viewdef('field_ops.v_supplier_email_summary', true);")
                view_def = cur.fetchone()
                if view_def:
                    print(view_def[0])
                else:
                    print("View not found.")

                # 2. Inspect a Sample Row
                print("\n=== 2. Sample Row (NULON) ===")
                cur.execute("SET app.supplier_name = 'NULON';")
                cur.execute("SET app.date_from = '2026-05-01';")
                cur.execute("SET app.date_to = '2026-06-01';")
                
                cur.execute("SELECT tasks->0 as first_task FROM field_ops.v_supplier_email_summary LIMIT 1;")
                row = cur.fetchone()
                if row:
                    print(json.dumps(row[0], indent=2))
                else:
                    print("No sample row found.")

                # 3. Check View Columns
                print("\n=== 3. View Columns ===")
                cur.execute("""
                    SELECT column_name, data_type
                    FROM information_schema.columns
                    WHERE table_schema = 'field_ops'
                      AND table_name = 'v_supplier_email_summary'
                    ORDER BY ordinal_position;
                """)
                columns = cur.fetchall()
                for col_name, data_type in columns:
                    print(f"{col_name:20} | {data_type}")

    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    inspect()

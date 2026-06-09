from src.database.connection import PostgreSQLConnection
from pathlib import Path

def apply_view():
    sql_path = Path("src/database/views/v_supplier_email_summary.sql")
    if not sql_path.exists():
        print(f"Error: {sql_path} not found.")
        return

    with open(sql_path, 'r') as f:
        sql = f.read()

    try:
        with PostgreSQLConnection() as conn:
            with conn.cursor() as cur:
                print(f"Applying view from {sql_path}...")
                cur.execute(sql)
                conn.commit()
                print("View applied successfully.")
    except Exception as e:
        print(f"Error applying view: {e}")

if __name__ == "__main__":
    apply_view()

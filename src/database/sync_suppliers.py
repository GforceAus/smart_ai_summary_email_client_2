"""Synchronize supplier data from PostgreSQL to local DuckDB."""

import os
import duckdb
from pathlib import Path
from dotenv import load_dotenv
from src.database.connection import PostgreSQLConnection

load_dotenv()

def sync_suppliers():
    """Fetch suppliers and clients from Postgres and save to DuckDB."""
    db_path = Path("data/processed/supplier_map.duckdb")
    db_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"Connecting to PostgreSQL...")
    with PostgreSQLConnection() as pg_conn:
        with pg_conn.cursor() as cursor:
            # Fetch clients
            print("Fetching public.clients...")
            cursor.execute("SELECT client_id, active, supplier_name, gfm_username, comission_based FROM public.clients")
            clients_data = cursor.fetchall()
            clients_cols = [desc[0] for desc in cursor.description]

            # Fetch suppliers
            print("Fetching field_ops.suppliers...")
            cursor.execute("""
                SELECT user_id, short_name, full_company_name, status, email, business_email, 
                       address, abn_no, state, country, contract_type, created_at, updated_at
                FROM field_ops.suppliers
            """)
            suppliers_data = cursor.fetchall()
            suppliers_cols = [desc[0] for desc in cursor.description]

    print(f"Connecting to DuckDB at {db_path}...")
    duck_conn = duckdb.connect(str(db_path))

    try:
        # Create and populate clients table
        print("Updating clients table in DuckDB...")
        duck_conn.execute("DROP TABLE IF EXISTS clients")
        # DuckDB can create table from python objects
        # We'll use a simple approach: create table then insert
        
        # Define types (simplified)
        client_types = ", ".join([f"{col} VARCHAR" for col in clients_cols])
        duck_conn.execute(f"CREATE TABLE clients ({client_types})")
        duck_conn.executemany(f"INSERT INTO clients VALUES ({','.join(['?'] * len(clients_cols))})", clients_data)

        # Create and populate suppliers table
        print("Updating suppliers table in DuckDB...")
        duck_conn.execute("DROP TABLE IF EXISTS suppliers")
        supplier_types = ", ".join([f"{col} VARCHAR" for col in suppliers_cols])
        duck_conn.execute(f"CREATE TABLE suppliers ({supplier_types})")
        duck_conn.executemany(f"INSERT INTO suppliers VALUES ({','.join(['?'] * len(suppliers_cols))})", suppliers_data)

        # Auto-register new suppliers into reporting_frequency (inactive by default)
        print("Checking for new suppliers to register...")
        duck_conn.execute("""
            CREATE TABLE IF NOT EXISTS reporting_frequency (
                id         INTEGER PRIMARY KEY,
                client_id  VARCHAR,
                supplier_name VARCHAR NOT NULL,
                frequency  VARCHAR NOT NULL DEFAULT 'monthly',
                active     BOOLEAN DEFAULT false,
                notes      VARCHAR,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        new_suppliers = duck_conn.execute("""
            SELECT s.short_name
            FROM suppliers s
            LEFT JOIN reporting_frequency rf ON rf.supplier_name = s.short_name
            WHERE rf.supplier_name IS NULL
              AND s.status = 'active'
              AND s.short_name IS NOT NULL
        """).fetchall()

        for (name,) in new_suppliers:
            duck_conn.execute("""
                INSERT INTO reporting_frequency (supplier_name, frequency, active, notes)
                VALUES (?, 'monthly', false, 'auto-registered — activate when ready')
            """, [name])
            print(f"  + Registered new supplier: {name} (inactive, monthly)")

        if not new_suppliers:
            print("  No new suppliers found.")

        print("Sync complete.")
    finally:
        duck_conn.close()

if __name__ == "__main__":
    sync_suppliers()

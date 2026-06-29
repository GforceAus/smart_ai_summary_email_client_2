"""Client management CLI for reporting_frequency table."""

import argparse
import duckdb

DB_PATH = "data/processed/supplier_map.duckdb"


def list_clients():
    con = duckdb.connect(DB_PATH, read_only=True)
    rows = con.execute("""
        SELECT supplier_name, frequency, active, notes
        FROM reporting_frequency
        ORDER BY active DESC, frequency, supplier_name
    """).fetchall()
    con.close()

    print(f"{'SUPPLIER':<35} {'FREQ':<14} {'ACTIVE'}")
    print("-" * 60)
    for name, freq, active, notes in rows:
        marker = "✓" if active else "✗"
        note = f"  [{notes}]" if notes else ""
        print(f"{name:<35} {freq:<14} {marker}{note}")
    print(f"\nTotal: {len(rows)}  Active: {sum(1 for r in rows if r[2])}")


def add_client(supplier: str, frequency: str):
    con = duckdb.connect(DB_PATH)
    existing = con.execute(
        "SELECT active FROM reporting_frequency WHERE supplier_name = ?", [supplier]
    ).fetchone()
    if existing:
        print(f"{supplier} already exists (active={existing[0]}). Use set-frequency or activate instead.")
    else:
        con.execute(
            "INSERT INTO reporting_frequency (supplier_name, frequency, active) VALUES (?, ?, true)",
            [supplier, frequency],
        )
        print(f"✓ Added {supplier} as {frequency} (active)")
    con.close()


def set_frequency(supplier: str, frequency: str):
    con = duckdb.connect(DB_PATH)
    con.execute(
        "UPDATE reporting_frequency SET frequency = ?, updated_at = CURRENT_TIMESTAMP WHERE supplier_name = ?",
        [frequency, supplier],
    )
    rows = con.execute(
        "SELECT frequency FROM reporting_frequency WHERE supplier_name = ?", [supplier]
    ).fetchone()
    con.close()
    if rows:
        print(f"✓ {supplier} frequency set to {frequency}")
    else:
        print(f"✗ Supplier '{supplier}' not found. Use: just add_client {supplier} {frequency}")


def set_manager(supplier: str, email: str):
    con = duckdb.connect(DB_PATH)
    con.execute(
        "UPDATE reporting_frequency SET account_manager = ?, updated_at = CURRENT_TIMESTAMP WHERE supplier_name = ?",
        [email, supplier],
    )
    exists = con.execute(
        "SELECT 1 FROM reporting_frequency WHERE supplier_name = ?", [supplier]
    ).fetchone()
    con.close()
    print(f"✓ {supplier} account manager set to {email}" if exists else f"✗ '{supplier}' not found")


def set_active(supplier: str, active: bool):
    con = duckdb.connect(DB_PATH)
    con.execute(
        "UPDATE reporting_frequency SET active = ?, updated_at = CURRENT_TIMESTAMP WHERE supplier_name = ?",
        [active, supplier],
    )
    exists = con.execute(
        "SELECT 1 FROM reporting_frequency WHERE supplier_name = ?", [supplier]
    ).fetchone()
    con.close()
    verb = "activated" if active else "deactivated"
    print(f"✓ {supplier} {verb}" if exists else f"✗ '{supplier}' not found")


def main():
    parser = argparse.ArgumentParser(description="Manage reporting_frequency clients")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="List all clients")

    p = sub.add_parser("add", help="Add a new client")
    p.add_argument("supplier")
    p.add_argument("frequency", choices=["weekly", "fortnightly", "monthly"], default="monthly", nargs="?")

    p = sub.add_parser("set-frequency", help="Change frequency for a client")
    p.add_argument("supplier")
    p.add_argument("frequency", choices=["weekly", "fortnightly", "monthly"])

    p = sub.add_parser("set-manager", help="Set account manager email for a client")
    p.add_argument("supplier")
    p.add_argument("email")

    p = sub.add_parser("activate", help="Start sending emails to a client")
    p.add_argument("supplier")

    p = sub.add_parser("deactivate", help="Stop sending emails to a client")
    p.add_argument("supplier")

    args = parser.parse_args()

    if args.cmd == "list":
        list_clients()
    elif args.cmd == "add":
        add_client(args.supplier, args.frequency)
    elif args.cmd == "set-frequency":
        set_frequency(args.supplier, args.frequency)
    elif args.cmd == "set-manager":
        set_manager(args.supplier, args.email)
    elif args.cmd == "activate":
        set_active(args.supplier, True)
    elif args.cmd == "deactivate":
        set_active(args.supplier, False)


if __name__ == "__main__":
    main()

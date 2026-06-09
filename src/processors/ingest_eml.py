#!/usr/bin/env python3
"""
ingest_eml.py
-------------
Parse approved .eml files and insert into reporting.email_examples.

Usage:
    uv run ingest_eml.py --dir ./emails/
    uv run ingest_eml.py --file ./emails/january_nulon.eml
    uv run ingest_eml.py --dir ./emails/ --dry-run

Schema required (run once):
    CREATE TABLE IF NOT EXISTS reporting.email_examples (
        id              SERIAL PRIMARY KEY,
        supplier_name   TEXT NOT NULL,
        date_from       DATE,
        date_to         DATE,
        email_body      TEXT NOT NULL,
        dropbox_links   JSONB,
        subject         TEXT,
        sent_by         TEXT,
        sent_to         TEXT,
        sent_at         TIMESTAMPTZ,
        source          TEXT DEFAULT 'manual',
        rating          SMALLINT DEFAULT 3,
        body_hash       TEXT UNIQUE,
        token_estimate  INTEGER,
        created_at      TIMESTAMPTZ DEFAULT NOW()
    );
"""

import argparse
import email
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import duckdb
from dotenv import load_dotenv

load_dotenv()

# ── Supplier detection ──────────────────────────────────────────────────────
# Extend this dict as you add more suppliers.
# Key = substring to match in Subject or To field (case-insensitive)
# Value = canonical supplier name matching field_ops.tasks.supplier_name
SUPPLIER_MAP = {
    "nulon":    "NULON",
    "osram":    "OSRAM", 
    "ring":     "RING",
    "kincrome": "KINCROME",
    "sheffield":"SHEFFIELD",
    "3m":       "3M",
    "selleys":  "SELLEYS",
    "stanley":  "STANLEY",
    "dyson":    "DYSON",
    "philips":  "PHILIPS",
    "acol":     "ACOL",
    "ahe":      "AHE",
    "qep":      "QEP", 
    "timepet":  "TIMEPET",
    "galintel": "GALINTEL",
    "dindas":   "DINDAS",
}

from email.header import decode_header


def decode_mime_header(s: str) -> str:
    if not s:
        return ""
    parts = decode_header(s)
    decoded_parts = []
    for content, charset in parts:
        if isinstance(content, bytes):
            decoded_parts.append(content.decode(charset or 'utf-8', errors='replace'))
        else:
            decoded_parts.append(content)
    return "".join(decoded_parts)


def get_suppliers_from_db() -> dict[str, tuple[str, str]]:
    """Fetch supplier mappings from local DuckDB.
    
    Returns:
        Dict mapping lowercase supplier names/keywords to tuples of (canonical_name, client_id).
    """
    db_path = Path("config/supplier_map.duckdb")
    if not db_path.exists():
        print(f"  [WARNING] Supplier map DB not found at {db_path}. Using fallback.")
        return {k: (v, None) for k, v in SUPPLIER_MAP.items()}
    
    try:
        conn = duckdb.connect(str(db_path))
        # Fetch active clients
        rows = conn.execute("SELECT supplier_name, client_id FROM clients WHERE active = 'true'").fetchall()
        
        # Build mapping: keyword -> (canonical, id)
        mapping = {}
        for name, client_id in rows:
            if name:
                # Add the full name as a keyword
                mapping[name.lower()] = (name, client_id)
                # If name has spaces or hyphens, also add the first part as a keyword (e.g. "AHE" from "AHE-...")
                parts = re.split(r'[\s\-]', name)
                if parts and len(parts[0]) > 2: # Only if first part is at least 3 chars
                    first_part = parts[0].lower()
                    if first_part not in mapping:
                        mapping[first_part] = (name, client_id)
                
                # Also add client_id as a keyword
                if client_id:
                    mapping[client_id.lower()] = (name, client_id)
        
        # Also add hardcoded ones if not present
        for k, v in SUPPLIER_MAP.items():
            if k not in mapping:
                mapping[k] = (v, None)
                
        conn.close()
        return mapping
    except Exception as e:
        print(f"  [ERROR] Failed to load supplier map from DB: {e}")
        return {k: (v, None) for k, v in SUPPLIER_MAP.items()}

SIGNATURE_CUTOFFS = [
    r'^Regards,', r'^Kind regards,', r'^Thanks,', r'^Thank you,',
    r'^Cheers,', r'^Best regards,', r'^Warm regards,', r'^Best,'
]

OPENER_PATTERN = re.compile(
    r'^(Hi|Dear|Hello|Good morning|Good afternoon)\s+[\w\s]+[,.]?\s*(\r?\n)+',
    re.IGNORECASE | re.MULTILINE
)

DISCLAIMER_PATTERN = re.compile(
    r'G-Force Category Solutions Disclaimer:.*$',
    re.DOTALL
)

RECONCILIATION_PATTERN = re.compile(
    r'In the spirit of reconciliation.*?today\.',
    re.DOTALL
)

DROPBOX_PATTERN = re.compile(
    r'([\w\s\-/&]+(?:BEFORE|AFTER|PHOTO|LINK|PHOTO LINK)[^\n<]*?)\s*'
    r'<(https?://www\.dropbox\.com/[^\s>]+)>',
    re.IGNORECASE
)

INLINE_URL_PATTERN = re.compile(r'<https?://[^\s>]+>')

CONTACT_BLOCK_PATTERN = re.compile(
    r'\+\s*61\s*\d[\d\s]+\|.*$',
    re.MULTILINE | re.DOTALL
)


def detect_supplier(subject: str, to_field: str, supplier_map: dict[str, tuple[str, str]] = None) -> tuple[str, str] | None:
    """Detect supplier from subject and to_field using database or fallback mapping.
    
    Returns:
        Tuple of (supplier_name, supplier_id) if found, None otherwise.
    """
    if supplier_map is None:
        supplier_map = get_suppliers_from_db()
    
    combined = f"{subject} {to_field}".lower()
    
    # Sort keys by length descending to match more specific ones first (e.g., "bedford-nz" before "bedford")
    sorted_keys = sorted(supplier_map.keys(), key=len, reverse=True)
    
    for key in sorted_keys:
        # Use lookbehind and lookahead to ensure keyword is not part of another alphanumeric word
        # This is more robust than \b for cases like "3M" or names with special chars
        pattern = rf'(?<![a-zA-Z0-9]){re.escape(key)}(?![a-zA-Z0-9])'
        if re.search(pattern, combined):
            return supplier_map[key]
            
    return None


def strip_signature(text: str) -> str:
    lines = text.split('\r\n') if '\r\n' in text else text.split('\n')
    for i, line in enumerate(lines):
        for pattern in SIGNATURE_CUTOFFS:
            if re.match(pattern, line.strip(), re.IGNORECASE):
                return '\n'.join(lines[:i]).strip()
    return text.strip()


def extract_dropbox_links(text: str) -> list[dict]:
    links = []
    for label, url in DROPBOX_PATTERN.findall(text):
        links.append({"label": label.strip(), "url": url.strip()})
    return links


def clean_body(text: str) -> str:
    # Strip signature block
    text = strip_signature(text)
    # Strip opener line
    text = OPENER_PATTERN.sub('', text, count=1).strip()
    # Strip inline URLs (keep label text)
    text = INLINE_URL_PATTERN.sub('', text)
    # Strip disclaimer
    text = DISCLAIMER_PATTERN.sub('', text)
    # Strip reconciliation footer
    text = RECONCILIATION_PATTERN.sub('', text)
    # Strip phone/contact block
    text = CONTACT_BLOCK_PATTERN.sub('', text)
    # Remove lines that are only whitespace or label orphans like "01-26 RECURRING AFTER PHOTO"
    # but keep actual content lines with asterisks (bullet points)
    # Collapse 3+ blank lines to 2
    sep = '\r\n' if '\r\n' in text else '\n'
    text = re.sub(r'(\r?\n){3,}', sep * 2, text)
    # Strip trailing "If there's anything..." filler closing line
    text = re.sub(
        r"If there'?s anything you'?d like to discuss.*?$",
        '',
        text,
        flags=re.DOTALL | re.IGNORECASE
    ).strip()
    return text.strip()


def token_estimate(text: str) -> int:
    return len(text) // 4


def body_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def parse_eml(path: Path) -> dict | None:
    with open(path, 'rb') as f:
        msg = email.message_from_bytes(f.read())

    subject   = decode_mime_header(msg.get('Subject', ''))
    from_addr = msg.get('From', '')
    to_addr   = msg.get('To', '')
    date_str  = msg.get('Date', '')

    # Parse date
    sent_at = None
    if date_str:
        try:
            sent_at = parsedate_to_datetime(date_str)
        except Exception:
            pass

    # Detect supplier
    supplier_info = detect_supplier(subject, to_addr)
    supplier_name = supplier_info[0] if supplier_info else None
    supplier_id = supplier_info[1] if supplier_info else None

    # Extract plain text part (prefer over HTML)
    plain_text = None
    for part in msg.walk():
        if part.get_content_type() == 'text/plain' and plain_text is None:
            raw = part.get_payload(decode=True)
            charset = part.get_content_charset() or 'utf-8'
            plain_text = raw.decode(charset, errors='replace')

    if not plain_text:
        print(f"  [SKIP] No plain text part: {path.name}")
        return None

    dropbox_links = extract_dropbox_links(plain_text)
    cleaned = clean_body(plain_text)

    if len(cleaned) < 200:
        print(f"  [SKIP] Body too short after cleaning ({len(cleaned)} chars): {path.name}")
        return None

    return {
        "supplier_name":  supplier_name,
        "supplier_id":    supplier_id,
        "subject":        subject,
        "sent_by":        from_addr,
        "sent_to":        to_addr,
        "sent_at":        sent_at,
        "email_body":     cleaned,
        "dropbox_links":  dropbox_links,
        "body_hash":      body_hash(cleaned),
        "token_estimate": token_estimate(cleaned),
        "source":         "manual",
        "rating":         3,
    }


def insert_record(conn, record: dict, dry_run: bool = False) -> str:
    """Returns 'inserted', 'skipped' (duplicate), or 'no_supplier'."""

    if record["supplier_name"] is None:
        return "no_supplier"

    if dry_run:
        return "dry_run"

    try:
        # Get next ID
        next_id_result = conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM email_examples").fetchone()
        next_id = next_id_result[0]
        
        conn.execute(
            """
            INSERT INTO email_examples
                (id, supplier_name, supplier_id, subject, sent_by, sent_to, sent_at,
                 email_body, dropbox_links, body_hash, token_estimate, source, rating)
            VALUES
                ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
            """,
            [next_id, record["supplier_name"], record["supplier_id"], record["subject"], 
             record["sent_by"], record["sent_to"], record["sent_at"],
             record["email_body"], record["dropbox_links"], 
             record["body_hash"], record["token_estimate"], record["source"], record["rating"]]
        )
        return "inserted"
    except Exception as e:
        if "Constraint Error: UNIQUE constraint failed" in str(e) or "duplicate" in str(e).lower():
            return "skipped"
        else:
            raise e


def create_table_if_missing(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS email_examples (
            id              INTEGER PRIMARY KEY,
            supplier_name   VARCHAR NOT NULL,
            supplier_id     VARCHAR,
            subject         VARCHAR,
            sent_by         VARCHAR,
            sent_to         VARCHAR,
            sent_at         TIMESTAMP,
            email_body      VARCHAR NOT NULL,
            dropbox_links   JSON,
            source          VARCHAR DEFAULT 'manual',
            rating          INTEGER DEFAULT 3,
            body_hash       VARCHAR UNIQUE,
            token_estimate  INTEGER,
            created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_email_examples_supplier
            ON email_examples(supplier_name);
    """)
    print("  [DB] Table email_examples ready in DuckDB.")


def main():
    parser = argparse.ArgumentParser(description="Ingest .eml files into DuckDB email_examples table")
    parser.add_argument('--dir',  type=Path, help="Directory of .eml files")
    parser.add_argument('--file', type=Path, help="Single .eml file")
    parser.add_argument('--dry-run', action='store_true', help="Parse only, no DB writes")
    args = parser.parse_args()

    if not args.dir and not args.file:
        print("Error: provide --dir or --file")
        sys.exit(1)

    # Collect files
    files: list[Path] = []
    if args.file:
        files = [args.file]
    elif args.dir:
        files = sorted(args.dir.glob('*.eml'))
        if not files:
            print(f"No .eml files found in {args.dir}")
            sys.exit(1)

    # DB connection
    conn = None
    if not args.dry_run:
        try:
            # Ensure data/processed directory exists
            db_dir = Path("data/processed")
            db_dir.mkdir(parents=True, exist_ok=True)
            
            # Connect to DuckDB
            db_path = db_dir / "training_approved_emails.duckdb"
            conn = duckdb.connect(str(db_path))
            create_table_if_missing(conn)
        except Exception as e:
            print(f"Error: Could not connect to database: {e}")
            sys.exit(1)

    # Process
    counts = {"inserted": 0, "skipped": 0, "no_supplier": 0, "dry_run": 0, "error": 0}

    for path in files:
        print(f"\nProcessing: {path.name}")
        try:
            record = parse_eml(path)
            if record is None:
                counts["error"] += 1
                continue

            status = insert_record(conn, record, dry_run=args.dry_run)
            counts[status] += 1

            print(f"  Subject:        {record['subject']}")
            print(f"  Supplier:       {record['supplier_name'] or '⚠ NOT DETECTED'}")
            print(f"  Supplier ID:    {record['supplier_id'] or 'N/A'}")
            print(f"  Sent by:        {record['sent_by']}")
            print(f"  Sent at:        {record['sent_at']}")
            print(f"  Dropbox links:  {len(record['dropbox_links'])}")
            print(f"  Cleaned chars:  {len(record['email_body'])}")
            print(f"  Token estimate: ~{record['token_estimate']}")
            print(f"  Hash:           {record['body_hash']}")
            print(f"  Status:         [{status.upper()}]")

            if record["supplier_name"] is None:
                print(f"  ⚠ Add supplier keyword to SUPPLIER_MAP to auto-detect.")

        except Exception as e:
            print(f"  [ERROR] {e}")
            counts["error"] += 1

    if conn:
        conn.close()

    print(f"\n{'='*50}")
    print(f"SUMMARY: {len(files)} files processed")
    for k, v in counts.items():
        if v:
            print(f"  {k.upper():12} {v}")


if __name__ == '__main__':
    main()

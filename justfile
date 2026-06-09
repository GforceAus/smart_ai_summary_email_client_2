# Smart AI Summary Email Client - Run Commands
# Sync dependencies using uv
sync:
    uv sync

# Testing the email processor
email_ingestion_test:
    uv run python src/processors/ingest_eml.py --dir ./data/raw/email_dot_eml_format/ --dry-run

# Running the email processor
email_ingestion_run:
    uv run python src/processors/ingest_eml.py --dir ./data/raw/email_dot_eml_format/

# Sync supplier/Client duckdb
datasync_supplier:
    uv run python -m src.database.sync_suppliers

# Fetch detailed supplier tasks
# Usage: just supplier_tasks NULON weekly 10
supplier_tasks supplier frequency='weekly' limit='5':
    uv run python -m src.cli.tasks \
        --supplier {{replace(supplier, 'supplier=', '')}} \
        --frequency {{replace(frequency, 'frequency=', '')}} \
        --limit {{replace(limit, 'limit=', '')}}

generate supplier frequency="weekly":
    uv run python -m src.generators.email_generator \
        --supplier {{replace(supplier, 'supplier=', '')}} \
        --frequency {{replace(frequency, 'frequency=', '')}}

generate_dry supplier frequency="weekly":
    uv run python -m src.generators.email_generator \
        --supplier {{replace(supplier, 'supplier=', '')}} \
        --frequency {{replace(frequency, 'frequency=', '')}} \
        --dry-run

# Smart AI Summary Email Client - Run Commands

# Show all available commands
help:
    @echo ""
    @echo "Smart AI Summary Email Client"
    @echo "=============================="
    @echo ""
    @echo "── Runs ──────────────────────────────────────────────────"
    @echo "  just full_run                        Send emails for all 100 active suppliers"
    @echo "  just full_run_dry                    Generate emails but do NOT send"
    @echo "  just test_mail SUPPLIER [FREQ]       Send email for one supplier"
    @echo "  just generate SUPPLIER [FREQ]        Generate + print email (no send)"
    @echo "  just generate_dry SUPPLIER [FREQ]    Print LLM prompt only (no generation)"
    @echo ""
    @echo "── Client management ─────────────────────────────────────"
    @echo "  just list_clients                    List all clients with frequency + status"
    @echo "  just add_client SUPPLIER [FREQ]      Add new client (active immediately)"
    @echo "  just set_frequency SUPPLIER FREQ     Change weekly/fortnightly/monthly"
    @echo "  just set_manager SUPPLIER EMAIL      Set account manager (primary recipient)"
    @echo "  just activate SUPPLIER               Start sending emails to a client"
    @echo "  just deactivate SUPPLIER             Stop sending emails (keeps in table)"
    @echo ""
    @echo "── Data ──────────────────────────────────────────────────"
    @echo "  just datasync_supplier               Sync suppliers from Postgres + auto-register new"
    @echo "  just supplier_summary SUPPLIER [FREQ] Fetch high-level metrics from Postgres"
    @echo "  just supplier_tasks SUPPLIER [FREQ] [LIMIT]  Fetch detailed task table"
    @echo ""
    @echo "── Diagnostics ───────────────────────────────────────────"
    @echo "  just diag                            System info + Ollama status"
    @echo "  just diag_gpu                        Check GPU offloading"
    @echo "  just diag_full                       Full diagnostic with live inference test"
    @echo ""
    @echo "── Deploy (run from local machine) ───────────────────────"
    @echo "  ./deploy.sh sync                         Rsync code to server (skips .env)"
    @echo "  ./deploy.sh rebuild                      Rebuild Docker image on server"
    @echo "  ./deploy.sh update                       sync + rebuild in one step"
    @echo "  ./deploy.sh run [weekly|fortnightly|monthly]  Trigger run now (default: all)"
    @echo "  ./deploy.sh run-dry [freq]               Dry run now (no emails)"
    @echo "  ./deploy.sh status                       Container + all 3 timer status"
    @echo "  ./deploy.sh logs [freq]                  Logs for one or all frequencies"
    @echo "  ./deploy.sh schedule                     Install all 3 timers:"
    @echo "                                             weekly     → every Mon 23:00"
    @echo "                                             fortnightly → every other Mon 23:00"
    @echo "                                             monthly    → last day of month 07:00"
    @echo "  ./deploy.sh change-time FREQ HH:MM       Change time for one frequency"
    @echo "  ./deploy.sh next-run                     Show next fire for all 3 timers"
    @echo ""
    @echo "  First deploy:"
    @echo "    ./deploy.sh sync"
    @echo "    scp .env user@209.38.82.33:~/data/Applications/services/smart_ai_summary_email_client_2/.env"
    @echo "    ./deploy.sh rebuild"
    @echo "    ./deploy.sh run-dry"
    @echo "    ./deploy.sh schedule"
    @echo ""
    @echo "── Frequencies ───────────────────────────────────────────"
    @echo "  weekly · fortnightly · monthly"
    @echo ""

# Sync dependencies using uv
sync:
    uv sync

# Full run — all 100 active suppliers, tracks timing/memory, sends emails via Graph
full_run:
    uv run python -m src.runners.full_run

# Dry run — generate all emails but do not send
full_run_dry:
    uv run python -m src.runners.full_run --dry-run

# Check what each timer would run today (no Gemini, no email — instant)
schedule_test:
    uv run python -m src.runners.full_run --check-only

# Test mail — generate and SEND email for a single supplier
# Usage: just test_mail NULON weekly
test_mail supplier frequency="weekly":
    uv run python -m src.runners.full_run \
        --supplier {{replace(supplier, 'supplier=', '')}} \
        --frequency {{replace(frequency, 'frequency=', '')}}

# Testing the email processor
email_ingestion_test:
    uv run python src/processors/ingest_eml.py --dir ./data/raw/email_dot_eml_format/ --dry-run

# Running the email processor
email_ingestion_run:
    uv run python src/processors/ingest_eml.py --dir ./data/raw/email_dot_eml_format/

# Sync supplier/Client duckdb (also auto-registers any new suppliers as inactive)
datasync_supplier:
    uv run python -m src.database.sync_suppliers

# ── Client management ──────────────────────────────────────────────────────

# List all clients with frequency and active status
list_clients:
    uv run python -m src.cli.manage list

# Add a new client (active immediately)
# Usage: just add_client NULON weekly
add_client supplier frequency='monthly':
    uv run python -m src.cli.manage add {{supplier}} {{frequency}}

# Change reporting frequency for a client
# Usage: just set_frequency NULON fortnightly
set_frequency supplier frequency:
    uv run python -m src.cli.manage set-frequency {{supplier}} {{frequency}}

# Assign account manager email for a client
# Usage: just set_manager NULON ACappellotto@gforceaus.com
set_manager supplier email:
    uv run python -m src.cli.manage set-manager {{supplier}} {{email}}

# Start sending emails to a client
# Usage: just activate NULON
activate supplier:
    uv run python -m src.cli.manage activate {{supplier}}

# Stop sending emails to a client (keeps them in the table)
# Usage: just deactivate NULON
deactivate supplier:
    uv run python -m src.cli.manage deactivate {{supplier}}

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

# Test run commands for different supplier groups
# Full test - all 100 active suppliers
max_run:
    @echo "=== MAX RUN: Testing all 100 suppliers ==="
    @echo "Weekly suppliers (6):"
    just generate PORTA-TIMBER weekly || true
    just generate QEP weekly || true
    just generate KINCROME weekly || true
    just generate AHE-SEPARATED-HALF-DAY weekly || true
    just generate NULON weekly || true
    just generate WILSON&BRADLEY weekly || true
    @echo "Fortnightly suppliers (4):"
    just generate TIMEPET fortnightly || true
    just generate SCANDIA fortnightly || true
    just generate STROL fortnightly || true
    just generate WHITES fortnightly || true
    @echo "Monthly suppliers (sample - add more as needed):"
    just generate ACOL monthly || true
    just generate GALINTEL monthly || true
    just generate DINDAS monthly || true

# Mid test - weekly and fortnightly suppliers only (10 total)
mid_run:
    @echo "=== MID RUN: Testing weekly + fortnightly suppliers (10 total) ==="
    @echo "Weekly suppliers (6):"
    just generate PORTA-TIMBER weekly || true
    just generate QEP weekly || true
    just generate KINCROME weekly || true
    just generate AHE-SEPARATED-HALF-DAY weekly || true
    just generate NULON weekly || true
    just generate WILSON&BRADLEY weekly || true
    @echo "Fortnightly suppliers (4):"
    just generate TIMEPET fortnightly || true
    just generate SCANDIA fortnightly || true
    just generate STROL fortnightly || true
    just generate WHITES fortnightly || true

# Quick test - subset for rapid testing
quick_test:
    @echo "=== QUICK TEST: Sample suppliers for rapid testing ==="
    just generate OSRAM weekly || true
    just generate NULON weekly || true
    just generate TIMEPET fortnightly || true

# Debug large suppliers (dry-run first to check token count)
debug_large:
    @echo "=== DEBUG LARGE SUPPLIERS: Checking token counts ==="
    @echo "OSRAM (known to timeout):"
    just generate_dry OSRAM weekly || true
    @echo "Other potentially large suppliers:"
    just generate_dry KINCROME weekly || true
    just generate_dry NULON weekly || true

# Test problematic suppliers with dry-run first
test_problematic:
    @echo "=== TESTING PROBLEMATIC SUPPLIERS (dry-run first) ==="
    @echo "Checking OSRAM token count:"
    just generate_dry OSRAM weekly
    @echo "If under 1800 tokens, running actual generation:"
    just generate OSRAM weekly || true

# Quick success test - known working suppliers
quick_success:
    @echo "=== QUICK SUCCESS: Testing known working suppliers ==="
    just generate NULON weekly || true
    just generate TIMEPET fortnightly || true
    just generate ACOL monthly || true

# Token analysis for all high-frequency suppliers
token_analysis:
    @echo "=== TOKEN ANALYSIS: All high-frequency suppliers ==="
    @echo "Weekly suppliers token check:"
    just generate_dry PORTA-TIMBER weekly || true
    just generate_dry QEP weekly || true
    just generate_dry KINCROME weekly || true
    just generate_dry AHE-SEPARATED-HALF-DAY weekly || true
    just generate_dry NULON weekly || true
    just generate_dry WILSON&BRADLEY weekly || true
    @echo "Fortnightly suppliers token check:"
    just generate_dry TIMEPET fortnightly || true
    just generate_dry SCANDIA fortnightly || true
    just generate_dry STROL fortnightly || true
    just generate_dry WHITES fortnightly || true

# Ollama diagnostic commands
# GPU check - fastest way to verify if GPU offloading is working
diag_gpu:
    uv run python ollama_diagnostic.py --gpu-only

# Basic diagnostic - system info + Ollama status + speed benchmark (no large prompt)
diag:
    uv run python ollama_diagnostic.py

# Full diagnostic - everything including 1500-token live inference test
diag_full:
    uv run python ollama_diagnostic.py --full

# Supplier-specific token budget breakdown
diag_supplier supplier frequency="weekly":
    uv run python ollama_diagnostic.py --supplier {{replace(supplier, 'supplier=', '')}} --frequency {{replace(frequency, 'frequency=', '')}}

# Quick diagnostic without benchmark (faster)
diag_quick:
    uv run python ollama_diagnostic.py --skip-bench

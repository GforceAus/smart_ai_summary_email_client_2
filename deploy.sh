#!/usr/bin/env bash
# Smart AI Summary Email Client — Deployment Script
# Run this ON the server, from the service directory.
# Usage: ./deploy.sh [command]

set -euo pipefail

SERVER="user@209.38.82.33"
REMOTE_DIR="~/data/Applications/services/smart_ai_summary_email_client_2"
SERVICE_DIR="$HOME/data/Applications/services/smart_ai_summary_email_client_2"
COMPOSE="docker compose"

# ── colours ────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

log_info()    { echo -e "${BLUE}[INFO]${NC} $1"; }
log_success() { echo -e "${GREEN}[SUCCESS]${NC} $1"; }
log_warning() { echo -e "${YELLOW}[WARNING]${NC} $1"; }
log_error()   { echo -e "${RED}[ERROR]${NC} $1"; }
banner()      { echo -e "\n${CYAN}=== $* ===${NC}"; }

# ── guards ─────────────────────────────────────────────────────────────────
if [[ ! -f "docker-compose.yml" ]]; then
    log_error "docker-compose.yml not found. Run from the service directory:"
    log_error "  cd $SERVICE_DIR && ./deploy.sh"
    exit 1
fi

sync_to_server() {
    banner "Syncing code to server (run from local machine)"
    LOCAL_DIR="$(cd "$(dirname "$0")" && pwd)"
    rsync -avz --progress \
        --exclude='.env' \
        --exclude='.venv' \
        --exclude='__pycache__' \
        --exclude='*.pyc' \
        --exclude='.git' \
        --exclude='graphify-out' \
        --exclude='logs' \
        --exclude='data' \
        "$LOCAL_DIR/" "$SERVER:$REMOTE_DIR/"
    log_success "Sync complete"
    log_warning ".env is NOT synced — copy manually on first deploy:"
    log_info    "  scp .env $SERVER:$REMOTE_DIR/.env"
}

check_dependencies() {
    banner "Checking dependencies"
    command -v docker   &>/dev/null || { log_error "Docker not installed"; exit 1; }
    docker compose version &>/dev/null || { log_error "Docker Compose plugin not found"; exit 1; }
    [[ -f ".env" ]] || { log_error ".env file not found — copy it from your local machine first"; exit 1; }
    [[ -d ".git" ]] || log_warning "Not a git repository — 'update' will skip git pull"
    log_success "Dependencies OK"
}

# ── commands ───────────────────────────────────────────────────────────────

build_image() {
    banner "Building Docker image"
    $COMPOSE build --no-cache
    log_success "Image built"
}

run_now() {
    FREQ="${1:-all}"
    banner "Running now (--run-for $FREQ)"
    $COMPOSE run --rm smart-email uv run python -m src.runners.full_run --run-for "$FREQ"
}

run_dry() {
    FREQ="${1:-all}"
    banner "Dry run (--run-for $FREQ, no emails)"
    $COMPOSE run --rm smart-email uv run python -m src.runners.full_run --run-for "$FREQ" --dry-run
}

check_schedule() {
    banner "Schedule check (no Gemini, no email)"
    $COMPOSE run --rm smart-email uv run python -m src.runners.full_run --check-only
}

show_status() {
    banner "Container status"
    $COMPOSE ps

    banner "Timer status"
    systemctl --user list-timers 'smart-email-*.timer' --no-pager 2>/dev/null \
        || log_warning "No timers installed — run: ./deploy.sh schedule"
}

show_logs() {
    FREQ="${1:-}"
    if [[ -n "$FREQ" ]]; then
        banner "Logs — $FREQ"
        journalctl --user -u "smart-email-${FREQ}.service" -n 100 --no-pager 2>/dev/null \
            || log_warning "No logs yet for $FREQ"
    else
        banner "Recent logs (all frequencies)"
        for freq in weekly fortnightly monthly; do
            echo -e "\n${YELLOW}--- $freq ---${NC}"
            journalctl --user -u "smart-email-${freq}.service" -n 20 --no-pager 2>/dev/null \
                || echo "  No logs yet"
        done
    fi
}

update_service() {
    banner "Updating service"
    check_dependencies

    if [[ -d ".git" ]]; then
        log_info "Pulling latest code..."
        git pull
        log_success "Code updated"
    else
        log_warning "Skipping git pull — not a git repo"
    fi

    build_image
    log_success "Update complete — run './deploy.sh run-dry' to verify"
}

install_schedule() {
    banner "Installing 3 systemd timers"
    mkdir -p ~/.config/systemd/user

    # ── weekly ────────────────────────────────────────────────────────────
    cat > ~/.config/systemd/user/smart-email-weekly.service <<SVC
[Unit]
Description=Smart AI Summary Email — weekly suppliers

[Service]
Type=oneshot
WorkingDirectory=${SERVICE_DIR}
ExecStart=/usr/bin/docker compose run --rm smart-email uv run python -m src.runners.full_run --run-for weekly
StandardOutput=journal
StandardError=journal
SVC

    cat > ~/.config/systemd/user/smart-email-weekly.timer <<TMR
[Unit]
Description=Smart Email — weekly (every Monday 23:00)

[Timer]
OnCalendar=Mon *-*-* 23:00:00
Persistent=true

[Install]
WantedBy=timers.target
TMR

    # ── fortnightly ───────────────────────────────────────────────────────
    cat > ~/.config/systemd/user/smart-email-fortnightly.service <<SVC
[Unit]
Description=Smart AI Summary Email — fortnightly suppliers

[Service]
Type=oneshot
WorkingDirectory=${SERVICE_DIR}
ExecStart=/usr/bin/docker compose run --rm smart-email uv run python -m src.runners.full_run --run-for fortnightly
StandardOutput=journal
StandardError=journal
SVC

    cat > ~/.config/systemd/user/smart-email-fortnightly.timer <<TMR
[Unit]
Description=Smart Email — fortnightly (every Monday 23:00, skips even ISO weeks)

[Timer]
OnCalendar=Mon *-*-* 23:00:00
Persistent=true

[Install]
WantedBy=timers.target
TMR

    # ── monthly ───────────────────────────────────────────────────────────
    cat > ~/.config/systemd/user/smart-email-monthly.service <<SVC
[Unit]
Description=Smart AI Summary Email — monthly suppliers

[Service]
Type=oneshot
WorkingDirectory=${SERVICE_DIR}
ExecStart=/usr/bin/docker compose run --rm smart-email uv run python -m src.runners.full_run --run-for monthly
StandardOutput=journal
StandardError=journal
SVC

    cat > ~/.config/systemd/user/smart-email-monthly.timer <<TMR
[Unit]
Description=Smart Email — monthly (last day of month 23:00)

[Timer]
OnCalendar=*-*~1 23:00:00
Persistent=true

[Install]
WantedBy=timers.target
TMR

    systemctl --user daemon-reload
    systemctl --user enable --now \
        smart-email-weekly.timer \
        smart-email-fortnightly.timer \
        smart-email-monthly.timer

    log_success "All 3 timers installed. Next runs:"
    systemctl --user list-timers 'smart-email-*.timer' --no-pager
}

change_time() {
    FREQ="${1:?Usage: ./deploy.sh change-time weekly|fortnightly|monthly HH:MM}"
    TIME="${2:?Usage: ./deploy.sh change-time weekly|fortnightly|monthly HH:MM}"
    HOUR="${TIME%%:*}"
    MIN="${TIME##*:}"
    banner "Updating ${FREQ} timer to ${HOUR}:${MIN}"

    case "$FREQ" in
        weekly|fortnightly) CALENDAR="Mon *-*-* ${HOUR}:${MIN}:00" ;;
        monthly)            CALENDAR="*-*~1 ${HOUR}:${MIN}:00" ;;
        *) log_error "Unknown frequency: $FREQ (use weekly, fortnightly, or monthly)"; exit 1 ;;
    esac

    TIMER_FILE="$HOME/.config/systemd/user/smart-email-${FREQ}.timer"
    [[ -f "$TIMER_FILE" ]] || { log_error "Timer not installed — run ./deploy.sh schedule first"; exit 1; }

    sed -i "s|^OnCalendar=.*|OnCalendar=${CALENDAR}|" "$TIMER_FILE"
    systemctl --user daemon-reload
    systemctl --user restart "smart-email-${FREQ}.timer"
    log_success "${FREQ} timer updated to ${HOUR}:${MIN}"
    systemctl --user list-timers "smart-email-${FREQ}.timer" --no-pager
}

# ── dispatch ───────────────────────────────────────────────────────────────
case "${1:-help}" in
    sync)          sync_to_server ;;
    build)         build_image ;;
    run)           run_now "${2:-all}" ;;
    run-dry)       run_dry "${2:-all}" ;;
    check)         check_schedule ;;
    status)        show_status ;;
    logs)          show_logs "${2:-}" ;;
    update)        update_service ;;
    schedule)      install_schedule ;;
    change-time)   change_time "${2:-}" "${3:-}" ;;
    next-run)      banner "Next scheduled runs"
                   systemctl --user list-timers 'smart-email-*.timer' --no-pager 2>/dev/null \
                       || log_warning "No timers installed — run: ./deploy.sh schedule" ;;
    help|*)
        echo ""
        echo "Usage: ./deploy.sh <command> [args]"
        echo ""
        echo "  sync                         Rsync code to server (run from local machine)"
        echo "  build                        Rebuild Docker image (run on server)"
        echo "  update                       git pull + rebuild (run on server)"
        echo "  run [weekly|fortnightly|monthly]  Trigger run now (default: all)"
        echo "  run-dry [freq]               Dry run (no emails sent)"
        echo "  check                        Show what would run today (no Gemini)"
        echo "  status                       Container + timer status"
        echo "  logs [weekly|fortnightly|monthly]  Show logs (default: all)"
        echo "  schedule                     Install all 3 timers:"
        echo "                                 weekly     → every Monday 23:00"
        echo "                                 fortnightly → every other Monday 23:00"
        echo "                                 monthly    → last day of month 23:00"
        echo "  change-time FREQ HH:MM       Update scheduled time for one frequency"
        echo "  next-run                     Show next fire time for all 3 timers"
        echo ""
        echo "  Note: fortnightly skips even ISO weeks (set FORTNIGHTLY_PARITY=even to flip)"
        echo ""
        echo "First deploy:"
        echo "  LOCAL:  ./deploy.sh sync"
        echo "  LOCAL:  scp .env $SERVER:$REMOTE_DIR/.env"
        echo "  SERVER: ./deploy.sh build"
        echo "  SERVER: ./deploy.sh run-dry   # verify"
        echo "  SERVER: ./deploy.sh schedule  # install timers"
        echo ""
        ;;
esac

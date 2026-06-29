#!/usr/bin/env bash
# deploy.sh — manage smart-email on the remote server
# Usage: ./deploy.sh <command>

set -euo pipefail

SERVER="user@209.38.82.33"
REMOTE_DIR="~/data/Applications/services/smart_ai_summary_email_client_2"
LOCAL_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE="smart-email"

# Monday morning 07:00 server local time (change with: ./deploy.sh schedule HH:MM)
DEFAULT_TIME="07:00"

_ssh() { ssh "$SERVER" "$@"; }

_banner() { echo -e "\n\033[1;36m=== $* ===\033[0m"; }

case "${1:-help}" in

  # ── sync ───────────────────────────────────────────────────────────────────
  sync)
    _banner "Syncing code to server"
    rsync -avz --progress \
      --exclude='.env' \
      --exclude='.venv' \
      --exclude='__pycache__' \
      --exclude='*.pyc' \
      --exclude='.git' \
      --exclude='graphify-out' \
      --exclude='delme' \
      --exclude='logs' \
      "$LOCAL_DIR/" "$SERVER:$REMOTE_DIR/"
    echo "✓ Sync complete"
    echo "  NOTE: .env is NOT synced — copy it manually if first deploy:"
    echo "  scp .env $SERVER:$REMOTE_DIR/.env"
    ;;

  # ── rebuild ────────────────────────────────────────────────────────────────
  rebuild)
    _banner "Rebuilding Docker image on server"
    _ssh "cd $REMOTE_DIR && docker compose build --no-cache"
    echo "✓ Image rebuilt"
    ;;

  # ── update ─────────────────────────────────────────────────────────────────
  update)
    _banner "Updating: sync + rebuild"
    "$0" sync
    "$0" rebuild
    echo "✓ Update complete"
    ;;

  # ── run ────────────────────────────────────────────────────────────────────
  # Usage: ./deploy.sh run [weekly|fortnightly|monthly]  (default: all)
  run)
    FREQ="${2:-all}"
    _banner "Triggering manual run on server (--run-for $FREQ)"
    _ssh "cd $REMOTE_DIR && docker compose run --rm $SERVICE uv run python -m src.runners.full_run --run-for $FREQ"
    ;;

  run-dry)
    FREQ="${2:-all}"
    _banner "Triggering dry run on server (--run-for $FREQ)"
    _ssh "cd $REMOTE_DIR && docker compose run --rm $SERVICE uv run python -m src.runners.full_run --run-for $FREQ --dry-run"
    ;;

  # ── status ─────────────────────────────────────────────────────────────────
  status)
    _banner "Container status"
    _ssh "cd $REMOTE_DIR && docker compose ps"
    echo ""
    _banner "Timer status"
    _ssh "systemctl --user list-timers 'smart-email-*.timer' --no-pager 2>/dev/null || echo 'No timers installed — run: ./deploy.sh schedule'"
    ;;

  # ── logs ───────────────────────────────────────────────────────────────────
  logs)
    FREQ="${2:-}"
    if [ -n "$FREQ" ]; then
      _banner "Logs — smart-email-${FREQ}"
      _ssh "journalctl --user -u smart-email-${FREQ}.service -n 100 --no-pager 2>/dev/null || echo 'No logs yet'"
    else
      _banner "Recent logs (all frequencies)"
      for freq in weekly fortnightly monthly; do
        echo -e "\n--- $freq ---"
        _ssh "journalctl --user -u smart-email-${freq}.service -n 20 --no-pager 2>/dev/null || echo '  No logs yet'"
      done
    fi
    ;;

  # ── schedule ───────────────────────────────────────────────────────────────
  # Installs 3 systemd user timers:
  #   weekly     — every Monday 07:00
  #   fortnightly — every Monday 07:00 (script skips wrong ISO weeks)
  #   monthly    — last day of month 07:00
  schedule)
    _banner "Installing 3 timers: weekly (Mon 23:00) · fortnightly (every other Mon 23:00) · monthly (last day 23:00)"
    _ssh bash <<'ENDSSH'
set -e
mkdir -p ~/.config/systemd/user
REMOTE_DIR="$HOME/data/Applications/services/smart_ai_summary_email_client_2"

# ── weekly ──────────────────────────────────────────────────────────────────
cat > ~/.config/systemd/user/smart-email-weekly.service <<SVC
[Unit]
Description=Smart AI Summary Email — weekly suppliers

[Service]
Type=oneshot
WorkingDirectory=$REMOTE_DIR
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

# ── fortnightly ─────────────────────────────────────────────────────────────
cat > ~/.config/systemd/user/smart-email-fortnightly.service <<SVC
[Unit]
Description=Smart AI Summary Email — fortnightly suppliers

[Service]
Type=oneshot
WorkingDirectory=$REMOTE_DIR
ExecStart=/usr/bin/docker compose run --rm smart-email uv run python -m src.runners.full_run --run-for fortnightly
StandardOutput=journal
StandardError=journal
SVC

cat > ~/.config/systemd/user/smart-email-fortnightly.timer <<TMR
[Unit]
Description=Smart Email — fortnightly (every Monday 23:00, script skips even ISO weeks)

[Timer]
OnCalendar=Mon *-*-* 23:00:00
Persistent=true

[Install]
WantedBy=timers.target
TMR

# ── monthly ─────────────────────────────────────────────────────────────────
cat > ~/.config/systemd/user/smart-email-monthly.service <<SVC
[Unit]
Description=Smart AI Summary Email — monthly suppliers

[Service]
Type=oneshot
WorkingDirectory=$REMOTE_DIR
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
systemctl --user enable --now smart-email-weekly.timer smart-email-fortnightly.timer smart-email-monthly.timer
echo ""
echo "✓ All timers installed. Next runs:"
systemctl --user list-timers 'smart-email-*.timer' --no-pager
ENDSSH
    ;;

  # ── change-time ────────────────────────────────────────────────────────────
  # Usage: ./deploy.sh change-time weekly 08:00
  change-time)
    FREQ="${2:?Usage: ./deploy.sh change-time weekly|fortnightly|monthly HH:MM}"
    TIME="${3:?Usage: ./deploy.sh change-time weekly|fortnightly|monthly HH:MM}"
    HOUR="${TIME%%:*}"
    MIN="${TIME##*:}"
    _banner "Updating ${FREQ} timer to ${HOUR}:${MIN}"
    case "$FREQ" in
      weekly|fortnightly)
        CALENDAR="Mon *-*-* ${HOUR}:${MIN}:00"
        ;;
      monthly)
        CALENDAR="*-*~1 ${HOUR}:${MIN}:00"
        ;;
      *)
        echo "Unknown frequency: $FREQ (use weekly, fortnightly, or monthly)"; exit 1 ;;
    esac
    _ssh bash <<EOF
sed -i "s|^OnCalendar=.*|OnCalendar=${CALENDAR}|" ~/.config/systemd/user/smart-email-${FREQ}.timer
systemctl --user daemon-reload
systemctl --user restart smart-email-${FREQ}.timer
echo "✓ ${FREQ} timer updated to ${HOUR}:${MIN}"
systemctl --user list-timers smart-email-${FREQ}.timer --no-pager
EOF
    ;;

  # ── next-run ───────────────────────────────────────────────────────────────
  next-run)
    _banner "Next scheduled runs"
    _ssh "systemctl --user list-timers 'smart-email-*.timer' --no-pager 2>/dev/null || echo 'No timers installed — run: ./deploy.sh schedule'"
    ;;

  # ── help ───────────────────────────────────────────────────────────────────
  help|*)
    echo ""
    echo "Usage: ./deploy.sh <command> [args]"
    echo ""
    echo "  sync                         Rsync code to server (skips .env)"
    echo "  rebuild                      Rebuild Docker image on server"
    echo "  update                       sync + rebuild in one step"
    echo "  run [weekly|fortnightly|monthly]  Trigger run now (default: all)"
    echo "  run-dry [freq]               Dry run now (no emails sent)"
    echo "  status                       Container + all timer status"
    echo "  logs [weekly|fortnightly|monthly]  Show logs (default: all)"
    echo "  schedule                     Install all 3 timers:"
    echo "                                 weekly     → every Monday 23:00"
    echo "                                 fortnightly → every other Monday 23:00"
    echo "                                 monthly    → last day of month 23:00"
    echo "  change-time FREQ HH:MM       Update timer for one frequency"
    echo "  next-run                     Show next fire time for all 3 timers"
    echo ""
    echo "  Note: fortnightly runs every Monday but skips even ISO weeks."
    echo "  Set FORTNIGHTLY_PARITY=even in .env to flip to even weeks."
    echo ""
    echo "First deploy:"
    echo "  1. ./deploy.sh sync"
    echo "  2. scp .env $SERVER:$REMOTE_DIR/.env"
    echo "  3. ./deploy.sh rebuild"
    echo "  4. ./deploy.sh run-dry       # verify it works"
    echo "  5. ./deploy.sh schedule      # install all 3 timers"
    echo ""
    ;;
esac

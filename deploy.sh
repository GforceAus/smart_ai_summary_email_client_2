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
  run)
    _banner "Triggering manual run on server"
    _ssh "cd $REMOTE_DIR && docker compose run --rm $SERVICE"
    ;;

  run-dry)
    _banner "Triggering dry run on server"
    _ssh "cd $REMOTE_DIR && docker compose run --rm $SERVICE uv run python -m src.runners.full_run --dry-run"
    ;;

  # ── status ─────────────────────────────────────────────────────────────────
  status)
    _banner "Container + timer status"
    _ssh "cd $REMOTE_DIR && docker compose ps"
    echo ""
    _ssh "systemctl --user status smart-email.timer 2>/dev/null || echo 'Timer not installed yet — run: ./deploy.sh schedule'"
    ;;

  # ── logs ───────────────────────────────────────────────────────────────────
  logs)
    _banner "Recent logs"
    _ssh "journalctl --user -u smart-email.service -n 100 --no-pager 2>/dev/null || echo 'No systemd logs yet'"
    ;;

  # ── schedule ───────────────────────────────────────────────────────────────
  schedule)
    TIME="${2:-$DEFAULT_TIME}"
    HOUR="${TIME%%:*}"
    MIN="${TIME##*:}"
    _banner "Installing Monday morning timer — ${HOUR}:${MIN} server time"
    _ssh bash <<EOF
set -e
mkdir -p ~/.config/systemd/user

cat > ~/.config/systemd/user/smart-email.service <<'SVC'
[Unit]
Description=Smart AI Summary Email — weekly supplier run

[Service]
Type=oneshot
WorkingDirectory=$REMOTE_DIR
ExecStart=/usr/bin/docker compose run --rm $SERVICE
StandardOutput=journal
StandardError=journal
SVC

cat > ~/.config/systemd/user/smart-email.timer <<'TMR'
[Unit]
Description=Smart AI Summary Email — every Monday morning

[Timer]
OnCalendar=Mon *-*-* ${HOUR}:${MIN}:00
Persistent=true

[Install]
WantedBy=timers.target
TMR

systemctl --user daemon-reload
systemctl --user enable --now smart-email.timer
systemctl --user status smart-email.timer
echo "✓ Timer installed — next run:"
systemctl --user list-timers smart-email.timer --no-pager
EOF
    ;;

  # ── change-time ────────────────────────────────────────────────────────────
  change-time)
    TIME="${2:?Usage: ./deploy.sh change-time HH:MM}"
    _banner "Updating schedule to $TIME"
    "$0" schedule "$TIME"
    ;;

  # ── next-run ───────────────────────────────────────────────────────────────
  next-run)
    _banner "Next scheduled run"
    _ssh "systemctl --user list-timers smart-email.timer --no-pager 2>/dev/null || echo 'Timer not installed'"
    ;;

  # ── help ───────────────────────────────────────────────────────────────────
  help|*)
    echo ""
    echo "Usage: ./deploy.sh <command> [args]"
    echo ""
    echo "  sync            Rsync code to server (skips .env)"
    echo "  rebuild         Rebuild Docker image on server"
    echo "  update          sync + rebuild in one step"
    echo "  run             Trigger a full run now (sends emails)"
    echo "  run-dry         Trigger a dry run now (no emails)"
    echo "  status          Show container + timer status"
    echo "  logs            Show last 100 log lines"
    echo "  schedule [HH:MM]  Install Monday morning systemd timer (default 07:00)"
    echo "  change-time HH:MM  Update the scheduled time"
    echo "  next-run        Show when the next run is scheduled"
    echo ""
    echo "First deploy:"
    echo "  1. ./deploy.sh sync"
    echo "  2. scp .env $SERVER:$REMOTE_DIR/.env"
    echo "  3. ./deploy.sh rebuild"
    echo "  4. ./deploy.sh run-dry    # verify it works"
    echo "  5. ./deploy.sh schedule   # install Monday 07:00 timer"
    echo ""
    ;;
esac

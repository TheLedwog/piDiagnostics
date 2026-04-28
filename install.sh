#!/usr/bin/env bash
# Install pi_system_report.py as a Raspberry Pi systemd timer that sends reports to Telegram.

set -euo pipefail

APP_NAME="pi-system-report"
INSTALL_DIR="/opt/pi-system-report"
SCRIPT_NAME="pi_system_report.py"
CONFIG_FILE="/etc/pi-system-report.env"
SERVICE_FILE="/etc/systemd/system/${APP_NAME}.service"
TIMER_FILE="/etc/systemd/system/${APP_NAME}.timer"
ALERT_SERVICE_FILE="/etc/systemd/system/${APP_NAME}-alert.service"
ALERT_TIMER_FILE="/etc/systemd/system/${APP_NAME}-alert.timer"
STATE_DIR="/var/lib/pi-system-report"
ALERT_STATE_FILE="${STATE_DIR}/alert-state.json"

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  installer_path="$0"
  if [[ "$installer_path" != /* ]]; then
    installer_path="$(pwd)/$installer_path"
  fi
  echo "This installer needs sudo so it can write to /opt, /etc, and systemd."
  exec sudo -E bash "$installer_path" "$@"
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_SCRIPT="${SCRIPT_DIR}/${SCRIPT_NAME}"

usage() {
  cat <<USAGE
Usage: sudo bash install.sh [options]

Options:
  --token TOKEN            Telegram bot token
  --chat-id CHAT_ID        Telegram chat ID. If omitted, installer can try to find it.
  --every-minutes N        Send a report every N minutes (default: 30)
  --duration N             Monitor for N seconds per report (default: 30)
  --top N                  Number of CPU-heavy programs to show (default: 10)
  --alert-every-minutes N  Check CPU/RAM alerts every N minutes (default: 5)
  --cpu-alert-percent N    Send alert when CPU reaches this percent (default: 60)
  --ram-alert-percent N    Send alert when RAM reaches this percent (default: 60)
  --alert-cooldown N       Minimum minutes between repeated alerts (default: 30)
  --silent                 Send Telegram messages silently
  --no-run-now             Install timer but do not send the first report now
  --uninstall              Remove installed services, timers, config, and state
  -h, --help               Show this help

You can also set PI_REPORT_TELEGRAM_BOT_TOKEN and PI_REPORT_TELEGRAM_CHAT_ID.
USAGE
}

uninstall_app() {
  echo "Uninstalling ${APP_NAME}..."

  if command -v systemctl >/dev/null 2>&1; then
    systemctl disable --now "${APP_NAME}.timer" >/dev/null 2>&1 || true
    systemctl disable --now "${APP_NAME}-alert.timer" >/dev/null 2>&1 || true
    systemctl stop "${APP_NAME}.service" >/dev/null 2>&1 || true
    systemctl stop "${APP_NAME}-alert.service" >/dev/null 2>&1 || true
  fi

  rm -f "$SERVICE_FILE" "$TIMER_FILE" "$ALERT_SERVICE_FILE" "$ALERT_TIMER_FILE"
  rm -f "$CONFIG_FILE"
  rm -rf "$INSTALL_DIR" "$STATE_DIR"

  if command -v systemctl >/dev/null 2>&1; then
    systemctl daemon-reload
    systemctl reset-failed "${APP_NAME}.service" "${APP_NAME}.timer" \
      "${APP_NAME}-alert.service" "${APP_NAME}-alert.timer" >/dev/null 2>&1 || true
  fi

  echo "Uninstalled ${APP_NAME}."
}

escape_env_value() {
  local value="$1"
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  value="${value//\$/\\\$}"
  value="${value//\`/\\\`}"
  printf '%s' "$value"
}

prompt_value() {
  local prompt="$1"
  local current="${2:-}"
  local answer

  if [[ -n "$current" ]]; then
    read -r -p "${prompt} [keep existing]: " answer
    printf '%s' "${answer:-$current}"
  else
    read -r -p "${prompt}: " answer
    printf '%s' "$answer"
  fi
}

prompt_secret() {
  local prompt="$1"
  local current="${2:-}"
  local answer

  if [[ -n "$current" ]]; then
    read -r -s -p "${prompt} [keep existing, hidden input]: " answer
    echo >&2
    printf '%s' "${answer:-$current}"
  else
    read -r -s -p "${prompt} [hidden input]: " answer
    echo >&2
    printf '%s' "$answer"
  fi
}

require_positive_integer() {
  local name="$1"
  local value="$2"
  if [[ ! "$value" =~ ^[1-9][0-9]*$ ]]; then
    echo "${name} must be a positive integer, got '${value}'." >&2
    exit 2
  fi
}

require_percent() {
  local name="$1"
  local value="$2"
  if [[ ! "$value" =~ ^([0-9]|[1-9][0-9]|100)(\.[0-9]+)?$ ]]; then
    echo "${name} must be a number from 0 to 100, got '${value}'." >&2
    exit 2
  fi
}

detect_chat_id() {
  local token="$1"
  "$PYTHON_BIN" - "$token" <<'PY'
import json
import sys
import urllib.error
import urllib.request

token = sys.argv[1]
url = f"https://api.telegram.org/bot{token}/getUpdates"
try:
    with urllib.request.urlopen(url, timeout=15) as response:
        body = json.loads(response.read().decode("utf-8", errors="replace"))
except (urllib.error.URLError, json.JSONDecodeError, TimeoutError):
    raise SystemExit(1)

if not body.get("ok"):
    raise SystemExit(1)

for update in reversed(body.get("result", [])):
    message = (
        update.get("message")
        or update.get("edited_message")
        or update.get("channel_post")
        or update.get("my_chat_member")
    )
    chat = message.get("chat") if isinstance(message, dict) else None
    if isinstance(chat, dict) and "id" in chat:
        print(chat["id"])
        raise SystemExit(0)

raise SystemExit(1)
PY
}

if [[ -f "$CONFIG_FILE" ]]; then
  # shellcheck disable=SC1090
  source "$CONFIG_FILE"
fi

BOT_TOKEN="${PI_REPORT_TELEGRAM_BOT_TOKEN:-${TELEGRAM_BOT_TOKEN:-}}"
CHAT_ID="${PI_REPORT_TELEGRAM_CHAT_ID:-${TELEGRAM_CHAT_ID:-}}"
REPORT_EVERY_MINUTES="${PI_REPORT_EVERY_MINUTES:-30}"
REPORT_DURATION="${PI_REPORT_DURATION:-30}"
REPORT_INTERVAL="${PI_REPORT_INTERVAL:-1}"
REPORT_TOP="${PI_REPORT_TOP:-10}"
ALERT_EVERY_MINUTES="${PI_REPORT_ALERT_EVERY_MINUTES:-5}"
CPU_ALERT_PERCENT="${PI_REPORT_CPU_ALERT_PERCENT:-60}"
RAM_ALERT_PERCENT="${PI_REPORT_RAM_ALERT_PERCENT:-60}"
ALERT_COOLDOWN_MINUTES="${PI_REPORT_ALERT_COOLDOWN_MINUTES:-30}"
TELEGRAM_SILENT="${PI_REPORT_TELEGRAM_SILENT:-}"
RUN_NOW="1"
UNINSTALL="0"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --token)
      BOT_TOKEN="${2:-}"
      shift 2
      ;;
    --chat-id)
      CHAT_ID="${2:-}"
      shift 2
      ;;
    --every-minutes)
      REPORT_EVERY_MINUTES="${2:-}"
      shift 2
      ;;
    --duration)
      REPORT_DURATION="${2:-}"
      shift 2
      ;;
    --top)
      REPORT_TOP="${2:-}"
      shift 2
      ;;
    --alert-every-minutes)
      ALERT_EVERY_MINUTES="${2:-}"
      shift 2
      ;;
    --cpu-alert-percent)
      CPU_ALERT_PERCENT="${2:-}"
      shift 2
      ;;
    --ram-alert-percent)
      RAM_ALERT_PERCENT="${2:-}"
      shift 2
      ;;
    --alert-cooldown)
      ALERT_COOLDOWN_MINUTES="${2:-}"
      shift 2
      ;;
    --silent)
      TELEGRAM_SILENT="1"
      shift
      ;;
    --no-run-now)
      RUN_NOW="0"
      shift
      ;;
    --uninstall)
      UNINSTALL="1"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage
      exit 2
      ;;
  esac
done

if [[ "$UNINSTALL" == "1" ]]; then
  uninstall_app
  exit 0
fi

if [[ ! -f "$SOURCE_SCRIPT" ]]; then
  echo "Could not find ${SCRIPT_NAME} next to this installer." >&2
  echo "Copy install.sh and ${SCRIPT_NAME} to the same folder on your Raspberry Pi." >&2
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required but was not found. Install it with: sudo apt install python3" >&2
  exit 1
fi
PYTHON_BIN="$(command -v python3)"

if ! command -v systemctl >/dev/null 2>&1; then
  echo "systemctl is required for the scheduled installer." >&2
  exit 1
fi

if [[ -t 0 ]]; then
  echo
  echo "Telegram setup"
  echo "Paste your bot token from @BotFather. The installer will store it in ${CONFIG_FILE}."
  echo "The token is hidden while you type or paste it. That is normal: paste it, then press Enter."
  BOT_TOKEN="$(prompt_secret "Telegram bot token" "$BOT_TOKEN")"
  if [[ -z "$CHAT_ID" ]]; then
    echo
    echo "Now open Telegram, send any message to your bot, then press Enter here."
    read -r -p "Press Enter after messaging the bot..."
    detected_chat_id="$(detect_chat_id "$BOT_TOKEN" || true)"
    if [[ -n "$detected_chat_id" ]]; then
      echo "Found Telegram chat ID: ${detected_chat_id}"
      CHAT_ID="$detected_chat_id"
    else
      echo "I could not auto-detect the chat ID. You can paste it manually."
      echo "Tip: open https://api.telegram.org/botYOUR_TOKEN/getUpdates and look for chat id."
    fi
  fi
  CHAT_ID="$(prompt_value "Telegram chat ID" "$CHAT_ID")"
  echo
  echo "Report and alert settings"
  REPORT_EVERY_MINUTES="$(prompt_value "Send a report every how many minutes" "$REPORT_EVERY_MINUTES")"
  ALERT_EVERY_MINUTES="$(prompt_value "Check for CPU/RAM alerts every how many minutes" "$ALERT_EVERY_MINUTES")"
  CPU_ALERT_PERCENT="$(prompt_value "CPU alert percent" "$CPU_ALERT_PERCENT")"
  RAM_ALERT_PERCENT="$(prompt_value "RAM alert percent" "$RAM_ALERT_PERCENT")"
  ALERT_COOLDOWN_MINUTES="$(prompt_value "Minimum minutes between repeated alerts" "$ALERT_COOLDOWN_MINUTES")"
else
  if [[ -z "$BOT_TOKEN" || -z "$CHAT_ID" ]]; then
    echo "Non-interactive install needs --token and --chat-id, or PI_REPORT_TELEGRAM_* env vars." >&2
    exit 2
  fi
fi

if [[ -z "$BOT_TOKEN" || -z "$CHAT_ID" ]]; then
  echo "Telegram bot token and chat ID are required." >&2
  exit 2
fi

require_positive_integer "--every-minutes" "$REPORT_EVERY_MINUTES"
require_positive_integer "--duration" "$REPORT_DURATION"
require_positive_integer "--top" "$REPORT_TOP"
require_positive_integer "--alert-every-minutes" "$ALERT_EVERY_MINUTES"
require_positive_integer "--alert-cooldown" "$ALERT_COOLDOWN_MINUTES"
require_positive_integer "PI_REPORT_INTERVAL" "$REPORT_INTERVAL"
require_percent "--cpu-alert-percent" "$CPU_ALERT_PERCENT"
require_percent "--ram-alert-percent" "$RAM_ALERT_PERCENT"

echo "Installing ${APP_NAME}..."
install -d -m 755 "$INSTALL_DIR"
install -d -m 700 "$STATE_DIR"
install -m 755 "$SOURCE_SCRIPT" "${INSTALL_DIR}/${SCRIPT_NAME}"

tmp_config="$(mktemp)"
{
  printf 'PI_REPORT_TELEGRAM_BOT_TOKEN="%s"\n' "$(escape_env_value "$BOT_TOKEN")"
  printf 'PI_REPORT_TELEGRAM_CHAT_ID="%s"\n' "$(escape_env_value "$CHAT_ID")"
  printf 'PI_REPORT_TELEGRAM_SILENT="%s"\n' "$(escape_env_value "$TELEGRAM_SILENT")"
  printf 'PI_REPORT_DURATION="%s"\n' "$(escape_env_value "$REPORT_DURATION")"
  printf 'PI_REPORT_INTERVAL="%s"\n' "$(escape_env_value "$REPORT_INTERVAL")"
  printf 'PI_REPORT_TOP="%s"\n' "$(escape_env_value "$REPORT_TOP")"
  printf 'PI_REPORT_EVERY_MINUTES="%s"\n' "$(escape_env_value "$REPORT_EVERY_MINUTES")"
  printf 'PI_REPORT_ALERT_EVERY_MINUTES="%s"\n' "$(escape_env_value "$ALERT_EVERY_MINUTES")"
  printf 'PI_REPORT_CPU_ALERT_PERCENT="%s"\n' "$(escape_env_value "$CPU_ALERT_PERCENT")"
  printf 'PI_REPORT_RAM_ALERT_PERCENT="%s"\n' "$(escape_env_value "$RAM_ALERT_PERCENT")"
  printf 'PI_REPORT_ALERT_COOLDOWN_MINUTES="%s"\n' "$(escape_env_value "$ALERT_COOLDOWN_MINUTES")"
  printf 'PI_REPORT_ALERT_STATE_FILE="%s"\n' "$(escape_env_value "$ALERT_STATE_FILE")"
} > "$tmp_config"
install -m 600 "$tmp_config" "$CONFIG_FILE"
rm -f "$tmp_config"

cat > "$SERVICE_FILE" <<SERVICE
[Unit]
Description=Send Raspberry Pi system report to Telegram
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
EnvironmentFile=${CONFIG_FILE}
ExecStart=/bin/sh -c 'exec ${PYTHON_BIN} ${INSTALL_DIR}/${SCRIPT_NAME} --duration "\$PI_REPORT_DURATION" --interval "\$PI_REPORT_INTERVAL" --top "\$PI_REPORT_TOP" --send-telegram \${PI_REPORT_TELEGRAM_SILENT:+--telegram-silent}'
SERVICE

cat > "$TIMER_FILE" <<TIMER
[Unit]
Description=Run Raspberry Pi system report every ${REPORT_EVERY_MINUTES} minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=${REPORT_EVERY_MINUTES}min
AccuracySec=1min
Persistent=true
Unit=${APP_NAME}.service

[Install]
WantedBy=timers.target
TIMER

cat > "$ALERT_SERVICE_FILE" <<SERVICE
[Unit]
Description=Send Raspberry Pi CPU/RAM alert to Telegram
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
EnvironmentFile=${CONFIG_FILE}
ExecStart=/bin/sh -c 'exec ${PYTHON_BIN} ${INSTALL_DIR}/${SCRIPT_NAME} --duration "\$PI_REPORT_DURATION" --interval "\$PI_REPORT_INTERVAL" --top "\$PI_REPORT_TOP" --alert-only --cpu-alert-percent "\$PI_REPORT_CPU_ALERT_PERCENT" --ram-alert-percent "\$PI_REPORT_RAM_ALERT_PERCENT" --alert-cooldown-minutes "\$PI_REPORT_ALERT_COOLDOWN_MINUTES" --alert-state-file "\$PI_REPORT_ALERT_STATE_FILE" \${PI_REPORT_TELEGRAM_SILENT:+--telegram-silent}'
SERVICE

cat > "$ALERT_TIMER_FILE" <<TIMER
[Unit]
Description=Check Raspberry Pi CPU/RAM alert thresholds every ${ALERT_EVERY_MINUTES} minutes

[Timer]
OnBootSec=3min
OnUnitActiveSec=${ALERT_EVERY_MINUTES}min
AccuracySec=1min
Persistent=true
Unit=${APP_NAME}-alert.service

[Install]
WantedBy=timers.target
TIMER

systemctl daemon-reload

if [[ "$RUN_NOW" == "1" ]]; then
  echo "Sending the first Telegram report now. This will take about ${REPORT_DURATION} seconds..."
  if ! systemctl start "${APP_NAME}.service"; then
    echo
    echo "The first report failed. Recent logs:" >&2
    journalctl -u "${APP_NAME}.service" -n 40 --no-pager >&2 || true
    echo
    echo "Fix ${CONFIG_FILE}, then test again with: sudo systemctl start ${APP_NAME}.service" >&2
    exit 1
  fi
fi

systemctl enable --now "${APP_NAME}.timer" >/dev/null
systemctl enable --now "${APP_NAME}-alert.timer" >/dev/null

echo
echo "Installed."
echo "Timer:       ${APP_NAME}.timer"
echo "Service:     ${APP_NAME}.service"
echo "Alert timer: ${APP_NAME}-alert.timer"
echo "Alert svc:   ${APP_NAME}-alert.service"
echo "Config:      ${CONFIG_FILE}"
echo "Program:     ${INSTALL_DIR}/${SCRIPT_NAME}"
echo "Schedule:    every ${REPORT_EVERY_MINUTES} minutes"
echo "Alerts:      CPU >= ${CPU_ALERT_PERCENT}% or RAM >= ${RAM_ALERT_PERCENT}%"
echo "Alert check: every ${ALERT_EVERY_MINUTES} minutes, ${ALERT_COOLDOWN_MINUTES} minute cooldown"
echo
echo "Useful commands:"
echo "  sudo systemctl start ${APP_NAME}.service"
echo "  sudo systemctl start ${APP_NAME}-alert.service"
echo "  systemctl status ${APP_NAME}.timer"
echo "  systemctl status ${APP_NAME}-alert.timer"
echo "  journalctl -u ${APP_NAME}.service -n 80 --no-pager"
echo "  journalctl -u ${APP_NAME}-alert.service -n 80 --no-pager"

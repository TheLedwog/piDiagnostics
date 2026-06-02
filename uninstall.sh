#!/usr/bin/env bash
# Safely uninstall pi-system-report: stops/disables the systemd timers and
# services, then removes the installed program, config, and state.
#
# Standalone — you only need this one file on the Pi:
#   scp uninstall.sh pi@YOUR_PI_IP:~
#   ssh pi@YOUR_PI_IP
#   sudo bash uninstall.sh
#
# Add --yes to skip the confirmation prompt (for scripts/automation).

set -euo pipefail

APP_NAME="pi-system-report"
INSTALL_DIR="/opt/pi-system-report"
CONFIG_FILE="/etc/pi-system-report.env"
SERVICE_FILE="/etc/systemd/system/${APP_NAME}.service"
TIMER_FILE="/etc/systemd/system/${APP_NAME}.timer"
ALERT_SERVICE_FILE="/etc/systemd/system/${APP_NAME}-alert.service"
ALERT_TIMER_FILE="/etc/systemd/system/${APP_NAME}-alert.timer"
STATE_DIR="/var/lib/pi-system-report"

ASSUME_YES="0"

usage() {
  cat <<USAGE
Usage: sudo bash uninstall.sh [--yes]

Removes the pi-system-report install:
  - stops and disables the report and alert timers/services
  - removes ${SERVICE_FILE}
  - removes ${TIMER_FILE}
  - removes ${ALERT_SERVICE_FILE}
  - removes ${ALERT_TIMER_FILE}
  - removes ${CONFIG_FILE}   (your Telegram token/chat id)
  - removes ${INSTALL_DIR}
  - removes ${STATE_DIR}

Options:
  --yes        Do not ask for confirmation
  -h, --help   Show this help
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --yes|-y)
      ASSUME_YES="1"
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

# Re-run with sudo if needed so we can touch /opt, /etc, and systemd.
if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  script_path="$0"
  if [[ "$script_path" != /* ]]; then
    script_path="$(pwd)/$script_path"
  fi
  echo "This uninstaller needs sudo so it can remove files from /opt, /etc, and systemd."
  exec sudo -E bash "$script_path" "$@"
fi

# Colors when attached to a terminal.
if [[ -t 1 ]] && command -v tput >/dev/null 2>&1; then
  C_RESET="$(tput sgr0 || true)"
  C_BOLD="$(tput bold || true)"
  C_DIM="$(tput dim || true)"
  C_GREEN="$(tput setaf 2 || true)"
  C_YELLOW="$(tput setaf 3 || true)"
else
  C_RESET=""; C_BOLD=""; C_DIM=""; C_GREEN=""; C_YELLOW=""
fi

line()    { printf '%s\n' "------------------------------------------------------------"; }
header()  { printf '\n%s%s%s\n' "$C_BOLD" "$1" "$C_RESET"; line; }
step()    { printf '\n%s[%s]%s %s%s%s\n' "$C_DIM" "$1" "$C_RESET" "$C_BOLD" "$2" "$C_RESET"; }
info()    { printf '%s%s%s\n' "$C_DIM" "$1" "$C_RESET"; }
warn()    { printf '%s%s%s\n' "$C_YELLOW" "$1" "$C_RESET"; }
success() { printf '%s%s%s\n' "$C_GREEN" "$1" "$C_RESET"; }

# Show what exists so the user knows what will actually be removed.
header "Raspberry Pi System Report Uninstaller"

found_any="0"
for target in \
  "$TIMER_FILE" "$SERVICE_FILE" \
  "$ALERT_TIMER_FILE" "$ALERT_SERVICE_FILE" \
  "$CONFIG_FILE" "$INSTALL_DIR" "$STATE_DIR"; do
  if [[ -e "$target" ]]; then
    info "will remove: $target"
    found_any="1"
  fi
done

if [[ "$found_any" == "0" ]]; then
  warn "Nothing found to remove — pi-system-report does not appear to be installed."
  info "Cleaning up any leftover systemd unit state anyway."
fi

if [[ "$ASSUME_YES" != "1" && "$found_any" == "1" ]]; then
  warn ""
  warn "This permanently deletes the above, including your saved Telegram token."
  printf 'Continue? [y/N] '
  read -r answer
  case "$answer" in
    y|Y|yes|YES) ;;
    *) info "Aborted. Nothing was changed."; exit 0 ;;
  esac
fi

step "1/3" "Stopping and disabling services and timers"
if command -v systemctl >/dev/null 2>&1; then
  # disable --now also stops the units; ignore errors if they don't exist.
  systemctl disable --now "${APP_NAME}.timer"        >/dev/null 2>&1 || true
  systemctl disable --now "${APP_NAME}-alert.timer"  >/dev/null 2>&1 || true
  systemctl stop          "${APP_NAME}.service"      >/dev/null 2>&1 || true
  systemctl stop          "${APP_NAME}-alert.service" >/dev/null 2>&1 || true
else
  warn "systemctl not found; skipping service/timer stop."
fi

step "2/3" "Removing installed files"
rm -f "$SERVICE_FILE" "$TIMER_FILE" "$ALERT_SERVICE_FILE" "$ALERT_TIMER_FILE"
rm -f "$CONFIG_FILE"
rm -rf "$INSTALL_DIR" "$STATE_DIR"

step "3/3" "Reloading systemd"
if command -v systemctl >/dev/null 2>&1; then
  systemctl daemon-reload
  systemctl reset-failed \
    "${APP_NAME}.service" "${APP_NAME}.timer" \
    "${APP_NAME}-alert.service" "${APP_NAME}-alert.timer" >/dev/null 2>&1 || true
fi

success ""
success "Uninstalled ${APP_NAME}."
info "Your local copies of pi_system_report.py, install.sh, and uninstall.sh were not touched."

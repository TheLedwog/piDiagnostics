# Raspberry Pi System Report

This gives your Raspberry Pi a small health monitor that can send Telegram messages.

It can:

- Send a full system report on a schedule
- Send automatic Telegram alerts when CPU or RAM goes over your chosen limit
- Show the programs using the most CPU
- Show RAM, swap, storage, temperature, throttling, uptime, IP address, and more

The program uses only Python's standard library, so there are no extra Python packages to install.

## Files

- `pi_system_report.py` is the monitor/report program
- `install.sh` installs it as scheduled Raspberry Pi services

## Quick Install

Copy both files to your Raspberry Pi:

```bash
scp pi_system_report.py install.sh pi@YOUR_PI_IP:~
```

SSH into the Pi:

```bash
ssh pi@YOUR_PI_IP
```

Run the installer:

```bash
sudo bash install.sh
```

During installation it will ask for:

- Your Telegram bot token
- Your Telegram chat ID, or it will try to find it for you
- How often to send full reports
- How often to check CPU/RAM alerts
- CPU alert percent, default `60`
- RAM alert percent, default `60`
- Alert cooldown, default `30` minutes

You can also provide everything in one command:

```bash
sudo bash install.sh \
  --token "123456:your-token" \
  --chat-id "123456789" \
  --every-minutes 30 \
  --alert-every-minutes 5 \
  --cpu-alert-percent 60 \
  --ram-alert-percent 60 \
  --alert-cooldown 30
```

## Telegram Setup

1. Open Telegram and message `@BotFather`.
2. Run `/newbot`.
3. Follow the prompts and copy the bot token.
4. Start a chat with your new bot.
5. Send the bot any message, such as `hello`.
6. Run `sudo bash install.sh` on the Pi and paste the token when asked.

The installer will try to find your chat ID automatically after you message the bot.

If auto-detection does not work, open this URL in a browser, replacing `TOKEN` with your bot token:

```text
https://api.telegram.org/botTOKEN/getUpdates
```

Look for:

```text
"chat":{"id":123456789
```

Use that number as the Telegram chat ID.

## What Gets Installed

The installer creates:

- Program: `/opt/pi-system-report/pi_system_report.py`
- Settings: `/etc/pi-system-report.env`
- Full report service: `pi-system-report.service`
- Full report timer: `pi-system-report.timer`
- Alert service: `pi-system-report-alert.service`
- Alert timer: `pi-system-report-alert.timer`

By default:

- A full report is sent every 30 minutes
- CPU/RAM alerts are checked every 5 minutes
- An alert is sent if CPU reaches 60% or RAM reaches 60%
- Repeated alerts are limited by a 30 minute cooldown

## Adjust Settings Later

The easiest way is to run the installer again:

```bash
sudo bash install.sh
```

It will keep existing values unless you type new ones.

You can also edit:

```bash
sudo nano /etc/pi-system-report.env
```

Useful settings:

```bash
PI_REPORT_CPU_ALERT_PERCENT="60"
PI_REPORT_RAM_ALERT_PERCENT="60"
PI_REPORT_ALERT_EVERY_MINUTES="5"
PI_REPORT_ALERT_COOLDOWN_MINUTES="30"
PI_REPORT_EVERY_MINUTES="30"
PI_REPORT_DURATION="30"
PI_REPORT_TOP="10"
```

After editing settings, restart the timers:

```bash
sudo systemctl daemon-reload
sudo systemctl restart pi-system-report.timer
sudo systemctl restart pi-system-report-alert.timer
```

## Useful Commands

Send a full report now:

```bash
sudo systemctl start pi-system-report.service
```

Check CPU/RAM alerts now:

```bash
sudo systemctl start pi-system-report-alert.service
```

Check timer status:

```bash
systemctl status pi-system-report.timer
systemctl status pi-system-report-alert.timer
```

View logs:

```bash
journalctl -u pi-system-report.service -n 80 --no-pager
journalctl -u pi-system-report-alert.service -n 80 --no-pager
```

## Run Without Installing

Print a report in the terminal:

```bash
python3 pi_system_report.py --duration 30
```

Send one report to Telegram:

```bash
PI_REPORT_TELEGRAM_BOT_TOKEN="123456:your-token" \
PI_REPORT_TELEGRAM_CHAT_ID="123456789" \
python3 pi_system_report.py --duration 30 --send-telegram
```

Run one alert check manually:

```bash
PI_REPORT_TELEGRAM_BOT_TOKEN="123456:your-token" \
PI_REPORT_TELEGRAM_CHAT_ID="123456789" \
python3 pi_system_report.py --duration 30 --alert-only --cpu-alert-percent 60 --ram-alert-percent 60
```

## Notes

Peak CPU and peak RAM mean the highest value seen while the script is running. For example, `--duration 30` means the report watches the Pi for 30 seconds, then reports the highest usage seen during those 30 seconds.

Temperature and throttling details use Raspberry Pi OS tools when available. If those tools are missing, the rest of the report still works.

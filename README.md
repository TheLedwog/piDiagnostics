# Raspberry Pi System Report

`pi_system_report.py` is a no-dependency Python program that reports Raspberry Pi health and resource usage.

It shows:

- Raspberry Pi model, OS, kernel, uptime, CPU, cores, and IP address
- Current, average, and peak CPU usage during the sample window
- Current and peak RAM/swap usage during the sample window
- CPU temperature, frequency, and Raspberry Pi throttling flags when available
- Storage total/free/used for mounted disks
- Network received/sent during the sample window
- Programs using the most CPU, with PID, CPU percent, RAM, and command

## Run It On The Pi

Copy `pi_system_report.py` to your Raspberry Pi, then run:

```bash
python3 pi_system_report.py
```

Monitor for a longer window:

```bash
python3 pi_system_report.py --duration 60
```

Show more CPU-heavy programs:

```bash
python3 pi_system_report.py --duration 30 --top 20
```

Save a JSON report:

```bash
python3 pi_system_report.py --duration 30 --json > pi-report.json
```

Send the report to Telegram:

```bash
PI_REPORT_TELEGRAM_BOT_TOKEN="123456:your-token" \
PI_REPORT_TELEGRAM_CHAT_ID="123456789" \
python3 pi_system_report.py --duration 30 --send-telegram
```

## Installer

Copy both files to your Raspberry Pi:

- `pi_system_report.py`
- `install.sh`

Then run:

```bash
sudo bash install.sh
```

The installer will:

- Copy the program to `/opt/pi-system-report/pi_system_report.py`
- Ask for your Telegram bot token and chat ID
- Save settings to `/etc/pi-system-report.env`
- Create a systemd service and timer
- Send the first Telegram report immediately
- Keep sending reports on the schedule you choose

Non-interactive install example:

```bash
sudo bash install.sh \
  --token "123456:your-token" \
  --chat-id "123456789" \
  --every-minutes 30 \
  --duration 30
```

Useful commands after installation:

```bash
sudo systemctl start pi-system-report.service
systemctl status pi-system-report.timer
journalctl -u pi-system-report.service -n 80 --no-pager
```

## Telegram Setup

1. In Telegram, message `@BotFather`, create a bot with `/newbot`, and copy the bot token.
2. Open a chat with your new bot and send it any message, such as `hello`.
3. Find your chat ID. One common way is to open this URL in a browser, replacing `TOKEN` with your bot token:

```text
https://api.telegram.org/botTOKEN/getUpdates
```

Look for `"chat":{"id":...}` in the response. Use that number as the chat ID.

## Notes

Peak CPU and peak memory are the highest values seen while the script is running. For example, `--duration 60` means "show me the highest usage seen during this 60 second sample."

For temperature and throttling details, Raspberry Pi OS usually includes `vcgencmd`. If it is unavailable, the rest of the report still works.

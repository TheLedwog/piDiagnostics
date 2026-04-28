#!/usr/bin/env python3
"""
Raspberry Pi system report and lightweight monitor.

This script avoids third-party packages so it can run on a fresh Raspberry Pi:

    python3 pi_system_report.py --duration 30
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import datetime as dt
import io
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


PROC = Path("/proc")
SYS = Path("/sys")
TELEGRAM_MESSAGE_LIMIT = 3900
DEFAULT_ALERT_STATE_FILE = "/var/lib/pi-system-report/alert-state.json"


@dataclasses.dataclass
class CpuTimes:
    idle: int
    total: int


@dataclasses.dataclass
class ProcessTimes:
    pid: int
    name: str
    command: str
    user_time: int
    system_time: int
    rss_bytes: int

    @property
    def total_time(self) -> int:
        return self.user_time + self.system_time


def read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


def run_command(command: list[str]) -> str | None:
    try:
        result = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def format_bytes(value: int | float | None) -> str:
    if value is None:
        return "unknown"
    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
    number = float(value)
    for unit in units:
        if abs(number) < 1024.0 or unit == units[-1]:
            if unit == "B":
                return f"{number:.0f} {unit}"
            return f"{number:.1f} {unit}"
        number /= 1024.0
    return f"{number:.1f} PiB"


def format_duration(seconds: float | int | None) -> str:
    if seconds is None:
        return "unknown"
    seconds = int(seconds)
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours or parts:
        parts.append(f"{hours}h")
    if minutes or parts:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return " ".join(parts)


def percent(used: int | float, total: int | float) -> float:
    if total <= 0:
        return 0.0
    return round((used / total) * 100.0, 1)


def parse_os_release() -> dict[str, str]:
    info: dict[str, str] = {}
    text = read_text(Path("/etc/os-release"))
    if not text:
        return info
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        info[key] = value.strip().strip('"')
    return info


def get_pi_model() -> str:
    model = read_text(SYS / "firmware/devicetree/base/model")
    if model:
        return model.replace("\x00", "").strip()
    cpuinfo = read_text(PROC / "cpuinfo")
    if cpuinfo:
        for line in cpuinfo.splitlines():
            if line.startswith("Model"):
                return line.split(":", 1)[1].strip()
            if line.startswith("Hardware"):
                return line.split(":", 1)[1].strip()
    return platform.machine() or "unknown"


def get_cpu_name() -> str:
    cpuinfo = read_text(PROC / "cpuinfo")
    if cpuinfo:
        for wanted in ("model name", "Hardware", "Processor"):
            for line in cpuinfo.splitlines():
                if line.lower().startswith(wanted.lower()):
                    return line.split(":", 1)[1].strip()
    processor = platform.processor()
    return processor or "unknown"


def get_uptime_seconds() -> float | None:
    text = read_text(PROC / "uptime")
    if not text:
        return None
    try:
        return float(text.split()[0])
    except (IndexError, ValueError):
        return None


def get_ip_addresses() -> list[str]:
    addresses: set[str] = set()
    output = run_command(["hostname", "-I"])
    if output:
        addresses.update(part for part in output.split() if part)

    try:
        hostname = socket.gethostname()
        for family, _, _, _, sockaddr in socket.getaddrinfo(hostname, None):
            if family == socket.AF_INET:
                address = sockaddr[0]
                if not address.startswith("127."):
                    addresses.add(address)
    except OSError:
        pass

    return sorted(addresses)


def read_cpu_times() -> CpuTimes | None:
    text = read_text(PROC / "stat")
    if not text:
        return None
    first_line = text.splitlines()[0]
    parts = first_line.split()
    if not parts or parts[0] != "cpu":
        return None
    try:
        values = [int(value) for value in parts[1:]]
    except ValueError:
        return None
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    total = sum(values)
    return CpuTimes(idle=idle, total=total)


def cpu_usage_between(before: CpuTimes | None, after: CpuTimes | None) -> float | None:
    if before is None or after is None:
        return None
    idle_delta = after.idle - before.idle
    total_delta = after.total - before.total
    if total_delta <= 0:
        return None
    usage = (1.0 - (idle_delta / total_delta)) * 100.0
    return round(max(0.0, min(100.0, usage)), 1)


def read_load_average() -> dict[str, float] | None:
    try:
        one, five, fifteen = os.getloadavg()
    except (AttributeError, OSError):
        return None
    return {"1m": round(one, 2), "5m": round(five, 2), "15m": round(fifteen, 2)}


def read_memory() -> dict[str, int]:
    result: dict[str, int] = {}
    text = read_text(PROC / "meminfo")
    if text:
        for line in text.splitlines():
            parts = line.split()
            if len(parts) >= 2:
                key = parts[0].rstrip(":")
                try:
                    result[key] = int(parts[1]) * 1024
                except ValueError:
                    continue

    if not result:
        try:
            usage = shutil.disk_usage("/")
        except OSError:
            usage = None
        if usage:
            result["MemTotal"] = 0
            result["MemAvailable"] = 0

    return result


def memory_summary() -> dict[str, Any]:
    mem = read_memory()
    total = mem.get("MemTotal", 0)
    available = mem.get("MemAvailable", mem.get("MemFree", 0))
    used = max(0, total - available)
    swap_total = mem.get("SwapTotal", 0)
    swap_free = mem.get("SwapFree", 0)
    swap_used = max(0, swap_total - swap_free)
    return {
        "total_bytes": total,
        "used_bytes": used,
        "available_bytes": available,
        "used_percent": percent(used, total) if total else 0.0,
        "swap_total_bytes": swap_total,
        "swap_used_bytes": swap_used,
        "swap_used_percent": percent(swap_used, swap_total) if swap_total else 0.0,
    }


def read_temperature_c() -> float | None:
    candidates = [
        SYS / "class/thermal/thermal_zone0/temp",
        SYS / "class/hwmon/hwmon0/temp1_input",
    ]
    for path in candidates:
        text = read_text(path)
        if not text:
            continue
        try:
            return round(float(text) / 1000.0, 1)
        except ValueError:
            pass

    output = run_command(["vcgencmd", "measure_temp"])
    if output:
        match = re.search(r"([-+]?\d+(?:\.\d+)?)", output)
        if match:
            return round(float(match.group(1)), 1)
    return None


def read_cpu_frequency_mhz() -> float | None:
    text = read_text(SYS / "devices/system/cpu/cpu0/cpufreq/scaling_cur_freq")
    if text:
        try:
            return round(float(text) / 1000.0, 1)
        except ValueError:
            pass

    output = run_command(["vcgencmd", "measure_clock", "arm"])
    if output and "=" in output:
        try:
            return round(float(output.split("=", 1)[1]) / 1_000_000.0, 1)
        except ValueError:
            pass
    return None


def read_throttling() -> dict[str, Any] | None:
    output = run_command(["vcgencmd", "get_throttled"])
    if not output or "=" not in output:
        return None
    raw = output.split("=", 1)[1].strip()
    try:
        value = int(raw, 16)
    except ValueError:
        return {"raw": raw, "flags": ["could not parse throttling value"]}

    flags = {
        0: "under-voltage now",
        1: "ARM frequency capped now",
        2: "currently throttled",
        3: "soft temperature limit now",
        16: "under-voltage occurred",
        17: "ARM frequency cap occurred",
        18: "throttling occurred",
        19: "soft temperature limit occurred",
    }
    active = [label for bit, label in flags.items() if value & (1 << bit)]
    return {"raw": raw, "flags": active or ["no throttling flags set"]}


def list_mounts() -> list[str]:
    mounts: list[str] = []
    text = read_text(PROC / "mounts")
    if not text:
        return ["/"]

    seen: set[str] = set()
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        device, mount_point, fs_type = parts[:3]
        if mount_point in seen:
            continue
        if fs_type in {
            "proc",
            "sysfs",
            "tmpfs",
            "devtmpfs",
            "devpts",
            "cgroup",
            "cgroup2",
            "overlay",
            "squashfs",
            "securityfs",
            "pstore",
            "debugfs",
            "tracefs",
            "fusectl",
            "configfs",
        }:
            continue
        if not (device.startswith("/dev/") or device.startswith("UUID=") or device.startswith("LABEL=")):
            continue
        seen.add(mount_point)
        mounts.append(mount_point.replace("\\040", " "))
    return mounts or ["/"]


def disk_summary() -> list[dict[str, Any]]:
    disks: list[dict[str, Any]] = []
    for mount_point in list_mounts():
        try:
            usage = shutil.disk_usage(mount_point)
        except OSError:
            continue
        disks.append(
            {
                "mount": mount_point,
                "total_bytes": usage.total,
                "used_bytes": usage.used,
                "free_bytes": usage.free,
                "used_percent": percent(usage.used, usage.total),
            }
        )
    return disks


def read_network_counters() -> dict[str, dict[str, int]]:
    text = read_text(PROC / "net/dev")
    if not text:
        return {}
    counters: dict[str, dict[str, int]] = {}
    for line in text.splitlines()[2:]:
        if ":" not in line:
            continue
        name, data = line.split(":", 1)
        parts = data.split()
        if len(parts) < 16:
            continue
        try:
            counters[name.strip()] = {
                "rx_bytes": int(parts[0]),
                "rx_packets": int(parts[1]),
                "tx_bytes": int(parts[8]),
                "tx_packets": int(parts[9]),
            }
        except ValueError:
            continue
    return counters


def parse_process_stat(path: Path) -> tuple[str, list[str]] | None:
    text = read_text(path)
    if not text:
        return None
    left = text.find("(")
    right = text.rfind(")")
    if left == -1 or right == -1 or right <= left:
        return None
    name = text[left + 1 : right]
    fields = text[right + 2 :].split()
    return name, fields


def read_process(pid: int) -> ProcessTimes | None:
    proc_dir = PROC / str(pid)
    parsed = parse_process_stat(proc_dir / "stat")
    if not parsed:
        return None
    name, fields = parsed
    try:
        # Fields after the process name start at stat field 3.
        user_time = int(fields[11])
        system_time = int(fields[12])
        rss_pages = int(fields[21])
    except (IndexError, ValueError):
        return None

    page_size = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096
    command = read_text(proc_dir / "cmdline")
    if command:
        command = command.replace("\x00", " ").strip()
    else:
        command = name

    return ProcessTimes(
        pid=pid,
        name=name,
        command=command,
        user_time=user_time,
        system_time=system_time,
        rss_bytes=max(0, rss_pages * page_size),
    )


def snapshot_processes() -> dict[int, ProcessTimes]:
    if not PROC.exists():
        return {}
    processes: dict[int, ProcessTimes] = {}
    for entry in PROC.iterdir():
        if not entry.name.isdigit():
            continue
        process = read_process(int(entry.name))
        if process:
            processes[process.pid] = process
    return processes


def process_cpu_percent(
    before: ProcessTimes,
    after: ProcessTimes,
    total_cpu_delta: int,
    cpu_count: int,
) -> float:
    if total_cpu_delta <= 0:
        return 0.0
    process_delta = after.total_time - before.total_time
    # /proc/stat total time includes all CPUs, so multiplying by cpu_count gives
    # a top-like value where one saturated core is roughly 100%.
    return round(max(0.0, (process_delta / total_cpu_delta) * 100.0 * cpu_count), 1)


def top_processes(
    before_processes: dict[int, ProcessTimes],
    after_processes: dict[int, ProcessTimes],
    before_cpu: CpuTimes | None,
    after_cpu: CpuTimes | None,
    limit: int,
) -> list[dict[str, Any]]:
    if before_cpu is None or after_cpu is None:
        return []
    total_cpu_delta = after_cpu.total - before_cpu.total
    cpu_count = os.cpu_count() or 1
    rows: list[dict[str, Any]] = []
    for pid, after in after_processes.items():
        before = before_processes.get(pid)
        if not before:
            continue
        rows.append(
            {
                "pid": pid,
                "name": after.name,
                "command": after.command,
                "cpu_percent": process_cpu_percent(before, after, total_cpu_delta, cpu_count),
                "memory_rss_bytes": after.rss_bytes,
            }
        )
    rows.sort(key=lambda item: (item["cpu_percent"], item["memory_rss_bytes"]), reverse=True)
    return rows[:limit]


def split_telegram_message(text: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    if not text:
        return [""]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        cut_at = remaining.rfind("\n", 0, limit)
        if cut_at < limit // 2:
            cut_at = limit
        chunks.append(remaining[:cut_at].rstrip())
        remaining = remaining[cut_at:].lstrip()
    chunks.append(remaining)
    return chunks


def send_telegram_message(
    bot_token: str,
    chat_id: str,
    text: str,
    *,
    silent: bool = False,
) -> int:
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    chunks = split_telegram_message(text)

    for index, chunk in enumerate(chunks, start=1):
        message = chunk
        if len(chunks) > 1:
            message = f"Raspberry Pi report ({index}/{len(chunks)})\n\n{chunk}"

        payload = urllib.parse.urlencode(
            {
                "chat_id": chat_id,
                "text": message,
                "disable_web_page_preview": "true",
                "disable_notification": "true" if silent else "false",
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "pi-system-report/1.0",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                raw_body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            raw_body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Telegram HTTP {exc.code}: {raw_body}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Telegram connection failed: {exc.reason}") from exc

        try:
            body = json.loads(raw_body)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Telegram returned invalid JSON: {raw_body}") from exc
        if not body.get("ok"):
            description = body.get("description", "unknown Telegram API error")
            raise RuntimeError(str(description))

    return len(chunks)


def read_json_file(path: Path) -> dict[str, Any]:
    text = read_text(path)
    if not text:
        return {}
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def write_json_file(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    temporary_path.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")
    temporary_path.replace(path)


def env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def get_telegram_credentials(args: argparse.Namespace) -> tuple[str | None, str | None]:
    bot_token = (
        args.telegram_token
        or os.getenv("PI_REPORT_TELEGRAM_BOT_TOKEN")
        or os.getenv("TELEGRAM_BOT_TOKEN")
    )
    chat_id = (
        args.telegram_chat_id
        or os.getenv("PI_REPORT_TELEGRAM_CHAT_ID")
        or os.getenv("TELEGRAM_CHAT_ID")
    )
    return bot_token, chat_id


def collect_static_info() -> dict[str, Any]:
    os_release = parse_os_release()
    uname = platform.uname()
    return {
        "timestamp": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "hostname": socket.gethostname(),
        "model": get_pi_model(),
        "os": os_release.get("PRETTY_NAME") or platform.platform(),
        "kernel": uname.release,
        "architecture": uname.machine,
        "python": sys.version.split()[0],
        "cpu_name": get_cpu_name(),
        "cpu_cores": os.cpu_count() or 0,
        "uptime_seconds": get_uptime_seconds(),
        "ip_addresses": get_ip_addresses(),
    }


def monitor(duration: float, interval: float, top_n: int) -> dict[str, Any]:
    duration = max(0.1, duration)
    interval = max(0.2, interval)

    start_cpu = read_cpu_times()
    previous_cpu = start_cpu
    previous_processes = snapshot_processes()
    first_processes = previous_processes
    start_network = read_network_counters()

    max_cpu_percent = 0.0
    max_memory_percent = 0.0
    max_swap_percent = 0.0
    max_temperature_c: float | None = None
    cpu_samples: list[float] = []
    memory_samples: list[float] = []

    deadline = time.monotonic() + duration
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(interval, remaining))

        current_cpu = read_cpu_times()
        current_memory = memory_summary()
        current_temperature = read_temperature_c()

        cpu_percent = cpu_usage_between(previous_cpu, current_cpu)
        if cpu_percent is not None:
            cpu_samples.append(cpu_percent)
            max_cpu_percent = max(max_cpu_percent, cpu_percent)
        memory_samples.append(current_memory["used_percent"])
        max_memory_percent = max(max_memory_percent, current_memory["used_percent"])
        max_swap_percent = max(max_swap_percent, current_memory["swap_used_percent"])
        if current_temperature is not None:
            max_temperature_c = (
                current_temperature
                if max_temperature_c is None
                else max(max_temperature_c, current_temperature)
            )

        previous_cpu = current_cpu
        previous_processes = snapshot_processes()

    end_cpu = read_cpu_times()
    end_processes = snapshot_processes()
    end_network = read_network_counters()

    avg_cpu = round(sum(cpu_samples) / len(cpu_samples), 1) if cpu_samples else None
    avg_memory = round(sum(memory_samples) / len(memory_samples), 1) if memory_samples else None

    network_delta: dict[str, dict[str, int]] = {}
    for name, end_values in end_network.items():
        start_values = start_network.get(name)
        if not start_values:
            continue
        network_delta[name] = {
            "rx_bytes": max(0, end_values["rx_bytes"] - start_values["rx_bytes"]),
            "tx_bytes": max(0, end_values["tx_bytes"] - start_values["tx_bytes"]),
        }

    return {
        "duration_seconds": round(duration, 1),
        "interval_seconds": round(interval, 1),
        "cpu": {
            "current_percent": cpu_usage_between(start_cpu, end_cpu),
            "average_percent": avg_cpu,
            "max_percent": round(max_cpu_percent, 1),
            "load_average": read_load_average(),
            "frequency_mhz": read_cpu_frequency_mhz(),
            "temperature_c": read_temperature_c(),
            "max_temperature_c": max_temperature_c,
            "throttling": read_throttling(),
        },
        "memory": {
            **memory_summary(),
            "average_used_percent": avg_memory,
            "max_used_percent": round(max_memory_percent, 1),
            "max_swap_used_percent": round(max_swap_percent, 1),
        },
        "disk": disk_summary(),
        "network_delta": network_delta,
        "top_processes_by_cpu": top_processes(
            first_processes or previous_processes,
            end_processes,
            start_cpu,
            end_cpu,
            top_n,
        ),
    }


def print_section(title: str) -> None:
    print(f"\n{title}")
    print("-" * len(title))


def print_report(report: dict[str, Any]) -> None:
    info = report["system"]
    metrics = report["metrics"]

    print_section("System")
    print(f"Time:         {info['timestamp']}")
    print(f"Hostname:     {info['hostname']}")
    print(f"Model:        {info['model']}")
    print(f"OS:           {info['os']}")
    print(f"Kernel:       {info['kernel']}")
    print(f"Architecture: {info['architecture']}")
    print(f"Python:       {info['python']}")
    print(f"Uptime:       {format_duration(info['uptime_seconds'])}")
    print(f"IP address:   {', '.join(info['ip_addresses']) or 'unknown'}")

    print_section("CPU")
    cpu = metrics["cpu"]
    load = cpu["load_average"] or {}
    throttling = cpu["throttling"] or {"raw": "unknown", "flags": ["vcgencmd unavailable"]}
    print(f"CPU:          {info['cpu_name']}")
    print(f"Cores:        {info['cpu_cores']}")
    print(f"Current use:  {cpu['current_percent'] if cpu['current_percent'] is not None else 'unknown'}%")
    print(f"Average use:  {cpu['average_percent'] if cpu['average_percent'] is not None else 'unknown'}%")
    print(f"Peak use:     {cpu['max_percent']}% during {metrics['duration_seconds']}s sample")
    print(f"Load avg:     1m {load.get('1m', 'unknown')}, 5m {load.get('5m', 'unknown')}, 15m {load.get('15m', 'unknown')}")
    print(f"Frequency:    {cpu['frequency_mhz'] if cpu['frequency_mhz'] is not None else 'unknown'} MHz")
    print(f"Temperature:  {cpu['temperature_c'] if cpu['temperature_c'] is not None else 'unknown'} C")
    print(f"Peak temp:    {cpu['max_temperature_c'] if cpu['max_temperature_c'] is not None else 'unknown'} C")
    print(f"Throttling:   {throttling['raw']} ({'; '.join(throttling['flags'])})")

    print_section("Memory")
    memory = metrics["memory"]
    print(f"RAM total:    {format_bytes(memory['total_bytes'])}")
    print(f"RAM used:     {format_bytes(memory['used_bytes'])} ({memory['used_percent']}%)")
    print(f"RAM free:     {format_bytes(memory['available_bytes'])}")
    print(f"Peak RAM use: {memory['max_used_percent']}% during sample")
    print(f"Swap used:    {format_bytes(memory['swap_used_bytes'])} / {format_bytes(memory['swap_total_bytes'])} ({memory['swap_used_percent']}%)")

    print_section("Storage")
    for disk in metrics["disk"]:
        print(
            f"{disk['mount']}: {format_bytes(disk['free_bytes'])} free / "
            f"{format_bytes(disk['total_bytes'])} total ({disk['used_percent']}% used)"
        )

    print_section("Network During Sample")
    network_delta = metrics["network_delta"]
    if not network_delta:
        print("No network counters found.")
    else:
        for name, values in sorted(network_delta.items()):
            print(f"{name}: received {format_bytes(values['rx_bytes'])}, sent {format_bytes(values['tx_bytes'])}")

    print_section("Top Programs By CPU")
    processes = metrics["top_processes_by_cpu"]
    if not processes:
        print("No process CPU data found. On Raspberry Pi OS this usually works from /proc.")
        return
    header = f"{'PID':>7} {'CPU%':>7} {'RAM':>10}  COMMAND"
    print(header)
    print("-" * len(header))
    for process in processes:
        command = process["command"]
        if len(command) > 90:
            command = command[:87] + "..."
        print(
            f"{process['pid']:>7} "
            f"{process['cpu_percent']:>7.1f} "
            f"{format_bytes(process['memory_rss_bytes']):>10}  "
            f"{command}"
        )


def render_report(report: dict[str, Any]) -> str:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        print_report(report)
    return buffer.getvalue().rstrip()


def find_alerts(report: dict[str, Any], cpu_threshold: float, ram_threshold: float) -> list[dict[str, Any]]:
    metrics = report["metrics"]
    alerts: list[dict[str, Any]] = []

    cpu_percent = metrics["cpu"]["max_percent"]
    if cpu_percent >= cpu_threshold:
        alerts.append(
            {
                "type": "cpu",
                "label": "CPU",
                "value": cpu_percent,
                "threshold": cpu_threshold,
            }
        )

    ram_percent = metrics["memory"]["max_used_percent"]
    if ram_percent >= ram_threshold:
        alerts.append(
            {
                "type": "ram",
                "label": "RAM",
                "value": ram_percent,
                "threshold": ram_threshold,
            }
        )

    return alerts


def render_alert(report: dict[str, Any], alerts: list[dict[str, Any]]) -> str:
    info = report["system"]
    metrics = report["metrics"]
    cpu = metrics["cpu"]
    memory = metrics["memory"]
    lines = [
        "Raspberry Pi alert",
        "",
        f"Host: {info['hostname']}",
        f"Time: {info['timestamp']}",
        f"Sample: {metrics['duration_seconds']}s",
        "",
        "Triggered:",
    ]

    for alert in alerts:
        lines.append(f"- {alert['label']} hit {alert['value']}% (limit {alert['threshold']}%)")

    lines.extend(
        [
            "",
            f"CPU current: {cpu['current_percent'] if cpu['current_percent'] is not None else 'unknown'}%",
            f"CPU average: {cpu['average_percent'] if cpu['average_percent'] is not None else 'unknown'}%",
            f"CPU peak: {cpu['max_percent']}%",
            f"RAM used: {format_bytes(memory['used_bytes'])} / {format_bytes(memory['total_bytes'])} ({memory['used_percent']}%)",
            f"RAM peak: {memory['max_used_percent']}%",
        ]
    )

    if cpu["temperature_c"] is not None:
        lines.append(f"Temperature: {cpu['temperature_c']} C")
    if info["ip_addresses"]:
        lines.append(f"IP: {', '.join(info['ip_addresses'])}")

    processes = metrics["top_processes_by_cpu"][:5]
    if processes:
        lines.extend(["", "Top CPU programs:"])
        for process in processes:
            command = process["command"]
            if len(command) > 70:
                command = command[:67] + "..."
            lines.append(
                f"- PID {process['pid']}: {process['cpu_percent']}% CPU, "
                f"{format_bytes(process['memory_rss_bytes'])} RAM, {command}"
            )

    return "\n".join(lines)


def should_send_alert(state: dict[str, Any], cooldown_minutes: int, now: float) -> tuple[bool, int]:
    last_alert_at = state.get("last_alert_at")
    if not isinstance(last_alert_at, (int, float)):
        return True, 0
    cooldown_seconds = max(0, cooldown_minutes) * 60
    next_allowed_at = int(last_alert_at + cooldown_seconds)
    if now >= next_allowed_at:
        return True, 0
    return False, max(0, next_allowed_at - int(now))


def handle_alert_mode(args: argparse.Namespace, report: dict[str, Any]) -> int:
    bot_token, chat_id = get_telegram_credentials(args)
    if not bot_token or not chat_id:
        print(
            "Alert mode needs PI_REPORT_TELEGRAM_BOT_TOKEN and "
            "PI_REPORT_TELEGRAM_CHAT_ID, or --telegram-token and --telegram-chat-id.",
            file=sys.stderr,
        )
        return 2

    alerts = find_alerts(report, args.cpu_alert_percent, args.ram_alert_percent)
    if not alerts:
        print(
            "No alert sent. "
            f"CPU peak {report['metrics']['cpu']['max_percent']}% "
            f"and RAM peak {report['metrics']['memory']['max_used_percent']}% "
            "are below the configured limits."
        )
        return 0

    now = time.time()
    state_path = Path(args.alert_state_file)
    state = read_json_file(state_path)
    can_send, wait_seconds = should_send_alert(state, args.alert_cooldown_minutes, now)
    if not can_send:
        print(
            "Alert threshold is still breached, but no Telegram message was sent "
            f"because cooldown has {format_duration(wait_seconds)} remaining."
        )
        return 0

    message = render_alert(report, alerts)
    try:
        message_count = send_telegram_message(
            bot_token,
            chat_id,
            message,
            silent=args.telegram_silent,
        )
    except RuntimeError as exc:
        print(f"Telegram alert failed: {exc}", file=sys.stderr)
        return 1

    state["last_alert_at"] = now
    state["last_alerts"] = alerts
    state["last_hostname"] = report["system"]["hostname"]
    try:
        write_json_file(state_path, state)
    except OSError as exc:
        print(f"Telegram alert sent, but state file could not be saved: {exc}", file=sys.stderr)
        return 1

    print(f"Telegram alert sent ({message_count} message{'s' if message_count != 1 else ''}).")
    return 0


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "system": collect_static_info(),
        "metrics": monitor(args.duration, args.interval, args.top),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Show Raspberry Pi system health, resource peaks, storage, and CPU-heavy programs."
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=10.0,
        help="How many seconds to monitor before printing the report. Default: 10.",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=1.0,
        help="Sampling interval in seconds. Default: 1.",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=10,
        help="How many top CPU-using programs to show. Default: 10.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON instead of the text report.",
    )
    parser.add_argument(
        "--send-telegram",
        action="store_true",
        help="Send the text report to Telegram using credentials from flags or environment variables.",
    )
    parser.add_argument(
        "--telegram-token",
        help="Telegram bot token. You can also set PI_REPORT_TELEGRAM_BOT_TOKEN.",
    )
    parser.add_argument(
        "--telegram-chat-id",
        help="Telegram chat ID. You can also set PI_REPORT_TELEGRAM_CHAT_ID.",
    )
    parser.add_argument(
        "--telegram-silent",
        action="store_true",
        help="Send Telegram messages without a notification sound.",
    )
    parser.add_argument(
        "--alert-only",
        action="store_true",
        help="Only send Telegram if CPU or RAM crosses the alert limits.",
    )
    parser.add_argument(
        "--cpu-alert-percent",
        type=float,
        default=env_float("PI_REPORT_CPU_ALERT_PERCENT", 60.0),
        help="CPU percent that triggers Telegram alerts. Default: 60.",
    )
    parser.add_argument(
        "--ram-alert-percent",
        type=float,
        default=env_float("PI_REPORT_RAM_ALERT_PERCENT", 60.0),
        help="RAM percent that triggers Telegram alerts. Default: 60.",
    )
    parser.add_argument(
        "--alert-cooldown-minutes",
        type=int,
        default=env_int("PI_REPORT_ALERT_COOLDOWN_MINUTES", 30),
        help="Minimum minutes between alert messages while usage stays high. Default: 30.",
    )
    parser.add_argument(
        "--alert-state-file",
        default=os.getenv("PI_REPORT_ALERT_STATE_FILE", DEFAULT_ALERT_STATE_FILE),
        help=f"Where alert cooldown state is stored. Default: {DEFAULT_ALERT_STATE_FILE}.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.top < 1:
        print("--top must be at least 1", file=sys.stderr)
        return 2
    if not 0 <= args.cpu_alert_percent <= 100:
        print("--cpu-alert-percent must be between 0 and 100", file=sys.stderr)
        return 2
    if not 0 <= args.ram_alert_percent <= 100:
        print("--ram-alert-percent must be between 0 and 100", file=sys.stderr)
        return 2
    if args.alert_cooldown_minutes < 0:
        print("--alert-cooldown-minutes must be 0 or greater", file=sys.stderr)
        return 2

    report = build_report(args)
    text_report = render_report(report)

    if args.alert_only:
        if args.json:
            print(json.dumps(report, indent=2, sort_keys=True))
        return handle_alert_mode(args, report)

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(text_report)

    if args.send_telegram:
        bot_token, chat_id = get_telegram_credentials(args)
        if not bot_token or not chat_id:
            print(
                "Telegram sending needs PI_REPORT_TELEGRAM_BOT_TOKEN and "
                "PI_REPORT_TELEGRAM_CHAT_ID, or --telegram-token and --telegram-chat-id.",
                file=sys.stderr,
            )
            return 2
        try:
            message_count = send_telegram_message(
                bot_token,
                chat_id,
                text_report,
                silent=args.telegram_silent,
            )
        except RuntimeError as exc:
            print(f"Telegram send failed: {exc}", file=sys.stderr)
            return 1
        print(f"Telegram report sent ({message_count} message{'s' if message_count != 1 else ''}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

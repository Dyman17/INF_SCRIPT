import ctypes
import datetime as dt
import json
import platform
import socket
import argparse
import urllib.error
import urllib.request
from ctypes import wintypes

import psutil


OUTPUT_FILE = "processes.json"
CDP_PORTS = (9222, 9223, 9333, 9444)


def utc_now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def enum_visible_windows():
    user32 = ctypes.windll.user32
    windows = []

    EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def _callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True

        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True

        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        title = buffer.value.strip()
        if not title:
            return True

        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))

        windows.append(
            {
                "hwnd": int(hwnd),
                "pid": int(pid.value),
                "title": title,
            }
        )
        return True

    user32.EnumWindows(EnumWindowsProc(_callback), 0)
    return windows


def collect_visible_window_titles():
    """
    Returns visible window titles (deduped, in first-seen order).
    """
    titles = []
    seen = set()
    for w in enum_visible_windows():
        title = (w.get("title") or "").strip()
        if not title:
            continue
        key = title.lower()
        if key in seen:
            continue
        seen.add(key)
        titles.append(title)
    return titles


def get_active_window_array():
    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return []

    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        title = ""
    else:
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        title = buffer.value.strip()

    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))

    # По требованию поле должно быть массивом
    return [
        {
            "hwnd": int(hwnd),
            "pid": int(pid.value),
            "title": title,
        }
    ]


def get_active_window_title():
    active = get_active_window_array()
    if not active:
        return ""
    return (active[0].get("title") or "").strip()


def collect_process_names():
    """
    Returns a list of running process names like ["chrome.exe", "explorer.exe", ...].
    De-duplicates case-insensitively while preserving first-seen order.
    """
    names = []
    seen = set()
    for proc in psutil.process_iter(["name"]):
        try:
            name = (proc.info.get("name") or "").strip()
            if not name:
                continue
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
            names.append(name)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return names


def collect_processes(window_map):
    rows = []
    attrs = ["pid", "name", "username", "cpu_percent", "memory_info", "exe", "create_time", "cmdline"]

    for proc in psutil.process_iter(attrs):
        try:
            info = proc.info
            memory_mb = None
            if info.get("memory_info") is not None:
                memory_mb = round(info["memory_info"].rss / (1024 * 1024), 2)

            create_time = None
            if info.get("create_time"):
                create_time = dt.datetime.fromtimestamp(info["create_time"], tz=dt.timezone.utc).isoformat()

            rows.append(
                {
                    "pid": info.get("pid"),
                    "name": info.get("name"),
                    "user": info.get("username"),
                    "cpu_percent": info.get("cpu_percent"),
                    "memory_mb": memory_mb,
                    "exe": info.get("exe"),
                    "create_time_utc": create_time,
                    "cmdline": info.get("cmdline") or [],
                    "window_titles": window_map.get(info.get("pid"), []),
                }
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

    return rows


def safe_http_json(url, timeout=0.6):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return json.loads(response.read().decode(charset, errors="replace"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def collect_tabs_from_cdp():
    tabs = []
    seen = set()

    for port in CDP_PORTS:
        version = safe_http_json(f"http://127.0.0.1:{port}/json/version")
        pages = safe_http_json(f"http://127.0.0.1:{port}/json")
        if not isinstance(pages, list):
            continue

        browser_name = "unknown"
        if isinstance(version, dict):
            browser_name = version.get("Browser", "unknown")

        for item in pages:
            if not isinstance(item, dict):
                continue
            if item.get("type") != "page":
                continue

            url = item.get("url", "")
            title = item.get("title", "")
            if not url:
                continue

            key = (port, url, title)
            if key in seen:
                continue
            seen.add(key)

            tabs.append(
                {
                    "source": "cdp",
                    "browser": browser_name,
                    "port": port,
                    "title": title,
                    "url": url,
                    "id": item.get("id"),
                }
            )

    return tabs


def resolve_domains_for_browser_processes(processes):
    browser_names = {"chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe", "opera_gx.exe"}
    browser_pids = {p["pid"] for p in processes if (p.get("name") or "").lower() in browser_names}
    domains = []

    for conn in psutil.net_connections(kind="tcp"):
        if conn.pid not in browser_pids:
            continue
        if not conn.raddr:
            continue
        ip = conn.raddr.ip
        port = conn.raddr.port
        try:
            host = socket.gethostbyaddr(ip)[0]
        except (socket.herror, socket.gaierror, OSError):
            host = ip

        domains.append(
            {
                "pid": conn.pid,
                "remote_ip": ip,
                "remote_port": port,
                "resolved_host": host,
                "status": conn.status,
            }
        )

    # Убираем дубликаты
    unique = []
    seen = set()
    for item in domains:
        key = (item["pid"], item["remote_ip"], item["remote_port"], item["resolved_host"], item["status"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)

    return unique


def main_full(output_file=OUTPUT_FILE):
    windows = enum_visible_windows()
    active_window = get_active_window_array()

    window_map = {}
    for w in windows:
        window_map.setdefault(w["pid"], []).append(w["title"])

    processes = collect_processes(window_map)
    cdp_tabs = collect_tabs_from_cdp()
    browser_connections = resolve_domains_for_browser_processes(processes)

    payload = {
        "generated_at_utc": utc_now_iso(),
        "host": {
            "node": platform.node(),
            "platform": platform.platform(),
            "python_version": platform.python_version(),
        },
        "summary": {
            "process_count": len(processes),
            "visible_window_count": len(windows),
            "active_window_count": len(active_window),
            "open_tabs_via_cdp_count": len(cdp_tabs),
            "browser_connection_count": len(browser_connections),
        },
        "processes": processes,
        "visible_windows": windows,
        "active_window": active_window,
        "open_tabs_via_cdp": cdp_tabs,
        "browser_network_activity": browser_connections,
        "notes": [
            "В этом JSON только текущее состояние системы на момент запуска скрипта.",
            "Поле active_window всегда массив: обычно 0 или 1 элемент.",
            "Поле open_tabs_via_cdp показывает реально открытые вкладки, но только если браузер запущен с remote-debugging портом.",
            "Поле browser_network_activity показывает активные сетевые подключения браузеров и помогает определить текущие сайты по доменам.",
        ],
    }

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"[ok] saved full snapshot -> {output_file}")
    print(
        f"Процессы: {len(processes)} | Окна: {len(windows)} | Active: {len(active_window)} | "
        f"Open tabs (CDP): {len(cdp_tabs)} | Browser connections: {len(browser_connections)}"
    )


def build_stdout_collector_payload():
    """
    Stdout-only payload for the C# app (no logs, only JSON on stdout).
    """
    try:
        payload = {
            "activeWindow": get_active_window_title(),
            "windows": collect_visible_window_titles(),
            "processes": collect_process_names(),
            "debug": {"source": "python-collector", "message": "ok"},
        }
    except Exception as exc:
        payload = {
            "activeWindow": "",
            "windows": [],
            "processes": [],
            "debug": {
                "source": "python-collector",
                "message": f"error: {type(exc).__name__}",
                "error": str(exc),
            },
        }
    return payload


def main_stdout():
    # The C# app reads only stdout, so do not print logs before/after this JSON.
    print(json.dumps(build_stdout_collector_payload(), ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description="Process/window collector (stdout JSON by default).")
    parser.add_argument(
        "--full",
        action="store_true",
        help=f"Write full snapshot JSON to file (default: {OUTPUT_FILE}) and print status logs.",
    )
    parser.add_argument("--output-file", default=OUTPUT_FILE, help="Output JSON file path for --full mode.")
    args = parser.parse_args()

    if args.full:
        main_full(output_file=args.output_file)
        return

    main_stdout()


if __name__ == "__main__":
    main()

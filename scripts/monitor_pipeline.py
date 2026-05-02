import argparse
import ctypes
import datetime as dt
import json
import os
import platform
import re
import socket
import time
import urllib.error
import urllib.request
from ctypes import wintypes
from pathlib import Path

import psutil


BASE_DIR = Path(__file__).resolve().parents[1]
OUTPUT_DIR = BASE_DIR / "output"
RAW_JSON_PATH = OUTPUT_DIR / "processes.json"
NORMALIZED_JSON_PATH = OUTPUT_DIR / "process_titles.json"
CLEAN_JSON_PATH = OUTPUT_DIR / "clean_process_titles.json"
CDP_PORTS = (9222, 9223, 9333, 9444)
BROWSER_WINDOW_SUFFIXES = (
    " - google chrome",
    " - chromium",
    " - microsoft edge",
    " - brave",
    " - opera",
    " - opera gx",
)
BROWSER_PROCESS_NAMES = {
    "chrome.exe",
    "msedge.exe",
    "firefox.exe",
    "brave.exe",
    "opera.exe",
    "opera_gx.exe",
}


def utc_now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def ensure_output_dir():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def save_json(path, payload):
    ensure_output_dir()
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def load_json(path):
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


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

    return [{"hwnd": int(hwnd), "pid": int(pid.value), "title": title}]


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


def resolve_cdp_ports():
    raw_ports = (os.getenv("CDP_PORTS") or "").strip()
    if not raw_ports:
        return CDP_PORTS

    parsed = []
    for chunk in raw_ports.split(","):
        token = chunk.strip()
        if not token:
            continue
        try:
            port = int(token)
        except ValueError:
            continue
        if 1 <= port <= 65535:
            parsed.append(port)

    if not parsed:
        return CDP_PORTS

    # Preserve order while removing duplicates.
    return tuple(dict.fromkeys(parsed))


def normalize_for_match(text):
    return re.sub(r"\s+", " ", (text or "").strip()).lower()


def split_browser_window_title(window_title):
    raw = (window_title or "").strip()
    if not raw:
        return "", False

    # Chrome may prefix title with unread counter like "(3) YouTube - Google Chrome".
    without_counter = re.sub(r"^\(\d+\)\s*", "", raw).strip()
    lowered = without_counter.lower()

    for suffix in BROWSER_WINDOW_SUFFIXES:
        if lowered.endswith(suffix):
            tab_title = without_counter[: -len(suffix)].strip(" -\t")
            return tab_title, True

    return without_counter, False


def collect_selected_browser_titles(visible_windows):
    selected = set()

    for row in visible_windows:
        title = (row.get("title") or "").strip()
        tab_title, is_browser_title = split_browser_window_title(title)
        if not is_browser_title:
            continue

        normalized = normalize_for_match(tab_title)
        if normalized:
            selected.add(normalized)

    return selected


def get_foreground_browser_title(active_window):
    if not active_window:
        return ""

    raw_title = (active_window[0].get("title") or "").strip()
    tab_title, is_browser_title = split_browser_window_title(raw_title)
    if not is_browser_title:
        return ""

    return normalize_for_match(tab_title)


def safe_http_json(url, timeout=0.6):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return json.loads(response.read().decode(charset, errors="replace"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def collect_tabs_from_cdp(visible_windows, active_window):
    tabs = []
    seen = set()
    selected_titles = collect_selected_browser_titles(visible_windows)
    foreground_title = get_foreground_browser_title(active_window)
    foreground_marked = False

    for port in resolve_cdp_ports():
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

            norm_title = normalize_for_match(title)
            key = (port, url, norm_title)
            if key in seen:
                continue
            seen.add(key)

            is_active = bool(norm_title and norm_title in selected_titles)
            is_foreground = False
            if foreground_title and norm_title == foreground_title and not foreground_marked:
                is_foreground = True
                foreground_marked = True

            tabs.append(
                {
                    "source": "cdp",
                    "browser": browser_name,
                    "port": port,
                    "title": title,
                    "url": url,
                    "id": item.get("id"),
                    "is_active": is_active,
                    "is_foreground": is_foreground,
                }
            )

    return tabs


def resolve_domains_for_browser_processes(processes):
    browser_pids = {p["pid"] for p in processes if (p.get("name") or "").lower() in BROWSER_PROCESS_NAMES}
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

    unique = []
    seen = set()
    for item in domains:
        key = (item["pid"], item["remote_ip"], item["remote_port"], item["resolved_host"], item["status"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)

    return unique


def build_process_snapshot():
    windows = enum_visible_windows()
    active_window = get_active_window_array()

    window_map = {}
    for w in windows:
        window_map.setdefault(w["pid"], []).append(w["title"])

    processes = collect_processes(window_map)
    cdp_tabs = collect_tabs_from_cdp(visible_windows=windows, active_window=active_window)
    browser_connections = resolve_domains_for_browser_processes(processes)
    chrome_process_count = sum(1 for p in processes if (p.get("name") or "").lower() == "chrome.exe")
    cdp_warning = None
    if chrome_process_count > 0 and not cdp_tabs:
        cdp_warning = (
            "Chrome is running, but CDP tabs are unavailable. "
            "Restart Chrome with --remote-debugging-port=9222 to collect all tabs."
        )
    checked_ports = list(resolve_cdp_ports())

    return {
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
            "chrome_process_count": chrome_process_count,
            "cdp_ports_checked": checked_ports,
            "cdp_warning": cdp_warning,
        },
        "processes": processes,
        "visible_windows": windows,
        "active_window": active_window,
        "open_tabs_via_cdp": cdp_tabs,
        "browser_network_activity": browser_connections,
    }


def build_normalized_payload(snapshot):
    records = []
    next_id = 1
    seen = set()
    active_titles = {
        (w.get("title") or "").strip()
        for w in snapshot.get("active_window", [])
        if (w.get("title") or "").strip()
    }

    for proc in snapshot.get("processes", []):
        pid = proc.get("pid")
        app_name = proc.get("name")
        titles = proc.get("window_titles") or []

        for title in titles:
            clean_title = (title or "").strip()
            if not clean_title:
                continue

            dedup_key = ("window", pid, app_name, clean_title)
            if dedup_key in seen:
                continue
            seen.add(dedup_key)

            records.append(
                {
                    "id": next_id,
                    "source": "window",
                    "process_pid": pid,
                    "app_name": app_name,
                    "title": clean_title,
                    "url": None,
                    "is_active": clean_title in active_titles,
                }
            )
            next_id += 1

    for tab in snapshot.get("open_tabs_via_cdp", []):
        title = (tab.get("title") or "").strip()
        url = (tab.get("url") or "").strip() or None
        browser_name = tab.get("browser")

        if not title and not url:
            continue

        dedup_key = ("cdp_tab", browser_name, title, url)
        if dedup_key in seen:
            continue
        seen.add(dedup_key)

        records.append(
            {
                "id": next_id,
                "source": "cdp_tab",
                "process_pid": None,
                "app_name": browser_name,
                "title": title or "(no title)",
                "url": url,
                "is_active": bool(tab.get("is_active")),
                "is_foreground": bool(tab.get("is_foreground")),
            }
        )
        next_id += 1

    return {
        "generated_at_utc": utc_now_iso(),
        "source_snapshot_time_utc": snapshot.get("generated_at_utc"),
        "host": snapshot.get("host", {}),
        "active_window": snapshot.get("active_window", []),
        "summary": {
            "records_count": len(records),
            "window_records_count": sum(1 for r in records if r["source"] == "window"),
            "cdp_tab_records_count": sum(1 for r in records if r["source"] == "cdp_tab"),
            "active_window_count": len(snapshot.get("active_window", [])),
            "active_records_count": sum(1 for r in records if r.get("is_active")),
        },
        "records": records,
    }


def build_clean_payload(normalized, dedupe=False):
    result = {
        "generated_at_utc": normalized.get("generated_at_utc"),
        "active_window": [],
        "records": [],
    }

    seen_active = set()
    window_index = {}
    for r in normalized.get("records", []):
        if (r.get("source") or "").strip() != "window":
            continue
        title = (r.get("title") or "").strip()
        pid = r.get("process_pid")
        app_name = (r.get("app_name") or "").strip() or None
        if not title:
            continue
        window_index[(title, pid)] = app_name

    for w in normalized.get("active_window", []):
        title = (w.get("title") or "").strip()
        pid = w.get("pid")
        if not title:
            continue
        if dedupe and title in seen_active:
            continue
        seen_active.add(title)
        active_row = {"title": title}
        app_name = window_index.get((title, pid))
        if app_name:
            active_row["app_name"] = app_name
        result["active_window"].append(active_row)

    seen_records = {}
    for r in normalized.get("records", []):
        title = (r.get("title") or "").strip()
        url = r.get("url")
        source = (r.get("source") or "").strip() or None
        app_name = (r.get("app_name") or "").strip() or None
        is_active = bool(r.get("is_active"))

        if not title and not url:
            continue

        key = (source, title, url, app_name)
        if dedupe and key in seen_records:
            idx = seen_records[key]
            if is_active:
                result["records"][idx]["is_active"] = True
            continue

        clean_row = {
            "title": title or None,
            "url": url,
            "source": source,
            "is_active": is_active,
        }
        if app_name:
            clean_row["app_name"] = app_name

        result["records"].append(clean_row)
        seen_records[key] = len(result["records"]) - 1

    return result


def collect_stage():
    snapshot = build_process_snapshot()
    save_json(RAW_JSON_PATH, snapshot)
    print(f"[collect] saved -> {RAW_JSON_PATH}")
    return snapshot


def normalize_stage(snapshot=None):
    if snapshot is None:
        snapshot = load_json(RAW_JSON_PATH)
    normalized = build_normalized_payload(snapshot)
    save_json(NORMALIZED_JSON_PATH, normalized)
    print(f"[normalize] saved -> {NORMALIZED_JSON_PATH}")
    return normalized


def clean_stage(normalized=None, dedupe=False):
    if normalized is None:
        normalized = load_json(NORMALIZED_JSON_PATH)
    clean = build_clean_payload(normalized, dedupe=dedupe)
    save_json(CLEAN_JSON_PATH, clean)
    print(f"[clean] saved -> {CLEAN_JSON_PATH}")
    return clean


def run_all(dedupe=False):
    snapshot = collect_stage()
    warning = (snapshot.get("summary") or {}).get("cdp_warning")
    if warning:
        print(f"[warn] {warning}")
    normalized = normalize_stage(snapshot=snapshot)
    clean_stage(normalized=normalized, dedupe=dedupe)
    print("[ok] pipeline complete")


def main():
    parser = argparse.ArgumentParser(description="Unified pipeline: collect -> normalize -> clean")
    parser.add_argument(
        "stage",
        nargs="?",
        default="all",
        choices=["collect", "normalize", "clean", "all"],
        help="Stage to run (default: all).",
    )
    parser.add_argument("--dedupe", action="store_true", help="Remove duplicate title/url in clean stage.")
    parser.add_argument("--loop", action="store_true", help="Run selected stage in loop.")
    parser.add_argument("--interval", type=int, default=300, help="Loop interval in seconds (default: 300).")
    args = parser.parse_args()

    if args.interval < 1:
        raise ValueError("--interval must be >= 1")

    def run_selected():
        if args.stage == "collect":
            collect_stage()
        elif args.stage == "normalize":
            normalize_stage()
        elif args.stage == "clean":
            clean_stage(dedupe=args.dedupe)
        else:
            run_all(dedupe=args.dedupe)

    if not args.loop:
        run_selected()
        return

    cycle = 1
    while True:
        print(f"\n=== cycle {cycle} ===")
        run_selected()
        print(f"[wait] next run in {args.interval} sec")
        time.sleep(args.interval)
        cycle += 1


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nstopped by user")

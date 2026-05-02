import argparse
import datetime as dt
import json
from pathlib import Path


DEFAULT_INPUT = "processes.json"
DEFAULT_OUTPUT = "process_titles.json"


def utc_now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def build_records(snapshot):
    records = []
    next_id = 1
    seen = set()

    # 1) Заголовки окон из процессов (текущее состояние)
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
                }
            )
            next_id += 1

    # 2) Вкладки из CDP (если браузер запущен с remote debugging)
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
            }
        )
        next_id += 1

    return records


def normalize_snapshot(input_path, output_path):
    with Path(input_path).open("r", encoding="utf-8") as f:
        snapshot = json.load(f)

    records = build_records(snapshot)

    output = {
        "generated_at_utc": utc_now_iso(),
        "source_snapshot_time_utc": snapshot.get("generated_at_utc"),
        "host": snapshot.get("host", {}),
        "active_window": snapshot.get("active_window", []),
        "summary": {
            "records_count": len(records),
            "window_records_count": sum(1 for r in records if r["source"] == "window"),
            "cdp_tab_records_count": sum(1 for r in records if r["source"] == "cdp_tab"),
            "active_window_count": len(snapshot.get("active_window", [])),
        },
        "records": records,
    }

    with Path(output_path).open("w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"Готово. Нормализованные данные сохранены в {output_path}")
    print(
        "Всего записей: "
        f"{output['summary']['records_count']} "
        f"(window={output['summary']['window_records_count']}, "
        f"cdp_tab={output['summary']['cdp_tab_records_count']})"
    )


def main():
    parser = argparse.ArgumentParser(
        description="Фильтрует snapshot процессов и создает компактный JSON с названиями окон/вкладок и ID."
    )
    parser.add_argument("--input", default=DEFAULT_INPUT, help=f"Входной JSON (по умолчанию: {DEFAULT_INPUT})")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help=f"Выходной JSON (по умолчанию: {DEFAULT_OUTPUT})")
    args = parser.parse_args()

    normalize_snapshot(args.input, args.output)


if __name__ == "__main__":
    main()

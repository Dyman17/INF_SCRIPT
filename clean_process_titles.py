import argparse
import json
from pathlib import Path


DEFAULT_INPUT = "process_titles.json"
DEFAULT_OUTPUT = "clean_process_titles.json"


def clean_payload(data, dedupe=False):
    result = {
        "generated_at_utc": data.get("generated_at_utc"),
        "active_window": [],
        "records": [],
    }

    seen_active = set()
    for w in data.get("active_window", []):
        title = (w.get("title") or "").strip()
        if not title:
            continue
        if dedupe and title in seen_active:
            continue
        seen_active.add(title)
        result["active_window"].append({"title": title})

    seen_records = set()
    for r in data.get("records", []):
        title = (r.get("title") or "").strip()
        url = r.get("url")
        if not title and not url:
            continue

        key = (title, url)
        if dedupe and key in seen_records:
            continue
        seen_records.add(key)

        result["records"].append(
            {
                "title": title or None,
                "url": url,
            }
        )

    return result


def main():
    parser = argparse.ArgumentParser(description="Очищает process_titles.json, оставляя только текстовые поля.")
    parser.add_argument("--input", default=DEFAULT_INPUT, help=f"Входной JSON (по умолчанию: {DEFAULT_INPUT})")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help=f"Выходной JSON (по умолчанию: {DEFAULT_OUTPUT})")
    parser.add_argument("--dedupe", action="store_true", help="Убирать дубли title/url в выходном JSON.")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        raise FileNotFoundError(f"Не найден входной файл: {args.input}")

    with input_path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    cleaned = clean_payload(data, dedupe=args.dedupe)

    with Path(args.output).open("w", encoding="utf-8") as f:
        json.dump(cleaned, f, ensure_ascii=False, indent=2)

    print(f"Готово -> {args.output}")
    print(
        f"active_window: {len(cleaned['active_window'])} | "
        f"records: {len(cleaned['records'])}"
    )


if __name__ == "__main__":
    main()

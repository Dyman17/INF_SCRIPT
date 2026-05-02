import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


DEFAULT_INPUT_ROOT = Path("clean_process_titles.json")
DEFAULT_INPUT_OUTPUT_DIR = Path("output") / "clean_process_titles.json"


def pick_input_path(cli_input):
    if cli_input:
        return Path(cli_input)
    if DEFAULT_INPUT_OUTPUT_DIR.exists():
        return DEFAULT_INPUT_OUTPUT_DIR
    return DEFAULT_INPUT_ROOT


def load_titles(input_path, dedupe=True):
    with input_path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    titles = []
    for row in data.get("active_window", []):
        title = (row.get("title") or "").strip()
        if title:
            titles.append(title)

    for row in data.get("records", []):
        title = (row.get("title") or "").strip()
        if title:
            titles.append(title)

    if dedupe:
        seen = set()
        uniq = []
        for t in titles:
            if t in seen:
                continue
            seen.add(t)
            uniq.append(t)
        titles = uniq

    return titles


def post_rows(supabase_url, supabase_key, table_name, rows):
    endpoint = f"{supabase_url.rstrip('/')}/rest/v1/{table_name}"
    payload = json.dumps(rows, ensure_ascii=False).encode("utf-8")

    req = urllib.request.Request(
        endpoint,
        data=payload,
        method="POST",
        headers={
            "apikey": supabase_key,
            "Authorization": f"Bearer {supabase_key}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code}: {body}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Network error: {e}") from e


def main():
    parser = argparse.ArgumentParser(description="Отправляет только title-значения в таблицу Supabase с колонкой 'name'.")
    parser.add_argument("--input", help="Путь к clean_process_titles.json")
    parser.add_argument("--table", default="name", help="Имя таблицы (по умолчанию: name)")
    parser.add_argument("--no-dedupe", action="store_true", help="Не удалять дубли title перед отправкой.")
    args = parser.parse_args()

    supabase_url = os.getenv("SUPABASE_URL")
    supabase_key = os.getenv("SUPABASE_KEY")

    if not supabase_url or not supabase_key:
        print("Нужны переменные окружения: SUPABASE_URL и SUPABASE_KEY")
        sys.exit(1)

    input_path = pick_input_path(args.input)
    if not input_path.exists():
        print(f"Входной JSON не найден: {input_path}")
        sys.exit(1)

    titles = load_titles(input_path, dedupe=not args.no_dedupe)
    rows = [{"name": t} for t in titles]

    if not rows:
        print("Нет title для отправки.")
        return

    status = post_rows(supabase_url, supabase_key, args.table, rows)
    print(f"Успешно отправлено: {len(rows)} строк в таблицу '{args.table}' (HTTP {status})")


if __name__ == "__main__":
    main()

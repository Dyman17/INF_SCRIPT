import argparse
import getpass
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path


DEFAULT_INPUT_ROOT = Path("clean_process_titles.json")
DEFAULT_INPUT_OUTPUT_DIR = Path("output") / "clean_process_titles.json"
DEFAULT_TABLE = "activity_logs"
DEFAULT_ENV_FILE = Path(".env.local")
# Фолбэк логина ученика (ИИН) для local/dev.
DEFAULT_STUDENT_LOGIN = ""
APP_DISPLAY_NAMES = {
    "code.exe": "Visual Studio Code",
    "chrome.exe": "Google Chrome",
    "msedge.exe": "Microsoft Edge",
    "firefox.exe": "Mozilla Firefox",
    "brave.exe": "Brave",
    "opera.exe": "Opera",
    "opera_gx.exe": "Opera GX",
}


def pick_input_path(cli_input):
    if cli_input:
        return Path(cli_input)
    if DEFAULT_INPUT_OUTPUT_DIR.exists():
        return DEFAULT_INPUT_OUTPUT_DIR
    return DEFAULT_INPUT_ROOT


def load_clean_json(input_path):
    with input_path.open("r", encoding="utf-8") as f:
        return json.load(f)


def load_env_file(env_path):
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")

        if key and key not in os.environ:
            os.environ[key] = value


def resolve_account_name(cli_account_name=None):
    return (
        (cli_account_name or "").strip()
        or (os.getenv("ACCOUNT_NAME") or "").strip()
        or (os.getenv("STUDENT_LOGIN") or "").strip()
        or (os.getenv("USER_LOGIN") or "").strip()
        or (os.getenv("USERNAME") or "").strip()
        or (os.getenv("USER") or "").strip()
        or getpass.getuser()
    )


def resolve_student_login(cli_student_login=None, cli_account_name=None):
    return (
        (cli_student_login or "").strip()
        or (os.getenv("STUDENT_LOGIN") or "").strip()
        or (cli_account_name or "").strip()
        or (os.getenv("ACCOUNT_NAME") or "").strip()
        or (os.getenv("USER_LOGIN") or "").strip()
        or (os.getenv("USERNAME") or "").strip()
        or (os.getenv("USER") or "").strip()
        or getpass.getuser()
    )


def validate_student_login(student_login):
    login = (student_login or "").strip()
    if not login:
        raise ValueError("student_login is empty")

    strict_iin = (os.getenv("STRICT_STUDENT_LOGIN_IIN") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if strict_iin and not re.fullmatch(r"\d{12}", login):
        raise ValueError("student_login must be 12 digits when STRICT_STUDENT_LOGIN_IIN=1")

    return login


def to_app_display_name(app_name):
    raw = (app_name or "").strip()
    if not raw:
        return ""
    return APP_DISPLAY_NAMES.get(raw.lower(), "")


def title_to_app_fallback(title):
    raw = (title or "").strip()
    if not raw:
        return ""
    if " - " in raw:
        return raw.rsplit(" - ", 1)[-1].strip()
    return raw


def build_activity_payload(clean_data, student_login, account_name=None, dedupe=True):
    active_window_title = ""
    active_window_app_name = ""
    active = clean_data.get("active_window", [])
    if active and isinstance(active, list):
        first = active[0] if isinstance(active[0], dict) else {}
        active_window_title = (first.get("title") or "").strip()
        active_window_app_name = to_app_display_name(first.get("app_name"))

    process_list = []
    for row in clean_data.get("records", []):
        title = (row.get("title") or "").strip()
        url = (row.get("url") or "").strip() or None
        source = (row.get("source") or "").strip() or None
        app_name = (row.get("app_name") or "").strip() or None
        is_active = bool(row.get("is_active"))

        name = title or url
        if not name:
            continue

        process_item = {
            "name": name,
            "is_active": is_active,
        }
        if url:
            process_item["url"] = url
        if source:
            process_item["source"] = source
        if app_name:
            process_item["app_name"] = app_name

        process_list.append(process_item)

    if dedupe and process_list:
        deduped = []
        index_by_key = {}
        for row in process_list:
            key = (
                row.get("name"),
                row.get("url"),
                row.get("source"),
                row.get("app_name"),
            )
            if key in index_by_key:
                idx = index_by_key[key]
                if row.get("is_active"):
                    deduped[idx]["is_active"] = True
                continue

            index_by_key[key] = len(deduped)
            deduped.append(row)
        process_list = deduped

    effective_active_window = (
        active_window_app_name
        or title_to_app_fallback(active_window_title)
        or active_window_title
        or (account_name or "").strip()
        or None
    )

    return [
        {
            "student_login": student_login,
            "active_window": effective_active_window,
            "process_list": process_list,
        }
    ]


def post_to_supabase(supabase_url, api_key, bearer_token, table, payload):
    endpoint = f"{supabase_url.rstrip('/')}/rest/v1/{table}"
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    req = urllib.request.Request(
        endpoint,
        data=body,
        method="POST",
        headers={
            "apikey": api_key,
            "Authorization": f"Bearer {bearer_token}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            resp_body = resp.read().decode("utf-8", errors="replace")
            return resp.status, resp_body
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code}: {err}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Network error: {e}") from e


def resolve_api_key(cli_jwt=None):
    # Если есть пользовательский JWT, приоритет у anon ключа (RLS-кейс).
    user_jwt = cli_jwt or os.getenv("SUPABASE_JWT") or os.getenv("ACCESS_TOKEN")
    if user_jwt:
        return (
            os.getenv("ANON_KEY")
            or os.getenv("SUPABASE_ANON_KEY")
            or os.getenv("SUPABASE_KEY")
            or os.getenv("SERVICE_ROLE")
            or os.getenv("SUPABASE_SERVICE_ROLE_KEY")
        )

    # Если JWT нет, приоритет у service_role (серверный/prod-кейс, как в curl).
    return (
        os.getenv("SERVICE_ROLE")
        or os.getenv("SUPABASE_SERVICE_ROLE_KEY")
        or os.getenv("SUPABASE_KEY")
        or os.getenv("ANON_KEY")
    )


def resolve_bearer_token(api_key, cli_jwt=None):
    # Для RLS с anon ключом здесь должен быть JWT авторизованного пользователя.
    return cli_jwt or os.getenv("SUPABASE_JWT") or os.getenv("ACCESS_TOKEN") or api_key


def print_json_safe(payload):
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    try:
        print(text)
    except UnicodeEncodeError:
        sys.stdout.buffer.write((text + "\n").encode("utf-8", errors="replace"))


def print_text_safe(text):
    try:
        print(text)
    except UnicodeEncodeError:
        sys.stdout.buffer.write((str(text) + "\n").encode("utf-8", errors="replace"))


def main():
    parser = argparse.ArgumentParser(
        description="Отправляет clean_process_titles.json в Supabase table activity_logs."
    )
    parser.add_argument("--input", help="Путь к clean_process_titles.json")
    parser.add_argument("--table", default=DEFAULT_TABLE, help=f"Таблица Supabase (по умолчанию: {DEFAULT_TABLE})")
    parser.add_argument(
        "--student-login",
        default=os.getenv("STUDENT_LOGIN", DEFAULT_STUDENT_LOGIN),
        help="Логин ученика (ИИН) для activity_logs.student_login.",
    )
    parser.add_argument("--no-dedupe", action="store_true", help="Не убирать дубли title в process_list")
    parser.add_argument("--dry-run", action="store_true", help="Только вывести payload, без POST")
    parser.add_argument("--jwt", help="JWT пользователя Supabase (для RLS с anon key)")
    parser.add_argument("--account-name", help="Имя учетки для поля active_window")
    parser.add_argument("--env-file", default=str(DEFAULT_ENV_FILE), help="Путь к локальному env-файлу")
    args = parser.parse_args()

    load_env_file(Path(args.env_file))

    supabase_url = os.getenv("SUPABASE_URL")
    api_key = resolve_api_key(cli_jwt=args.jwt)
    bearer_token = resolve_bearer_token(api_key, cli_jwt=args.jwt)
    input_path = pick_input_path(args.input)

    if not input_path.exists():
        print(f"Входной JSON не найден: {input_path}")
        sys.exit(1)

    student_login = resolve_student_login(args.student_login, args.account_name)
    try:
        student_login = validate_student_login(student_login)
    except ValueError as exc:
        print(f"Ошибка student_login: {exc}")
        print("Передай --student-login или выстави STUDENT_LOGIN/ACCOUNT_NAME в env.")
        sys.exit(1)

    clean_data = load_clean_json(input_path)
    account_name = resolve_account_name(args.account_name)
    payload = build_activity_payload(
        clean_data,
        student_login,
        account_name=account_name,
        dedupe=not args.no_dedupe,
    )

    if args.dry_run:
        print_json_safe(payload)
        return

    if not supabase_url or not api_key or not bearer_token:
        print(
            "Нужны env: SUPABASE_URL и ключ "
            "(SERVICE_ROLE или SUPABASE_SERVICE_ROLE_KEY или SUPABASE_KEY или ANON_KEY). "
            "Для anon+RLS нужен JWT пользователя: --jwt или SUPABASE_JWT."
        )
        sys.exit(1)

    status, response_body = post_to_supabase(supabase_url, api_key, bearer_token, args.table, payload)
    print(f"Успешно: HTTP {status}")
    print_text_safe(response_body)


if __name__ == "__main__":
    main()

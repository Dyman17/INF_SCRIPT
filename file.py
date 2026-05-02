import argparse
import os
import sys
import time
from pathlib import Path

import push_activity_logs as push
from scripts import monitor_pipeline


DEFAULT_INTERVAL = 5
DEFAULT_ENV_FILE = ".env.local"


def run_cycle(args, dedupe):
    monitor_pipeline.run_all(dedupe=dedupe)

    input_path = push.pick_input_path(args.input)
    if not input_path.exists():
        raise FileNotFoundError(f"Input JSON not found: {input_path}")

    clean_data = push.load_clean_json(input_path)
    student_login = push.validate_student_login(
        push.resolve_student_login(args.student_login, args.account_name)
    )

    account_name = push.resolve_account_name(args.account_name)
    payload = push.build_activity_payload(
        clean_data,
        student_login,
        account_name=account_name,
        dedupe=dedupe,
    )

    supabase_url = os.getenv("SUPABASE_URL")
    api_key = push.resolve_api_key(cli_jwt=args.jwt)
    bearer_token = push.resolve_bearer_token(api_key, cli_jwt=args.jwt)
    if not supabase_url or not api_key or not bearer_token:
        raise RuntimeError(
            "Missing SUPABASE_URL and key "
            "(SERVICE_ROLE or SUPABASE_SERVICE_ROLE_KEY or SUPABASE_KEY or ANON_KEY). "
            "For anon+RLS provide SUPABASE_JWT or --jwt."
        )

    status, response_body = push.post_to_supabase(
        supabase_url=supabase_url,
        api_key=api_key,
        bearer_token=bearer_token,
        table=args.table,
        payload=payload,
    )
    print(f"[push] HTTP {status}", flush=True)
    if response_body:
        print(response_body, flush=True)


def main():
    parser = argparse.ArgumentParser(
        description="Collects process activity and sends to Supabase in a loop."
    )
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL, help="Loop interval in seconds.")
    parser.add_argument("--input", help="Path to clean_process_titles.json (optional).")
    parser.add_argument("--table", default=push.DEFAULT_TABLE, help="Supabase table name.")
    parser.add_argument(
        "--student-login",
        default=os.getenv("STUDENT_LOGIN", push.DEFAULT_STUDENT_LOGIN),
        help="Student login (IIN) for activity_logs.student_login.",
    )
    parser.add_argument("--account-name", help="Account name to store in activity_logs.active_window.")
    parser.add_argument("--jwt", help="Supabase user JWT for RLS inserts with anon key.")
    parser.add_argument("--env-file", default=DEFAULT_ENV_FILE, help="Path to env file.")
    parser.add_argument("--no-dedupe", action="store_true", help="Do not dedupe process titles.")
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit.")
    args = parser.parse_args()

    if args.interval < 1:
        raise ValueError("--interval must be >= 1")

    push.load_env_file(Path(args.env_file))
    dedupe = not args.no_dedupe

    cycle = 1
    while True:
        started_at = time.time()
        print(f"\n=== cycle {cycle} ===", flush=True)
        try:
            run_cycle(args, dedupe=dedupe)
            print("[ok] cycle completed", flush=True)
        except Exception as exc:
            print(f"[error] {exc}", flush=True)
            lowered = str(exc).lower()
            if "row-level security policy" in lowered or "http 401" in lowered:
                print(
                    "[hint] Add SERVICE_ROLE key or SUPABASE_JWT (or --jwt) to pass RLS policy.",
                    flush=True,
                )

        if args.once:
            break

        elapsed = time.time() - started_at
        wait_s = max(0, args.interval - elapsed)
        print(f"[wait] next cycle in {wait_s:.1f} sec", flush=True)
        time.sleep(wait_s)
        cycle += 1


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nstopped by user", flush=True)
        sys.exit(0)
    except Exception as exc:
        print(f"\n[fatal] {exc}", flush=True)
        sys.exit(1)

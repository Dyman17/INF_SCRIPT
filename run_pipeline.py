import argparse
import subprocess
import sys
import time
from pathlib import Path


STEP_1 = "script.py"
STEP_2 = "normalize_titles.py"


def run_python_file(file_name, extra_args=None):
    path = Path(file_name)
    if not path.exists():
        raise FileNotFoundError(f"Файл не найден: {file_name}")

    print(f"\n[RUN] {file_name}", flush=True)
    cmd = [sys.executable, str(path)] + (extra_args or [])
    result = subprocess.run(cmd, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"Скрипт {file_name} завершился с ошибкой (code={result.returncode})")


def run_once():
    run_python_file(STEP_1, extra_args=["--full"])
    run_python_file(STEP_2)
    print("\n[OK] Цепочка завершена: script.py -> normalize_titles.py", flush=True)


def main():
    parser = argparse.ArgumentParser(
        description="Последовательно запускает два скрипта: script.py и normalize_titles.py."
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Запускать цепочку бесконечно с паузой между циклами.",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=300,
        help="Пауза в секундах между циклами при --loop (по умолчанию: 300).",
    )
    args = parser.parse_args()

    if args.interval < 1:
        raise ValueError("--interval должен быть >= 1")

    if not args.loop:
        run_once()
        return

    cycle = 1
    while True:
        print(f"\n===== CYCLE {cycle} =====", flush=True)
        run_once()
        print(f"[WAIT] Следующий запуск через {args.interval} сек.", flush=True)
        time.sleep(args.interval)
        cycle += 1


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nОстановлено пользователем.", flush=True)
    except Exception as exc:
        print(f"\n[ERROR] {exc}", flush=True)
        sys.exit(1)

"""Small cross-platform stress runner for GitHub Actions.

Runs ``tests/run.py`` repeatedly with a per-run timeout, writes one log per
iteration, and fails immediately on a non-zero exit or a native-lifetime
signature.  This keeps diagnostic workflows short and lets GitHub Actions
parallelize repetitions across matrix shards instead of spending hours in a
single six-hour-limited job.

Example:
    python tests/ci_stress.py --label highlights --repeat 20 --timeout 1200 -- \
        tests.test_highlights_integration.MainWindowHighlightTests
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys


DANGER_PATTERNS = (
    "QObject::killTimer",
    "Timers cannot be stopped from another thread",
    "QThread: Destroyed while thread is still running",
    "0xC0000005",
    "0xC0000374",
    "EXC_BAD_ACCESS",
    "SIGSEGV",
    "Fatal Python error",
    "Segmentation fault",
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", required=True)
    parser.add_argument("--repeat", type=int, required=True)
    parser.add_argument("--timeout", type=int, default=1200, help="seconds per test process")
    parser.add_argument("--log-dir", default="stress-logs")
    parser.add_argument("tests", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    tests = list(args.tests)
    if tests and tests[0] == "--":
        tests = tests[1:]
    if not tests:
        parser.error("at least one unittest module/class/method is required after --")

    log_dir = Path(args.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env.setdefault("PYTHONFAULTHANDLER", "1")

    for iteration in range(1, args.repeat + 1):
        log_path = log_dir / f"{args.label}-{iteration:04d}.log"
        command = [sys.executable, "tests/run.py", *tests, "-v"]
        print(f"[{args.label}] {iteration}/{args.repeat}: {' '.join(command)}", flush=True)
        try:
            completed = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
                timeout=args.timeout,
                env=env,
                check=False,
            )
            output = completed.stdout or ""
        except subprocess.TimeoutExpired as exc:
            output = (exc.stdout or "")
            if isinstance(output, bytes):
                output = output.decode(errors="replace")
            log_path.write_text(output, encoding="utf-8", errors="replace")
            print(f"::error::{args.label} iteration {iteration} timed out after {args.timeout}s")
            print(output[-8000:])
            return 124

        log_path.write_text(output, encoding="utf-8", errors="replace")

        matched = next((pattern for pattern in DANGER_PATTERNS if pattern in output), None)
        if completed.returncode != 0 or matched:
            reason = f"exit={completed.returncode}"
            if matched:
                reason += f", matched={matched!r}"
            print(f"::error::{args.label} iteration {iteration} failed ({reason})")
            print(output[-12000:])
            return completed.returncode or 1

        print(f"[{args.label}] {iteration}/{args.repeat} clean", flush=True)

    print(f"[{args.label}] all {args.repeat} runs clean", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

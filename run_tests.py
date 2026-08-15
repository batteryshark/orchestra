#!/usr/bin/env python3
"""Run the suite with one process per test module.

Every module builds its own tempdir, its own DROMOND_HOME and its own fake
servers, so nothing is shared and nothing has to run in order. Serially that
independence bought nothing; here it is the whole speedup, and the wall clock
becomes the slowest single module instead of the sum of all of them.

    uv run python run_tests.py            # everything
    uv run python run_tests.py merge http # substring match on module names
    uv run python run_tests.py -j 4       # fewer workers

Slowest-first scheduling needs last run's timings, kept in .test-times.json.
A missing or stale entry just means that module is scheduled last, which
costs a little packing efficiency and nothing else.
"""
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).parent
TIMES = ROOT / ".test-times.json"


def modules(patterns: list[str]) -> list[str]:
    found = sorted(p.stem for p in (ROOT / "tests").glob("test_*.py"))
    if not patterns:
        return found
    return [m for m in found if any(p in m for p in patterns)]


def run(module: str) -> tuple[str, int, str, float]:
    started = time.perf_counter()
    proc = subprocess.run([sys.executable, "-m", "unittest", f"tests.{module}"],
                          cwd=str(ROOT), capture_output=True, text=True)
    return module, proc.returncode, proc.stderr + proc.stdout, time.perf_counter() - started


def main() -> int:
    argv = sys.argv[1:]
    workers = os.cpu_count() or 4
    if "-j" in argv:
        at = argv.index("-j")
        workers = int(argv[at + 1])
        argv = argv[:at] + argv[at + 2:]

    targets = modules(argv)
    if not targets:
        print(f"no test modules match {argv}")
        return 1

    known = json.loads(TIMES.read_text()) if TIMES.exists() else {}
    targets.sort(key=lambda m: -known.get(m, 0.0))  # long poles start first

    wall = time.perf_counter()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(run, targets))
    wall = time.perf_counter() - wall

    ran = failed = 0
    for module, code, output, _ in results:
        for line in output.splitlines():
            if line.startswith("Ran "):
                ran += int(line.split()[1])
        if code != 0:
            failed += 1
            print(f"\n{'=' * 70}\nFAILED: {module}\n{'=' * 70}\n{output.rstrip()}")

    TIMES.write_text(json.dumps({m: round(t, 2) for m, _, _, t in results}, indent=1,
                                sort_keys=True))
    slowest = sorted(results, key=lambda r: -r[3])[:3]
    print(f"\nRan {ran} tests in {wall:.1f}s across {len(targets)} modules, "
          f"{workers} workers")
    print("slowest: " + ", ".join(f"{m} {t:.0f}s" for m, _, _, t in slowest))
    print("FAILED" if failed else "OK")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

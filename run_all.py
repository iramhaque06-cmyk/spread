#!/usr/bin/env python3
"""Run the whole pipeline in the order the website depends on.

    python run_all.py            # cached responses where still fresh
    python run_all.py --refresh  # force live requests

Steps:

1. ``fetch_venue_a.py`` — Kalshi's wider economics scan into its own table.
2. ``fetch_venue_b.py`` — Polymarket's wider economy scan into its own table.
3. ``spread.py`` — the three curated pairs, which appends price snapshots and
   writes ``index.html`` and ``site/data.json``.

Only step 3 produces the website, so only step 3 can fail the run. The two
scans are extra coverage: if a venue is down or rate-limiting, the job warns,
keeps the site publishing from what it already holds, and says so instead of
blocking the update. API terms and rate limits are respected by the cached,
paced requests inside each script - one pass per invocation, never a tight
loop.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OPTIONAL = ("fetch_venue_a.py", "fetch_venue_b.py")
REQUIRED = ("spread.py",)


def run(script: str, *, refresh: bool) -> tuple[str, int]:
    """Run one pipeline script, echoing its output as it goes."""
    command = [sys.executable, str(ROOT / script)]
    if refresh:
        command.append("--refresh")
    print(f"\n=== {' '.join(Path(part).name for part in command)} ===", flush=True)
    completed = subprocess.run(command, cwd=ROOT)
    return script, completed.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--refresh", action="store_true", help="ignore the response cache"
    )
    parser.add_argument(
        "--skip-scans",
        action="store_true",
        help="only run spread.py (the step the website needs)",
    )
    args = parser.parse_args(argv)

    warnings: list[str] = []
    for script in (() if args.skip_scans else OPTIONAL):
        _, code = run(script, refresh=args.refresh)
        if code != 0:
            warnings.append(f"{script} exited {code}")
            print(f"warning: {script} failed; continuing with the export", file=sys.stderr)

    failures: list[str] = []
    for script in REQUIRED:
        _, code = run(script, refresh=args.refresh)
        if code != 0:
            failures.append(f"{script} exited {code}")

    print("\n=== summary ===")
    print(f"required steps failed: {failures or 'none'}")
    print(f"optional scans skipped or failing: {warnings or 'none'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

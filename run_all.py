#!/usr/bin/env python3
"""Run the whole pipeline in the order the website depends on.

    python run_all.py                 # everything: the 6 hourly pass
    python run_all.py --prices-only   # the 30 minute pass
    python run_all.py --refresh       # force live requests
    python run_all.py --no-log        # do not write run_all.log

Steps, in order, because each one reads only what the previous one stored:

1. ``fetch_venue_a.py`` — Kalshi's wider economics scan into its own table.
2. ``fetch_venue_b.py`` — Polymarket's wider economy scan into its own table.
3. ``spread.py`` — the three curated pairs, which appends price snapshots and
   writes ``index.html`` and ``site/data.json``.
4. ``catalog.py`` — the multi-venue catalog: every open market listed, matched
   into events, priced, and gap-scored.
5. ``export.py`` — the static JSON under ``data/`` that the wide site reads:
   catalog shards, the search index, one file per matched event, ``home.json``.

``--prices-only`` is the 30 minute cadence: it skips the two wide scans and
tells the catalog and the exporter to refresh prices, events, ``home.json``
and the price snapshots only, leaving the catalog shards and the search index
to the 6 hourly pass that owns them. Rebuilding 175,000 sharded market records
and their token index every half hour would be wasted bandwidth for data that
barely moved.

Only ``spread.py`` is required, because only it produces the curated site. The
wide scans and the catalog are extra coverage: if a venue is down or
rate-limiting, the job warns, keeps publishing from what it already holds, and
says so instead of blocking the update. API terms and rate limits are respected
by the cached, paced requests inside each script - one pass per invocation,
never a tight loop.

Everything each step prints is echoed here and appended to ``run_all.log``, so
a scheduled run leaves a record of what it did and what it found.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOG_PATH = ROOT / "run_all.log"
OPTIONAL = ("fetch_venue_a.py", "fetch_venue_b.py")
REQUIRED = ("spread.py",)
# (script, extra arguments) for the multi-venue layer.
CATALOG_STEPS = (("catalog.py", ()), ("export.py", ()))


def run(script: str, *, refresh: bool, extra: tuple = (), log=None) -> tuple[str, int]:
    """Run one pipeline script, echoing its output as it goes.

    Output is streamed rather than captured: a scheduled run is worth watching
    live, and a step that hangs should show how far it got.
    """
    command = [sys.executable, str(ROOT / script), *extra]
    if refresh:
        command.append("--refresh")
    print(f"\n=== {' '.join(Path(part).name for part in command)} ===", flush=True)
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    for line in process.stdout:
        sys.stdout.write(line)
        if log is not None:
            log.write(line)
    process.wait()
    sys.stdout.flush()
    return script, process.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--refresh", action="store_true", help="ignore the response cache"
    )
    parser.add_argument(
        "--prices-only",
        action="store_true",
        help="the 30 minute pass: prices, events and home.json only",
    )
    parser.add_argument(
        "--skip-scans",
        action="store_true",
        help="only run spread.py (the step the website needs)",
    )
    parser.add_argument(
        "--skip-catalog",
        action="store_true",
        help="leave the multi-venue catalog and its export alone",
    )
    parser.add_argument(
        "--no-log", action="store_true", help=f"do not append to {LOG_PATH.name}"
    )
    args = parser.parse_args(argv)

    log = None
    if not args.no_log:
        log = LOG_PATH.open("a", encoding="utf-8")
        log.write(
            f"\n===== run_all.py {datetime.now(timezone.utc).isoformat(timespec='seconds')}"
            f" (prices_only={bool(args.prices_only)}) =====\n"
        )
        log.flush()

    try:
        warnings: list[str] = []
        for script in (() if (args.skip_scans or args.prices_only) else OPTIONAL):
            _, code = run(script, refresh=args.refresh, log=log)
            if code != 0:
                warnings.append(f"{script} exited {code}")
                print(f"warning: {script} failed; continuing with the export", file=sys.stderr)

        failures: list[str] = []
        for script in REQUIRED:
            _, code = run(script, refresh=args.refresh, log=log)
            if code != 0:
                failures.append(f"{script} exited {code}")

        for script, extra in () if args.skip_catalog else CATALOG_STEPS:
            chosen = list(extra)
            if args.prices_only:
                chosen.append("--prices-only")
            _, code = run(script, refresh=args.refresh, extra=tuple(chosen), log=log)
            if code != 0:
                failures.append(f"{script} exited {code}")

        print("\n=== summary ===")
        print(f"cadence: {'30 minute prices-only pass' if args.prices_only else '6 hourly full pass'}")
        print(f"required steps failed: {failures or 'none'}")
        print(f"optional scans skipped or failing: {warnings or 'none'}")
        return 1 if failures else 0
    finally:
        if log is not None:
            log.close()


if __name__ == "__main__":
    raise SystemExit(main())

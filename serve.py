#!/usr/bin/env python3
"""Serve the Spread site locally, with caching switched off.

    python serve.py            # http://127.0.0.1:8765
    python serve.py 9000       # a different port

``python -m http.server`` sends no ``Cache-Control`` header, so a browser
applies heuristic freshness: a page you edited two minutes ago can still
render from the copy it already had, and the mistake looks like a bug in
your code. Every response here says ``no-cache``, which is free locally and
means what you see is what is on disk. It serves nothing outside ``site/``.

Standard library only, read-only, one thread per request (see CLAUDE.md).
"""

from __future__ import annotations

import sys
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SITE = Path(__file__).resolve().parent / "site"


class Handler(SimpleHTTPRequestHandler):
    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-cache, max-age=0")
        super().end_headers()


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    port = int(argv[0]) if argv else 8765
    handler = partial(Handler, directory=str(SITE))
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    print(f"Spread on http://127.0.0.1:{port}/index.html  (serving {SITE})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

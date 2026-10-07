#!/usr/bin/env python3
"""Serve the Spread site locally, with caching switched off.

    python serve.py            # http://127.0.0.1:8765/site/
    python serve.py 9000       # a different port

The site reads two trees: `site/` (the pages) and `data/` (everything the pages
fetch). That is exactly what the deploy artifact looks like - the workflow
uploads both, so at the published root `index.html` and `data/` sit side by
side - and it is why the pages resolve their data against the site's own
directory rather than a hard-coded path.

Locally the pages live under `/site/`, one directory below the root that holds
`data/`, which is what `site/app.js` accounts for. So this serves the
repository root and prints the `/site/` URL to open. `python -m http.server`
from the repository root does the same job; this adds the `Cache-Control`
header that a plain `http.server` omits, so a page you just edited cannot render
from the browser's heuristic copy and look like a bug in your code.

Standard library only, read-only, one thread per request (see CLAUDE.md).
"""

from __future__ import annotations

import sys
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent


class Handler(SimpleHTTPRequestHandler):
    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-cache, max-age=0")
        super().end_headers()


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    port = int(argv[0]) if argv else 8765
    handler = partial(Handler, directory=str(ROOT))
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    print(f"Spread on http://127.0.0.1:{port}/site/index.html  (serving {ROOT})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Read a local ``.env`` without adding a dependency (CLAUDE.md: stdlib only).

The file exists for local development and is gitignored. Nothing here ever
prints, logs or returns a value by accident: :func:`load` fills ``os.environ``
only for keys that are not already set, so a real environment always wins, and
:func:`redact` scrubs known secrets out of any string before it is shown.

    from envfile import load, redact
    load()
    key = os.environ.get("ODDS_API_KEY")
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parent / ".env"

# Keys this project knows are secret. Kept explicit rather than guessing, so a
# value can never leak into a log line by way of a clever pattern.
SECRET_KEYS = ("ODDS_API_KEY", "METACULUS_TOKEN")

_ASSIGNMENT = re.compile(r"(?i)\b(apikey|api_key|token|key)=([^&\s]+)")


def parse(text: str) -> dict[str, str]:
    """Parse ``KEY=value`` lines, ignoring comments and quoting."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def load(path: Path | str | None = None) -> dict[str, str]:
    """Load ``.env`` into the environment; returns the keys that were read."""
    path = Path(path) if path is not None else ENV_PATH
    if not path.exists():
        return {}
    values = parse(path.read_text(encoding="utf-8"))
    for key, value in values.items():
        os.environ.setdefault(key, value)
    return values


def get(name: str, path: Path | str | None = None) -> str:
    """One required value, or a clear error that does not name the secret."""
    if not os.environ.get(name):
        load(path)
    value = os.environ.get(name, "").strip()
    if not value:
        raise KeyError(
            f"{name} is not set. Put it in .env (gitignored) or the environment."
        )
    return value


def redact(text: object) -> str:
    """Scrub secrets and ``key=``-style values so a message is safe to show."""
    out = str(text)
    for name in SECRET_KEYS:
        value = os.environ.get(name)
        if value and len(value) >= 4:
            out = out.replace(value, "***")
    return _ASSIGNMENT.sub(lambda m: f"{m.group(1)}=***", out)

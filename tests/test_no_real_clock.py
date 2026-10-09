"""No test may read the wall clock: a suite that depends on "now" expires.

Three gap tests used to judge stored prices against ``datetime.now()``. They
passed the day they were written and failed 24 hours later, when their fixture
timestamps crossed the freshness window - a suite that reports a defect in the
calendar rather than in the code. The fix per test is easy; keeping it fixed is
not, so this module reads every test file and refuses both shapes of the
mistake:

* a direct read of the clock (``datetime.now()``, ``time.time()``, ...), and
* a call to a helper whose ``now=`` defaults to the clock
  (:func:`freshness.assess`, :func:`gaps.event_prices`,
  :func:`store.read_window_changes`, the ``catalog`` steps) without a ``now=``.

Duration timers (``perf_counter``, ``monotonic``, ``process_time``) are
deliberately allowed: they measure an interval and cannot leak a date.
"""

import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parent
SELF = Path(__file__).resolve()

# Calls that answer "what time is it now".
CLOCK_CALLS = (
    "datetime.now(",
    "datetime.utcnow(",
    "datetime.today(",
    "date.today(",
    "time.time(",
    "time.time_ns(",
    "time.localtime(",
    "time.gmtime(",
    "time.ctime(",
    "time.strftime(",
)

# Functions that decide something from "now" unless they are told which now.
# Omitting now= makes the test's verdict depend on the day it runs.
NOW_DEFAULTING_CALLS = (
    "assess(",
    "event_prices(",
    "read_window_changes(",
    "read_market_window_changes(",
    "compute_gaps(",
    "refresh_prices(",
    "run_matching(",
    "sync_catalog(",
)


def _rel(path: Path) -> str:
    """A path as the reader sees it: relative when it is inside the repo."""
    try:
        return str(path.relative_to(ROOT))
    except ValueError:  # the throwaway sample file the detector test writes
        return str(path)


def test_files() -> list[Path]:
    """Every test module: the suite's files, and the root smoke script."""
    found = {path.resolve() for path in TESTS_DIR.rglob("test_*.py")}
    found.update(path.resolve() for path in ROOT.glob("test_*.py"))
    return sorted(found - {SELF})


def _call_arguments(text: str, open_paren: int) -> str:
    """The text of one call, from its ``(`` to the matching ``)``."""
    depth = 0
    for index in range(open_paren, len(text)):
        char = text[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return text[open_paren : index + 1]
    return text[open_paren:]


def offenders(path: Path) -> list[str]:
    """Every clock dependency in one test file, as ``line: what`` strings."""
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    comments = {
        number
        for number, line in enumerate(lines, 1)
        if line.lstrip().startswith("#")
    }
    found = []

    def is_call_line(number: int) -> bool:
        """A definition is not a call, however it is named."""
        if number in comments:
            return False
        stripped = lines[number - 1].lstrip()
        return not stripped.startswith(("def ", "class "))

    for number, line in enumerate(lines, 1):
        if not is_call_line(number):
            continue
        for call in CLOCK_CALLS:
            if call in line:
                found.append(f"{number}: reads the clock with {call}  {line.strip()}")

    for call in NOW_DEFAULTING_CALLS:
        offset = text.find(call)
        while offset != -1:
            number = text.count("\n", 0, offset) + 1
            if is_call_line(number) and "now=" not in _call_arguments(
                text, offset + len(call) - 1
            ):
                found.append(
                    f"{number}: {call.rstrip('(')}() without now=, so it uses the real clock"
                )
            offset = text.find(call, offset + 1)

    return [f"{_rel(path)}:{item}" for item in found]


class NoRealClockTest(unittest.TestCase):
    def test_no_test_file_depends_on_the_wall_clock(self):
        offenders_by_file = {
            _rel(path): found for path in test_files() if (found := offenders(path))
        }
        self.assertEqual(
            offenders_by_file,
            {},
            "tests must be given a fixed instant instead of reading the clock:\n"
            + "\n".join(
                f"  {path}\n" + "\n".join(f"    {item}" for item in items)
                for path, items in offenders_by_file.items()
            ),
        )

    def test_the_scan_finds_the_test_files(self):
        """A mis-globbed scan would report success by looking at nothing."""
        names = {path.name for path in test_files()}
        self.assertGreaterEqual(len(names), 10, names)
        self.assertIn("test_matching.py", names)
        self.assertIn("test_smoke.py", names)

    def test_the_check_itself_catches_a_clock_dependency(self):
        """Prove the detector bites, on a temporary file that must not pass."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            sample = Path(tmp) / "test_sample.py"
            sample.write_text(
                "import datetime\n"
                "import gaps\n"
                "NOW = datetime.datetime(2026, 10, 7)\n"
                "def wobbly():\n"
                "    return datetime.datetime.now()\n"
                "def also_wobbly():\n"
                "    return gaps.event_prices([], {}, )\n"
                "def fine():\n"
                "    return gaps.event_prices([], {}, now=NOW)\n",
                encoding="utf-8",
            )
            found = offenders(sample)
        self.assertEqual(len(found), 2, found)
        self.assertTrue(any("datetime.datetime.now(" in item for item in found), found)
        self.assertTrue(any("event_prices()" in item for item in found), found)


if __name__ == "__main__":
    unittest.main()

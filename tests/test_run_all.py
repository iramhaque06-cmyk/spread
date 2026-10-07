"""Offline tests for the pipeline runner: which step runs, and in what order.

Nothing here executes a pipeline script. `subprocess.Popen` is replaced with a
recorder, so the test asserts the *commands* run_all.py would issue - which is
the whole content of the cadence decision - without spending a live request.
"""

import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import run_all


class Recorder:
    """Stands in for subprocess.Popen and remembers what was asked for."""

    issued: list[list[str]] = []

    def __init__(self, command, **kwargs):
        self.command = list(command)
        self.returncode = 0
        Recorder.issued.append(self.command)
        self.stdout = iter([f"{self.command[1]}: output\n"])

    def wait(self):
        return self.returncode


class RunnerTest(unittest.TestCase):
    def setUp(self):
        Recorder.issued = []
        self.tmp = tempfile.TemporaryDirectory()
        self.log = Path(self.tmp.name) / "run_all.log"
        self.patchers = [
            mock.patch.object(run_all.subprocess, "Popen", Recorder),
            mock.patch.object(run_all, "LOG_PATH", self.log),
        ]
        for patcher in self.patchers:
            patcher.start()
        self.addCleanup(lambda: [p.stop() for p in self.patchers])
        self.addCleanup(self.tmp.cleanup)

    def scripts(self):
        return [Path(command[1]).name for command in Recorder.issued]

    def run_main(self, argv) -> int:
        """Run the CLI with its echoed output swallowed, as a suite should."""
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return run_all.main(argv)

    def test_full_pass_runs_the_whole_chain_in_order(self):
        self.assertEqual(self.run_main([]), 0)
        self.assertEqual(
            self.scripts(),
            [
                "fetch_venue_a.py",
                "fetch_venue_b.py",
                "spread.py",
                "catalog.py",
                "export.py",
            ],
        )

    def test_prices_only_skips_the_scans_and_narrows_the_export(self):
        self.assertEqual(self.run_main(["--prices-only"]), 0)
        self.assertEqual(self.scripts(), ["spread.py", "catalog.py", "export.py"])
        catalog, export = Recorder.issued[1], Recorder.issued[2]
        self.assertIn("--prices-only", catalog)
        self.assertIn("--prices-only", export)
        # The wide scans are the 6 hourly job's work, not the 30 minute one's.
        self.assertNotIn("fetch_venue_a.py", self.scripts())

    def test_refresh_is_forwarded_to_every_step(self):
        self.run_main(["--refresh"])
        for command in Recorder.issued:
            self.assertIn("--refresh", command)

    def test_skip_catalog_leaves_the_multi_venue_layer_alone(self):
        self.run_main(["--skip-catalog"])
        self.assertEqual(
            self.scripts(), ["fetch_venue_a.py", "fetch_venue_b.py", "spread.py"]
        )

    def test_skip_scans_still_runs_the_catalog(self):
        self.run_main(["--skip-scans"])
        self.assertEqual(self.scripts(), ["spread.py", "catalog.py", "export.py"])

    def test_output_is_echoed_and_appended_to_the_log(self):
        self.run_main([])
        text = self.log.read_text(encoding="utf-8")
        self.assertIn("run_all.py", text)
        for script in self.scripts():
            self.assertIn(script, text)

    def test_no_log_leaves_no_log_file(self):
        self.run_main(["--no-log"])
        self.assertFalse(self.log.exists())

    def test_a_failing_required_step_fails_the_run(self):
        class Failing(Recorder):
            def __init__(self, command, **kwargs):
                super().__init__(command, **kwargs)
                if Path(command[1]).name == "spread.py":
                    self.returncode = 1

        with mock.patch.object(run_all.subprocess, "Popen", Failing):
            self.assertEqual(self.run_main([]), 1)

    def test_a_failing_optional_scan_does_not_fail_the_run(self):
        class FailingScan(Recorder):
            def __init__(self, command, **kwargs):
                super().__init__(command, **kwargs)
                if Path(command[1]).name == "fetch_venue_a.py":
                    self.returncode = 3

        with mock.patch.object(run_all.subprocess, "Popen", FailingScan):
            self.assertEqual(self.run_main([]), 0)


if __name__ == "__main__":
    unittest.main()

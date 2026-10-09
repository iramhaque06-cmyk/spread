"""Offline tests for the match-only runner: what it refuses, and what it reuses.

Nothing here reaches a venue or opens a database. The guard is asserted where
it runs (before any connection), so a mistake in it fails a test rather than a
live catalog.
"""

import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from unittest import mock

import run_match_only
import store

NOW = datetime(2026, 10, 7, 12, 0, 0, tzinfo=timezone.utc)


class LiveDatabaseGuardTest(unittest.TestCase):
    """``spread.db`` is the published site's source; it needs asking for."""

    def test_the_live_database_is_refused_without_the_flag(self):
        message = run_match_only._refuse_live_db(store.DB_PATH, allowed=False)
        self.assertIn("refusing", message)
        self.assertIn("--allow-real-db", message)
        # The default --db is the live file, so a bare run is a refusal too.
        self.assertIn("refusing", run_match_only._refuse_live_db("spread.db", allowed=False))

    def test_the_live_database_is_allowed_when_asked_for(self):
        self.assertEqual(run_match_only._refuse_live_db(store.DB_PATH, allowed=True), "")

    def test_any_other_database_is_worked_on_quietly(self):
        for path in ("test_run.db", "run.db", "data/scratch.db"):
            self.assertEqual(run_match_only._refuse_live_db(path, allowed=False), "", path)

    def test_main_refuses_before_it_touches_the_database(self):
        with mock.patch.object(run_match_only.store, "connect") as connect:
            with redirect_stderr(io.StringIO()) as errors:
                code = run_match_only.main(["--db", str(store.DB_PATH)])
        self.assertEqual(code, 2)
        connect.assert_not_called()
        self.assertIn("--allow-real-db", errors.getvalue())


class CatalogDelegationTest(unittest.TestCase):
    """The catalog step must be the shared one, not a second copy of it."""

    def test_the_catalog_step_reuses_the_shared_listing_pass(self):
        calls = {}

        def fake(conn, **kwargs):
            calls["conn"] = conn
            calls.update(kwargs)
            return {
                "kalshi": {
                    "listed": 1, "open": 1, "closed": 0, "refused": "", "errors": "",
                }
            }

        sentinel = object()
        with mock.patch.object(run_match_only.catalog, "sync_catalog", fake):
            with redirect_stdout(io.StringIO()):
                report = run_match_only._catalog_now(
                    sentinel, max_age=0, limit=25, now=NOW
                )

        self.assertIs(calls.pop("conn"), sentinel)
        self.assertEqual(calls, {"max_age": 0, "limit": 25, "now": NOW})
        self.assertEqual(report["kalshi"]["listed"], 1)


if __name__ == "__main__":
    unittest.main()

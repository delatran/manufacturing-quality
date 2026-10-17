"""Historical accounting and uncapped admission stay distinct and durable."""

import json
import sqlite3

import pytest
from analyst.store import SessionStore


def test_legacy_budget_migration_preserves_all_existing_values(tmp_path):
    database = sqlite3.connect(tmp_path / "workspace.sqlite3")
    database.executescript("""
        CREATE TABLE budget_reservations (
            run_id TEXT PRIMARY KEY, amount REAL NOT NULL,
            settled INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE costs (id TEXT PRIMARY KEY, run_id TEXT NOT NULL, amount REAL NOT NULL);
        INSERT INTO budget_reservations VALUES ('legacy-unknown', 7.25, 0);
        INSERT INTO budget_reservations VALUES ('legacy-settled', 4.5, 1);
        INSERT INTO costs VALUES ('resp-old-1', 'legacy-unknown', 1.125);
        INSERT INTO costs VALUES ('resp-old-2', 'legacy-settled', 2.75);
    """)
    database.close()
    store = SessionStore(tmp_path)
    try:
        assert [
            tuple(row)
            for row in store.db.execute(
                "SELECT run_id,amount,settled,unbounded FROM budget_reservations ORDER BY run_id"
            )
        ] == [("legacy-settled", 4.5, 1, 0), ("legacy-unknown", 7.25, 0, 0)]
        assert [tuple(row) for row in store.db.execute("SELECT * FROM costs ORDER BY id")] == [
            ("resp-old-1", "legacy-unknown", 1.125),
            ("resp-old-2", "legacy-settled", 2.75),
        ]
        assert store.remaining_budget(12) == 2
        assert store.reserve_budget("new-unbounded", None, None) is None
        assert store.remaining_budget(12) == 2
    finally:
        store.close()
    reopened = SessionStore(tmp_path)
    try:
        assert reopened.remaining_budget(12) == 2
        assert reopened.db.execute(
            "SELECT amount,settled,unbounded FROM budget_reservations WHERE run_id='legacy-unknown'"
        ).fetchone()[:] == (7.25, 0, 0)
    finally:
        reopened.close()


def test_unbounded_admission_uses_zero_marker_without_inventing_financial_liability(tmp_path):
    store = SessionStore(tmp_path)
    try:
        assert store.reserve_budget("unbounded-run", None, None) is None
        row = dict(
            store.db.execute(
                "SELECT * FROM budget_reservations WHERE run_id='unbounded-run'"
            ).fetchone()
        )
        assert row == {"run_id": "unbounded-run", "amount": 0, "settled": 0, "unbounded": 1}
        assert store.remaining_budget(None) is None
        encoded = json.dumps(
            {"admission": None, "remaining": store.remaining_budget(None), "reservation": row},
            allow_nan=False,
        )
        assert "Infinity" not in encoded and "NaN" not in encoded
        store.record_cost("unbounded-run", "observed-token-estimate", 0.125)
        assert store.remaining_budget(10) == 9.875
        store.settle_budget("unbounded-run")
        assert store.remaining_budget(10) == 9.875
    finally:
        store.close()


@pytest.mark.parametrize("run_limit,dev_limit,expected", [(None, 10, 7), (5, None, 5), (5, 10, 5)])
def test_optional_bounded_limits_and_nonfinite_rejection(tmp_path, run_limit, dev_limit, expected):
    store = SessionStore(tmp_path)
    try:
        store.reserve_budget("previous", 3, 10)
        assert store.reserve_budget("bounded-run", run_limit, dev_limit) == expected
        row = store.db.execute(
            "SELECT amount,unbounded FROM budget_reservations WHERE run_id='bounded-run'"
        ).fetchone()
        assert tuple(row) == (expected, 0)
        for invalid in (float("inf"), float("-inf"), float("nan"), -1):
            for limits in ((invalid, None), (None, invalid)):
                with pytest.raises(ValueError, match="finite"):
                    store.reserve_budget("must-not-be-created", *limits)
        assert (
            store.db.execute(
                "SELECT count(*) FROM budget_reservations WHERE run_id='must-not-be-created'"
            ).fetchone()[0]
            == 0
        )
    finally:
        store.close()

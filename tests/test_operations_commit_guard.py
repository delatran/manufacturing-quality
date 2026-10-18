"""Runtime permission checks run at the final transactional write boundary."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager

import pytest
from analyst.operations import OperationForbidden
from test_operations import create, operation_counts, service


@pytest.mark.asyncio
async def test_create_rechecks_revoked_authority_after_context_preparation(tmp_path, monkeypatch):
    subject = service(tmp_path)
    payload = await subject.analysis.get_context()
    entered, proceed = asyncio.Event(), asyncio.Event()
    original = subject.analysis.context_by_id
    revoked = False
    calls = []

    async def delayed_context(context_id):
        result = await original(context_id)
        entered.set()
        await proceed.wait()
        return result

    def guard():
        calls.append(revoked)
        if revoked:
            raise OperationForbidden("Owner revoked draft authority during preparation")

    monkeypatch.setattr(subject.analysis, "context_by_id", delayed_context)
    pending = asyncio.create_task(
        subject.create_case(
            {
                "context_id": payload["context"]["context_id"],
                "title": "Must not be saved",
                "idempotency_key": "revocation-during-preparation",
            },
            before_commit=guard,
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), 10)
        revoked = True
        proceed.set()
        with pytest.raises(OperationForbidden, match="revoked"):
            await asyncio.wait_for(pending, 10)
        assert calls == [True]
        assert operation_counts(subject) == (0, 0, 0)
    finally:
        proceed.set()
        await asyncio.gather(pending, return_exceptions=True)
        subject.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["create", "replay", "update", "unchanged"])
async def test_commit_guard_runs_inside_transaction_and_blocks_every_result(
    tmp_path, monkeypatch, operation
):
    subject = service(tmp_path)
    record = await create(subject)
    original_connect = subject._connect
    transaction = []
    guard_calls = []

    @contextmanager
    def connected(*, write=False):
        with original_connect(write=write) as db:
            if write:
                transaction.append(db)
            try:
                yield db
            finally:
                if write:
                    transaction.pop()

    def guard():
        guard_calls.append(True)
        assert len(transaction) == 1 and transaction[0].in_transaction
        raise OperationForbidden("Owner instruction superseded this draft")

    monkeypatch.setattr(subject, "_connect", connected)
    try:
        with pytest.raises(OperationForbidden, match="superseded"):
            if operation in {"create", "replay"}:
                await subject.create_case(
                    {
                        "context_id": record["context_id"],
                        "title": record["title"],
                        "idempotency_key": "test-request-1"
                        if operation == "replay"
                        else "another-request",
                    },
                    before_commit=guard,
                )
            else:
                await subject.update_case(
                    record["id"],
                    record["version"],
                    {"title": record["title"] if operation == "unchanged" else "Revised title"},
                    before_commit=guard,
                )
        assert guard_calls == [True]
        assert operation_counts(subject) == (1, 1, 1)
        assert subject.get_case(record["id"]) == record
        assert not transaction
    finally:
        subject.close()

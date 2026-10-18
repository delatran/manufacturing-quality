"""Dashboard responsiveness and cache isolation without timing-based speed gates."""

from __future__ import annotations

import asyncio
import copy
import io
import json
import threading
from zipfile import ZipFile

import pytest
from analyst import dashboard
from analyst.case_study.ingestion import SourceError
from analyst.dashboard import EvidenceIntegrityError
from analyst.evidence import EvidenceLedger, canonical, sha256
from test_dashboard import Loader, event, make_snapshot, service


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["get_context", "context_by_id", "records", "export_csv"])
async def test_warm_integrity_reads_leave_the_event_loop_responsive(
    tmp_path, monkeypatch, operation
):
    subject = service(tmp_path)
    payload = await subject.get_context()
    context_id = payload["context"]["context_id"]
    entered = threading.Event()
    release = threading.Event()
    verify = subject._verify

    def held_verification(saved):
        entered.set()
        if not release.wait(timeout=5):
            raise AssertionError("The event loop could not release the integrity read")
        return verify(saved)

    monkeypatch.setattr(subject, "_verify", held_verification)
    arguments = () if operation == "get_context" else (context_id,)
    request = asyncio.create_task(getattr(subject, operation)(*arguments))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        # This coroutine must run while file verification remains in progress.
        assert not request.done()
        await asyncio.sleep(0)
    finally:
        release.set()
        await request


@pytest.mark.asyncio
async def test_record_page_mutation_cannot_change_cached_values_or_order(tmp_path):
    subject = service(tmp_path)
    payload = await subject.get_context()
    context_id = payload["context"]["context_id"]
    first = await subject.records(context_id, sort_by="defect_id", sort_order="asc")
    expected = copy.deepcopy(first)
    first["rows"][0].update(defect_id=999, repair_cost=-999, severity="Unknown")
    first["rows"].clear()

    assert await subject.records(context_id, sort_by="defect_id", sort_order="asc") == expected
    by_cost = await subject.records(context_id, sort_by="repair_cost", sort_order="asc")
    assert {row["defect_id"] for row in by_cost["rows"]} == {
        row["defect_id"] for row in expected["rows"]
    }
    assert all(row["repair_cost"] != -999 for row in by_cost["rows"])


@pytest.mark.asyncio
async def test_cached_records_still_reject_changed_export_artifacts(tmp_path):
    subject = service(tmp_path)
    payload = await subject.get_context()
    context_id = payload["context"]["context_id"]
    await subject.records(context_id)
    target = subject.contexts_dir / context_id / "artifacts" / "evidence.vi.zip"
    target.write_bytes(target.read_bytes() + b"changed")

    with pytest.raises(EvidenceIntegrityError):
        await subject.records(context_id)


@pytest.mark.asyncio
async def test_english_bundle_reuses_default_content_with_complete_localized_manifests(tmp_path):
    subject = service(tmp_path)
    payload = await subject.get_context()
    context_id = payload["context"]["context_id"]
    default = await subject.export_zip(context_id)
    assert await subject.export_zip(context_id, "en") == default

    for locale in ("en", "vi"):
        with ZipFile(io.BytesIO(await subject.export_zip(context_id, locale))) as archive:
            assert archive.testzip() is None
            metadata = json.loads(archive.read("export_metadata.json"))
            assert metadata["export_locale"] == locale
            assert metadata["context_id"] == context_id
            manifest = json.loads(archive.read("bundle_manifest.json"))
            assert set(manifest["files"]) == set(archive.namelist()) - {"bundle_manifest.json"}
            assert all(
                sha256(archive.read(name)) == expected
                for name, expected in manifest["files"].items()
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["get_context", "context_by_id", "records", "export_csv"])
async def test_existing_context_remains_available_while_another_context_is_prepared(
    tmp_path, monkeypatch, operation
):
    subject = service(tmp_path)
    original = await subject.get_context()
    context_id = original["context"]["context_id"]
    entered = threading.Event()
    release = threading.Event()
    create = subject._create

    def held_creation(*args):
        entered.set()
        assert release.wait(timeout=15), "Independent reads were blocked by context preparation"
        return create(*args)

    monkeypatch.setattr(subject, "_create", held_creation)
    cold = asyncio.create_task(subject.get_context({"severity": "Critical"}))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        arguments = () if operation == "get_context" else (context_id,)
        value = await asyncio.wait_for(getattr(subject, operation)(*arguments), timeout=5)
        assert not cold.done()
        if operation != "export_csv":
            assert value["context"] == original["context"]
    finally:
        release.set()
        await cold


@pytest.mark.asyncio
async def test_concurrent_reads_each_verify_current_artifact_bytes(tmp_path, monkeypatch):
    subject = service(tmp_path)
    original = await subject.get_context()
    context_id = original["context"]["context_id"]
    entered = threading.Event()
    release = threading.Event()
    counter_lock = threading.Lock()
    calls = 0
    verify = subject._verify

    def held_first_verification(saved):
        nonlocal calls
        with counter_lock:
            calls += 1
            first = calls == 1
        if first:
            entered.set()
            assert release.wait(timeout=15)
        return verify(saved)

    monkeypatch.setattr(subject, "_verify", held_first_verification)
    first = asyncio.create_task(subject.context_by_id(context_id))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        second = await asyncio.wait_for(subject.context_by_id(context_id), timeout=5)
        assert second["analysis"] == original["analysis"]
        assert calls == 2
        target = subject.contexts_dir / context_id / "artifacts" / "evidence.vi.zip"
        target.write_bytes(target.read_bytes() + b"changed after the second read")
    finally:
        release.set()
    with pytest.raises(EvidenceIntegrityError):
        await first


@pytest.mark.asyncio
async def test_cancelled_creation_waiter_does_not_cancel_shared_preparation(tmp_path, monkeypatch):
    subject = service(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    create = subject._create
    calls = 0

    def held_creation(*args):
        nonlocal calls
        calls += 1
        entered.set()
        assert release.wait(timeout=15)
        return create(*args)

    monkeypatch.setattr(subject, "_create", held_creation)
    cancelled = asyncio.create_task(subject.get_context())
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        survivor = asyncio.create_task(subject.get_context())
        await asyncio.sleep(0)
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        assert not survivor.done()
    finally:
        release.set()
    payload = await survivor
    assert calls == 1
    assert payload["analysis"]["kpis"]["event_count"] == 6
    assert len(list(subject.contexts_dir.iterdir())) == 1
    assert subject._context_tasks == {}
    assert subject._snapshot_task is None


@pytest.mark.asyncio
async def test_failed_shared_preparation_is_evicted_and_can_be_retried(tmp_path, monkeypatch):
    subject = service(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    create = subject._create
    calls = 0

    def failed_creation(*args):
        nonlocal calls
        calls += 1
        entered.set()
        assert release.wait(timeout=15)
        raise OSError("synthetic staging failure")

    monkeypatch.setattr(subject, "_create", failed_creation)
    first = asyncio.create_task(subject.get_context())
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        second = asyncio.create_task(subject.get_context())
        await asyncio.sleep(0)
    finally:
        release.set()
    outcomes = await asyncio.gather(first, second, return_exceptions=True)
    assert all(isinstance(result, OSError) for result in outcomes)
    assert calls == 1
    assert subject._context_tasks == {}
    assert subject._contexts == {}
    monkeypatch.setattr(subject, "_create", create)
    assert (await subject.get_context())["analysis"]["kpis"]["event_count"] == 6


@pytest.mark.asyncio
async def test_cancelled_fetch_waiter_does_not_cancel_or_repeat_the_fetch(tmp_path):
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def loader():
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return make_snapshot()

    subject = service(tmp_path, loader)
    first = asyncio.create_task(subject.get_context())
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        second = asyncio.create_task(subject.get_context())
        await asyncio.sleep(0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
    finally:
        release.set()
    assert (await second)["analysis"]["kpis"]["event_count"] == 6
    assert calls == 1
    assert subject._snapshot_task is None


@pytest.mark.asyncio
@pytest.mark.parametrize("loader_kind", ["sync", "async", "returned_awaitable"])
async def test_snapshot_freezing_is_responsive_atomic_and_survives_waiter_cancellation(
    tmp_path, monkeypatch, loader_kind
):
    snapshots = Loader(make_snapshot([event(1)]), make_snapshot([event(1), event(2)]))

    async def async_loader():
        return snapshots()

    def awaitable_loader():
        return async_loader()

    subject = service(
        tmp_path,
        {"sync": snapshots, "async": async_loader, "returned_awaitable": awaitable_loader}[
            loader_kind
        ],
    )
    original = await subject.get_context()
    original_id = original["context"]["snapshot_id"]
    entered = threading.Event()
    release = threading.Event()
    freeze = subject._freeze_snapshot
    calls = 0

    def held_freeze(snapshot):
        nonlocal calls
        calls += 1
        entered.set()
        assert release.wait(timeout=15), "Snapshot freezing blocked the event loop"
        return freeze(snapshot)

    monkeypatch.setattr(subject, "_freeze_snapshot", held_freeze)
    first = asyncio.create_task(subject.refresh())
    survivor = None
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        # An incomplete freeze never becomes the selected source generation.
        assert subject._latest == original_id
        assert set(subject._snapshots) == {original_id}
        existing = await asyncio.wait_for(
            subject.context_by_id(original["context"]["context_id"]), timeout=5
        )
        assert existing["analysis"] == original["analysis"]
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert subject._snapshot_task is not None and not subject._snapshot_task.done()
        survivor = asyncio.create_task(subject.refresh())
        await asyncio.sleep(0)
        assert not survivor.done()
    finally:
        release.set()
    assert survivor is not None
    refreshed = await survivor
    assert snapshots.calls == 2
    assert calls == 1
    assert refreshed["analysis"]["kpis"]["event_count"] == 2
    assert refreshed["context"]["snapshot_id"] != original_id
    assert subject._latest == refreshed["context"]["snapshot_id"]
    assert subject._snapshot_task is None


@pytest.mark.asyncio
async def test_snapshot_exposure_copy_is_responsive_and_detached(tmp_path, monkeypatch):
    subject = service(tmp_path)
    payload = await subject.get_context()
    context_id = payload["context"]["context_id"]
    frozen = subject._snapshots[payload["context"]["snapshot_id"]].snapshot
    entered = threading.Event()
    release = threading.Event()
    deepcopy = dashboard.copy.deepcopy

    def held_copy(value, *args, **kwargs):
        if value is frozen:
            entered.set()
            assert release.wait(timeout=15), "Defensive snapshot copying blocked the event loop"
        return deepcopy(value, *args, **kwargs)

    monkeypatch.setattr(dashboard.copy, "deepcopy", held_copy)
    request = asyncio.create_task(subject.snapshot_for_context(context_id))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        existing = await asyncio.wait_for(subject.context_by_id(context_id), timeout=5)
        assert existing["analysis"] == payload["analysis"]
        assert not request.done()
    finally:
        release.set()
    exposed = await request
    assert exposed.events == frozen.events
    assert exposed.events[0] is not frozen.events[0]
    assert exposed.raw_records == frozen.raw_records
    assert exposed.master_bytes == frozen.master_bytes
    exposed.quality["raw_event_count"] = -1
    exposed.raw_summary["changed"] = True
    exposed.provenance["source_sha256"] = "changed"
    assert frozen.quality["raw_event_count"] >= 0
    assert "changed" not in frozen.raw_summary
    assert frozen.provenance["source_sha256"] == payload["context"]["source_sha256"]


@pytest.mark.asyncio
@pytest.mark.parametrize("has_existing_snapshot", [False, True])
async def test_failed_snapshot_freeze_publishes_nothing_and_can_retry(
    tmp_path, has_existing_snapshot
):
    valid = make_snapshot([event(1)])
    invalid = make_snapshot([event(1), event(2)])
    invalid.provenance["source_sha256"] = "invalid digest"
    loader = Loader(*([valid] if has_existing_snapshot else []), invalid, valid)
    subject = service(tmp_path, loader)
    if has_existing_snapshot:
        original = await subject.get_context()
        stale = await subject.refresh()
        assert stale["context"] == original["context"]
        assert stale["analysis"] == original["analysis"]
        assert len(subject._snapshots) == len(subject._contexts) == 1
    else:
        with pytest.raises(SourceError, match="invalid source_sha256"):
            await subject.get_context()
        assert subject._snapshots == subject._contexts == {}
    assert subject._snapshot_task is None
    assert subject._status["stale"] is True
    assert "invalid source_sha256" in subject._status["last_error"]
    recovered = await subject.refresh()
    assert recovered["analysis"]["kpis"]["event_count"] == 1
    assert recovered["status"]["stale"] is False
    assert recovered["status"]["last_error"] is None
    assert len(subject._snapshots) == len(subject._contexts) == 1


@pytest.mark.asyncio
async def test_failed_initial_fetch_releases_all_waiters_and_allows_retry(tmp_path):
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def loader():
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
            raise SourceError("synthetic unavailable source")
        return make_snapshot()

    subject = service(tmp_path, loader)
    first = asyncio.create_task(subject.get_context())
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        second = asyncio.create_task(subject.get_context())
        await asyncio.sleep(0)
    finally:
        release.set()
    outcomes = await asyncio.gather(first, second, return_exceptions=True)
    assert all(isinstance(result, SourceError) for result in outcomes)
    assert calls == 1
    assert subject._snapshot_task is None
    assert (await subject.get_context())["analysis"]["kpis"]["event_count"] == 6
    assert calls == 2


@pytest.mark.asyncio
async def test_failed_staged_write_cleans_its_directory_and_can_retry(tmp_path, monkeypatch):
    subject = service(tmp_path)
    zip_bytes = dashboard._zip_bytes

    def failed_bundle(_files):
        raise OSError("synthetic disk write failure")

    monkeypatch.setattr(dashboard, "_zip_bytes", failed_bundle)
    with pytest.raises(OSError, match="synthetic disk write failure"):
        await subject.get_context()
    assert list(subject.contexts_dir.iterdir()) == []
    assert subject._context_tasks == {}
    assert subject._contexts == {}
    monkeypatch.setattr(dashboard, "_zip_bytes", zip_bytes)
    assert (await subject.get_context())["analysis"]["kpis"]["event_count"] == 6


@pytest.mark.asyncio
async def test_refresh_keeps_its_fetched_generation_while_a_newer_refresh_finishes(
    tmp_path, monkeypatch
):
    snapshots = [make_snapshot([event(i) for i in range(1, size + 1)]) for size in (1, 2, 3)]
    subject = service(tmp_path, Loader(*snapshots))
    original = await subject.get_context()
    entered = threading.Event()
    release = threading.Event()
    create = subject._create

    def hold_second_generation(frozen, *args):
        if len(frozen.snapshot.events) == 2:
            entered.set()
            assert release.wait(timeout=15)
        return create(frozen, *args)

    monkeypatch.setattr(subject, "_create", hold_second_generation)
    second_request = asyncio.create_task(subject.refresh())
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        third = await asyncio.wait_for(subject.refresh(), timeout=5)
        assert third["analysis"]["kpis"]["event_count"] == 3
        old = await subject.context_by_id(original["context"]["context_id"])
        assert old["analysis"] == original["analysis"]
    finally:
        release.set()
    second = await second_request
    assert second["analysis"]["kpis"]["event_count"] == 2
    assert len({value["context"]["snapshot_id"] for value in (original, second, third)}) == 3
    assert (await subject.get_context())["context"] == third["context"]


@pytest.mark.asyncio
async def test_concurrent_restore_uses_one_receipt_and_survives_a_cancelled_waiter(
    tmp_path, monkeypatch
):
    original = await service(tmp_path).get_context()
    context_id = original["context"]["context_id"]
    subject = service(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    restore = subject._restore
    calls = 0

    def held_restore(*args):
        nonlocal calls
        calls += 1
        entered.set()
        assert release.wait(timeout=15)
        return restore(*args)

    monkeypatch.setattr(subject, "_restore", held_restore)
    first = asyncio.create_task(subject.context_by_id(context_id))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        second = asyncio.create_task(subject.get_context())
        third = asyncio.create_task(subject.records(context_id))
        await asyncio.sleep(0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
    finally:
        release.set()
    restored, records = await asyncio.gather(second, third)
    assert calls == 1
    assert restored["evidence"] == original["evidence"]
    assert records["context"] == original["context"]
    assert subject._context_tasks == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["artifact", "rehashed_manifest"])
async def test_export_checks_original_seal_after_full_verification(tmp_path, monkeypatch, changed):
    subject = service(tmp_path)
    original = await subject.get_context()
    export_bytes = subject._export_bytes

    def changed_export(saved, name):
        ledger = EvidenceLedger(saved.directory)
        target = saved.directory / "artifacts" / name
        target.write_bytes(b"changed export")
        if changed == "rehashed_manifest":
            manifest = ledger.artifacts()
            for entry in manifest:
                if entry["path"] == "artifacts/" + name:
                    entry["sha256"] = sha256(target.read_bytes())
            ledger.manifest_path.write_text(canonical(manifest), encoding="utf-8")
        return export_bytes(saved, name)

    monkeypatch.setattr(subject, "_export_bytes", changed_export)
    with pytest.raises(EvidenceIntegrityError):
        await subject.export_csv(original["context"]["context_id"])

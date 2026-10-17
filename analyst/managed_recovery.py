"""Stop orphaned managed work after a process restart without replaying input."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx2
from openai import AsyncOpenAI

from .config import Settings
from .store import SessionStore

RECOVERY_STOP = "Managed provider recovery is unresolved; saved input must not be replayed."


def _clear_recovery_stop(store: SessionStore, sid: str) -> None:
    with store._lock, store.db:
        cleared = store.db.execute(
            "DELETE FROM session_stops WHERE session_id=? AND reason=?", (sid, RECOVERY_STOP)
        ).rowcount
    if cleared:
        store.emit(
            sid,
            None,
            "managed_recovery",
            {
                "provider_api": "agents",
                "phase": "conversation_reconciled",
                "terminal_confirmed": True,
                "input_replayed": False,
            },
        )


def _pending_sessions(events: list[dict]) -> tuple[set[str], bool]:
    pending: set[str] = set()
    unknown_create = False
    for event in events:
        data = event["data"]
        kind = event["type"]
        if data.get("provider_api") != "agents":
            continue
        identity = data.get("session_id") or data.get("provider_session_id")
        if kind == "model_started":
            if identity:
                pending.add(identity)
            else:
                unknown_create = True
        elif kind == "managed_session_created" and identity:
            pending.add(identity)
            unknown_create = False
        elif kind == "managed_session_rejected" and data.get("generation_dispatched") is False:
            unknown_create = False
        elif kind == "usage" and data.get("provider_terminal") is True:
            pending.discard(identity)
        elif kind in {"provider_cancel", "managed_recovery"}:
            if data.get("terminal_confirmed") is True:
                pending.discard(identity)
    return pending, unknown_create


async def recover_managed_sessions(
    store: SessionStore, settings: Settings, *, client: Any = None
) -> None:
    """Cancel known orphan sessions and retain a durable block for uncertainty.

    This lifecycle operation sends only cancel events and read requests. It never
    reconstructs local tools, resubmits input, or starts a replacement session.
    Historical Responses runs remain interrupted without changing transports.
    """
    with store._lock:
        rows = store.db.execute(
            "SELECT id,session_id FROM runs "
            "WHERE state IN ('interrupted','failed','cancelled') ORDER BY rowid"
        ).fetchall()
    work = []
    for row in rows:
        events = [
            event for event in store.events(row["session_id"]) if event["run_id"] == row["id"]
        ]
        pending, unknown = _pending_sessions(events)
        if pending or unknown:
            work.append((row["session_id"], row["id"], pending, unknown))
    if not work:
        for sid in {row["session_id"] for row in rows}:
            _clear_recovery_stop(store, sid)
        return
    owned_client = client is None
    if client is None and settings.live:
        client = AsyncOpenAI(
            api_key=settings.api_key,
            base_url=settings.openai_base_url or "https://api.openai.com/v1",
            max_retries=0,
            timeout=httpx2.Timeout(connect=10, read=10, write=10, pool=10),
        )
    unresolved_conversations: set[str] = set()
    recovered_conversations = {row["session_id"] for row in rows}
    try:
        for sid, rid, pending, unknown in work:
            unresolved = unknown
            if unknown:
                if not store.stopped(sid):
                    store.stop_session(sid, RECOVERY_STOP)
                store.emit(
                    sid,
                    rid,
                    "managed_recovery",
                    {
                        "provider_api": "agents",
                        "terminal_confirmed": False,
                        "reason": "creation_admission_unknown",
                        "input_replayed": False,
                    },
                )
            for identity in sorted(pending):
                payload: dict[str, Any] = {
                    "provider_api": "agents",
                    "session_id": identity,
                    "terminal_confirmed": False,
                    "input_replayed": False,
                }
                try:
                    if client is None:
                        raise RuntimeError(
                            "Managed recovery requires the configured provider connection"
                        )
                    sessions = client.beta.agents.sessions
                    async with asyncio.timeout(15):
                        await sessions.events.create(
                            identity,
                            events=[{"type": "agent.session.input.cancel"}],
                            idempotency_key=f"restart-cancel-{rid}-{identity}",
                        )
                        while True:
                            session = await sessions.retrieve(identity)
                            if session.id != identity:
                                raise ValueError("Recovery returned a different session identity")
                            if session.status in {"idle", "failed"}:
                                break
                            await asyncio.sleep(0.25)
                        page = await sessions.turns.list(identity, order="desc", limit=1)
                        turns = [turn async for turn in page]
                        if not turns or turns[0].session_id != identity:
                            raise ValueError("Recovery omitted the session's durable turn")
                        turn = turns[0]
                        payload.update(
                            status=session.status, turn_id=turn.id, turn_status=turn.status
                        )
                        if turn.status not in {"completed", "failed", "cancelled"}:
                            raise ValueError("Recovery did not confirm a terminal turn")
                        if "misalignment_policy_violation" in str(
                            (getattr(session, "error", None), getattr(turn, "error", None))
                        ):
                            if not store.stopped(sid) or store.stopped(sid) == RECOVERY_STOP:
                                store.stop_session(sid, "Provider policy stop")
                            payload["provider_stopped"] = True
                        payload["terminal_confirmed"] = True
                except Exception as error:
                    payload["error_type"] = type(error).__name__
                    unresolved = True
                # Persist the conversation block before acknowledging unresolved
                # recovery, including when a durable checkpoint itself fails.
                if unresolved:
                    if not store.stopped(sid):
                        store.stop_session(sid, RECOVERY_STOP)
                store.emit(sid, rid, "managed_recovery", payload)
            if unresolved:
                if not store.stopped(sid):
                    store.stop_session(sid, RECOVERY_STOP)
                unresolved_conversations.add(sid)
            else:
                recovered_conversations.add(sid)
        # Clear only this recovery guard after all affected runs reconcile. Owner
        # and provider policy stops remain authoritative.
        for sid in recovered_conversations - unresolved_conversations:
            _clear_recovery_stop(store, sid)
    finally:
        if owned_client and client is not None:
            await client.close()

"""Verify observer bootstrap against the actual Agent session selector without a provider."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from scripts.eval_browser_observer import EvalBrowserObserver

ROOT = Path(__file__).resolve().parents[1]
DRIVER = r"""
const fs = require("node:fs"), vm = require("node:vm");
const input = JSON.parse(fs.readFileSync(0, "utf8"));
const key = "analyst.workspace.session.v1", draft = "analyst.workspace.draft.v2";
const shared = {[key]: "old-shared-session", [draft]: "old shared draft"};
const tab = {[key]: "old-tab-session", [draft]: "old tab draft"};
function storage(state, denied) {
    return Object.fromEntries(["getItem", "setItem", "removeItem"].map(operation =>
        [operation, (name, value) => {
            if (denied.includes(operation)) throw new Error("Storage denied");
            if (operation === "getItem") return state[name] ?? null;
            if (operation === "setItem") state[name] = value;
            else if (operation === "removeItem") delete state[name];
        }]));
}
const localStorage = storage(shared, input.local_denied);
const sessionStorage = storage(tab, input.tab_denied);
const message = {textContent: "Opening the existing eval session. No prompt is sent."};
let redirect = null;
const environment = {
    localStorage, sessionStorage, storageKey: key,
    window: {localStorage, sessionStorage, location: {replace: value => {redirect = value;}}},
    document: {querySelector: selector => {if (selector !== "p") throw new Error(selector); return message;}},
};
vm.createContext(environment);
vm.runInContext(input.bootstrap, environment);
vm.runInContext(input.selector + "globalThis.selectedSession = readSession();", environment);
process.stdout.write(JSON.stringify({shared, tab, selected: environment.selectedSession, redirect, message: message.textContent}));
"""


def bootstrap_script(session_id: str = "intended-eval-session") -> str:
    observer = object.__new__(EvalBrowserObserver)
    observer._bootstrap_sessions = {"known-token": session_id}
    request = SimpleNamespace(path_params={"token": "known-token"})
    response = asyncio.run(observer._bootstrap(request))
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    script = re.search(r'<script nonce="[^"]+">(.*?)</script>', response.body.decode(), re.S)
    assert script
    return script.group(1)


@pytest.mark.parametrize(
    ("tab_denied", "local_denied", "opens"),
    [
        ([], [], True),
        (["getItem", "setItem", "removeItem"], [], True),
        ([], ["getItem", "setItem", "removeItem"], True),
        (["getItem", "setItem", "removeItem"], ["getItem", "setItem", "removeItem"], False),
        (["setItem"], [], False),
        ([], ["removeItem"], False),
    ],
)
def test_bootstrap_selects_intended_tab_or_reports_storage_failure(
    tab_denied: list[str], local_denied: list[str], opens: bool
) -> None:
    node = shutil.which("node")
    assert node, "Node is required for the source-bound browser-storage contract check"
    source = (ROOT / "frontend/src/AgentWorkspace.tsx").read_text(encoding="utf-8")
    selector = source[source.index("const readSession =") : source.index("const saveSession =")]
    result = subprocess.run(
        [node, "-e", DRIVER],
        input=json.dumps(
            {
                "bootstrap": bootstrap_script(),
                "selector": selector,
                "tab_denied": tab_denied,
                "local_denied": local_denied,
            }
        ),
        text=True,
        capture_output=True,
        check=True,
        timeout=10,
    )
    observed = json.loads(result.stdout)
    key, draft = "analyst.workspace.session.v1", "analyst.workspace.draft.v2"
    if opens:
        assert observed["redirect"] == "/"
        assert observed["selected"] == "intended-eval-session"
        assert "could not be restored" not in observed["message"]
    else:
        assert observed["redirect"] is None
        assert "could not be restored" in observed["message"]
        assert "No prompt was sent" in observed["message"]
    if not tab_denied:
        assert observed["tab"][key] == "intended-eval-session"
        assert draft not in observed["tab"]
    if not local_denied:
        assert observed["shared"][key] == "intended-eval-session"
        assert draft not in observed["shared"]


def test_bootstrap_rejects_unknown_token_and_escapes_embedded_session() -> None:
    observer = object.__new__(EvalBrowserObserver)
    observer._bootstrap_sessions = {}
    request = SimpleNamespace(path_params={"token": "unknown-token"})
    assert asyncio.run(observer._bootstrap(request)).status_code == 404
    assert "</script" not in bootstrap_script('</script><img src=x onerror="bad">')

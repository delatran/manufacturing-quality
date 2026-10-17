// This bootstrap runs on previous images too; only their existing persistence
// module is required. The parent remains the sole writer of its primary ETag.
export const PREPARE_UPGRADE = String.raw`
import json, os, signal, sqlite3, sys, tempfile, time
from contextlib import closing
from pathlib import Path
from analyst.cloud_runtime import SnapshotBridge

root = Path("/runtime")
database = root / "workspace.sqlite3"
marker = sys.argv[1]
stage_etag = sys.argv[2] or None
deadline = time.monotonic() + 25
fences = ("analyst_image_upgrade_runs", "analyst_image_upgrade_steering")
installed = False

def release():
    if database.exists():
        with closing(sqlite3.connect(database)) as db:
            with db:
                for name in fences:
                    db.execute("DROP TRIGGER IF EXISTS " + name)

def progress(*args):
    if time.monotonic() > deadline:
        raise RuntimeError("Upgrade checkpoint did not finish")

try:
    if sys.argv[3] == "release":
        release()
        print(json.dumps({"status": "released"}), flush=True)
        sys.exit(0)
    runtime = Path("/proc/1/cmdline").read_bytes().split(b"\0")
    if b"analyst.cloud_runtime" not in runtime:
        raise RuntimeError("Unknown container entrypoint")
    if database.exists():
        with closing(sqlite3.connect(database)) as db:
            db.execute("BEGIN IMMEDIATE")
            if sys.argv[3] != "inspect":
                for name, table in zip(fences, ("runs", "steering")):
                    db.execute("CREATE TRIGGER IF NOT EXISTS " + name + " BEFORE INSERT ON " + table + " BEGIN SELECT RAISE(ABORT, 'Workspace is updating; retry shortly'); END")
            if db.execute("SELECT COUNT(*) FROM runs WHERE state='running'").fetchone()[0]:
                db.rollback()
                print(json.dumps({"status": "busy"}), flush=True)
                sys.exit(0)
            db.commit()
            installed = sys.argv[3] != "inspect"
    if sys.argv[3] == "inspect":
        print(json.dumps({"status": "idle"}), flush=True)
        sys.exit(0)
    source = SnapshotBridge(root, "http://state.internal/snapshot")
    with tempfile.TemporaryDirectory(prefix="analyst-upgrade-") as temporary:
        staging = Path(temporary)
        for path in source._paths():
            progress()
            target = staging / path.relative_to(root)
            target.parent.mkdir(parents=True, exist_ok=True)
            if path.suffix == ".sqlite3":
                with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
                    with closing(sqlite3.connect(target)) as destination:
                        db.backup(destination, pages=256, progress=progress, sleep=0.05)
            else:
                target.write_bytes(path.read_bytes())
        saved = SnapshotBridge(staging, "http://state.internal/upgrade-snapshot")
        saved.etag = stage_etag
        saved.checkpoint(force=True)
        saved.close()
    source.close()
    # SIGTERM closes listener and existing WebSockets before the old runtime's
    # lifespan checkpoints. The unique marker proves its archive was acknowledged.
    (root / ".upgrade-handoff.json").write_text(json.dumps({"marker": marker}))
    print(json.dumps({"status": "prepared", "staged_etag": saved.etag}), flush=True)
    os.kill(1, signal.SIGTERM)
except Exception as error:
    if installed:
        release()
    print(json.dumps({"status": "failed", "error": type(error).__name__}), flush=True)
    sys.exit(1)
`;

export class UpgradeDeferred extends Error {}

export function upgradeReadAllowed(request) {
  return ["GET", "HEAD"].includes(request.method) ||
    (request.method === "POST" && /^\/api\/sessions\/[^/]+\/cancel$/.test(new URL(request.url).pathname));
}

export async function containsHandoffMarker(bytes, marker) {
  const reader = new Blob([bytes]).stream().pipeThrough(new DecompressionStream("gzip")).getReader();
  const decoder = new TextDecoder();
  let header = new Uint8Array(512), used = 0, remaining = 0, content = null, contentUsed = 0, total = 0;
  try {
    while (true) {
      const {done, value} = await reader.read();
      if (done) return false;
      total += value.byteLength;
      if (total > 256 * 1024 * 1024) return false;
      let offset = 0;
      while (offset < value.byteLength) {
        if (remaining) {
          const length = Math.min(remaining, value.byteLength - offset);
          if (content && contentUsed < content.length) {
            const selected = Math.min(length, content.length - contentUsed);
            content.set(value.subarray(offset, offset + selected), contentUsed); contentUsed += selected;
          }
          offset += length; remaining -= length;
          if (!remaining && content) {
            try { if (JSON.parse(decoder.decode(content)).marker === marker) return true; } catch {}
            content = null;
          }
          continue;
        }
        const length = Math.min(512 - used, value.byteLength - offset);
        header.set(value.subarray(offset, offset + length), used); used += length; offset += length;
        if (used !== 512) continue;
        const name = decoder.decode(header.subarray(0, 100)).replace(/\0.*$/, "");
        const size = Number.parseInt(decoder.decode(header.subarray(124, 136)).replace(/\0.*$/, "").trim() || "0", 8);
        if (!Number.isSafeInteger(size) || size < 0) return false;
        if (name === ".upgrade-handoff.json" && size > 0 && size <= 256) { content = new Uint8Array(size); contentUsed = 0; }
        remaining = Math.ceil(size / 512) * 512; used = 0;
      }
    }
  } finally { await reader.cancel(); }
}

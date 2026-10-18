FROM python:3.12-slim-bookworm AS sqlite
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /sqlite
RUN python - <<'PYSQLITE'
import hashlib
import io
import json
import pathlib
import re
import subprocess
import urllib.request
import zipfile

url = "https://sqlite.org/2026/sqlite-amalgamation-3540100.zip"
archive_sha3 = "92281c86e2957f9b9305890f9ac04204caa7b677cc868dc326d942689df28e30"
source_sha3 = "e2b869d3003b33c942b7dd7063604fbc53553e1b66609570df39b9b7b558ee81"
expected_source_id = "2026-10-16 14:45:20 555e07ef33f69a0957d24d8e555b702464a131d6f1607f4c3c1e4551e8e6ca76"
with urllib.request.urlopen(url, timeout=120) as response:
    archive = response.read()
if hashlib.sha3_256(archive).hexdigest() != archive_sha3:
    raise SystemExit("SQLite archive integrity verification failed")
with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
    for name in ("sqlite3.c", "sqlite3.h"):
        pathlib.Path(name).write_bytes(bundle.read("sqlite-amalgamation-3540100/" + name))
if hashlib.sha3_256(pathlib.Path("sqlite3.c").read_bytes()).hexdigest() != source_sha3:
    raise SystemExit("SQLite amalgamation integrity verification failed")
source_id = re.search(r'^#define SQLITE_SOURCE_ID\s+"(.+)"', pathlib.Path("sqlite3.h").read_text(), re.M).group(1)
if source_id != expected_source_id:
    raise SystemExit("SQLite source identity verification failed")
flags = [
    "-O2", "-fno-strict-aliasing", "-fPIC", "-shared",
    "-Wl,-soname,libsqlite3.so.0", "-Wl,-z,relro,-z,now",
    "-DSQLITE_THREADSAFE=1", "-DSQLITE_ENABLE_COLUMN_METADATA",
    "-DSQLITE_ENABLE_FTS3", "-DSQLITE_ENABLE_FTS3_PARENTHESIS",
    "-DSQLITE_ENABLE_FTS4", "-DSQLITE_ENABLE_FTS5", "-DSQLITE_ENABLE_RTREE",
    "-DSQLITE_ENABLE_DBSTAT_VTAB", "-DSQLITE_ENABLE_PREUPDATE_HOOK",
    "-DSQLITE_ENABLE_SESSION", "-DSQLITE_ENABLE_UNLOCK_NOTIFY",
    "-DSQLITE_ENABLE_MATH_FUNCTIONS", "-DSQLITE_SECURE_DELETE", "-DSQLITE_USE_URI=1",
    "-DSQLITE_MAX_VARIABLE_NUMBER=250000",
]
out = pathlib.Path("/sqlite-out")
out.mkdir()
subprocess.run(["gcc", *flags, "sqlite3.c", "-o", str(out / "libsqlite3.so.0"), "-ldl", "-lm", "-lpthread"], check=True)
manifest = {
    "version": "3.54.1", "source_id": source_id, "source_url": url,
    "archive_sha3_256": archive_sha3, "amalgamation_sha3_256": source_sha3,
    "library_sha256": hashlib.sha256((out / "libsqlite3.so.0").read_bytes()).hexdigest(),
    "compiler": subprocess.check_output(["gcc", "--version"], text=True).splitlines()[0],
    "compiler_flags": flags,
}
(out / "sqlite-build.json").write_text(json.dumps(manifest, indent=2) + "\n")
PYSQLITE

FROM node:24-bookworm-slim AS frontend
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
COPY knowledge/glossary.json /build/knowledge/glossary.json
RUN npm run build:production

FROM python:3.12-slim-bookworm
COPY --from=sqlite /sqlite-out/libsqlite3.so.0 /usr/local/lib/libsqlite3.so.0
COPY --from=sqlite /sqlite-out/sqlite-build.json /usr/local/share/sqlite-build.json
RUN ldconfig
RUN python - <<'PYSQLITE'
import hashlib
import json
import pathlib
import sqlite3

manifest = json.loads(pathlib.Path("/usr/local/share/sqlite-build.json").read_text())
with sqlite3.connect(":memory:") as db:
    actual = db.execute("SELECT sqlite_version(), sqlite_source_id()").fetchone()
    if actual != (manifest["version"], manifest["source_id"]):
        raise SystemExit("Python did not load the verified SQLite build")
    if sqlite3.threadsafety != 3:
        raise SystemExit("SQLite serialized thread safety is required")
    if db.execute("SELECT json_extract('{\"value\": 1}', '$.value')").fetchone()[0] != 1:
        raise SystemExit("SQLite JSON support is required")
    if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise SystemExit("SQLite integrity check failed")
loaded = {line.split()[-1] for line in pathlib.Path("/proc/self/maps").read_text().splitlines() if "libsqlite3.so" in line}
if loaded != {"/usr/local/lib/libsqlite3.so.0"}:
    raise SystemExit("Python SQLite library path does not match the verified build")
library = pathlib.Path("/usr/local/lib/libsqlite3.so.0")
if hashlib.sha256(library.read_bytes()).hexdigest() != manifest["library_sha256"]:
    raise SystemExit("SQLite shared library integrity verification failed")
print(json.dumps({"sqlite_version": actual[0], "sqlite_source_id": actual[1], "library": str(library)}))
PYSQLITE
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    MPLBACKEND=Agg \
    MPLCONFIGDIR=/tmp/matplotlib \
    OPENBLAS_NUM_THREADS=1 \
    OMP_NUM_THREADS=1
WORKDIR /app
COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock \
    && groupadd --gid 10001 analyst \
    && useradd --uid 10001 --gid analyst --create-home analyst \
    && mkdir -p /runtime /app/.runtime \
    && chown analyst:analyst /runtime /app/.runtime
COPY analyst/ ./analyst/
COPY config/ ./config/
COPY knowledge/ ./knowledge/
COPY data/master_data.csv ./data/master_data.csv
COPY DATA_QUALITY_TEMPLATE.xlsx DATA_QUALITY.xlsx ./
COPY --from=frontend /build/frontend/dist/ ./frontend/dist/
USER analyst
EXPOSE 8000
CMD ["python", "-m", "analyst.cloud_runtime"]

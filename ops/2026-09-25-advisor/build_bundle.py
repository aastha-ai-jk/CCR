"""Build the GPU-box deployment bundle for the CCR advisor change.

Payload files are taken from git blobs of NEW (LF line endings, exactly what
is committed); MANIFEST.tsv records each file's sha256 at BASE (the version
the server is expected to run) and at NEW. Output:
    ops/2026-09-25-advisor/dist/ccr-advisor-20260925.tar.gz

    python ops/2026-09-25-advisor/build_bundle.py [NEW_COMMIT] [--server-hashes FILE]

--server-hashes takes `sha256sum` output from the server (paths like ./x/y.py).
Each listed file's real server hash becomes its base, and every other deployable
file the server has at an older version (or lacks) is added to the bundle too.
"""
from __future__ import annotations

import datetime
import hashlib
import io
import subprocess
import sys
import tarfile
from pathlib import Path

BASE = "6763a01"                                    # feature/digital-twin-ml tip = server
_args = sys.argv[1:]
SERVER_HASHES = None
if "--server-hashes" in _args:
    i = _args.index("--server-hashes")
    SERVER_HASHES = Path(_args[i + 1])
    del _args[i:i + 2]
NEW = _args[0] if _args else "HEAD"
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
# every app file changed since BASE (the ops scripts themselves are not deployed)
FILES = [f for f in subprocess.run(["git", "diff", "--name-only", BASE, NEW], cwd=ROOT,
                                   capture_output=True, text=True, check=True).stdout.splitlines()
         if not f.startswith("ops/")]
NAME = "ccr-advisor-" + datetime.date.today().strftime("%Y%m%d")
# server-hash mode: what the server's find covered, and what is never deployed
HASHED_EXT = (".py", ".ts", ".tsx", ".js", ".json", ".html", ".txt", ".css")
PRUNED = ("data/", "models/", "node_modules/", "frontend/node_modules/", ".venv/", ".git/",
          "frontend/.next/")
NOT_DEPLOYED = ("ops/", "docs/progress-report/", "docs/twin-dashboard/", "twin/exp_", ".claude/",
                "frontend/.claude/", "OPC Data call syntex")


def blob(rev: str, path: str) -> bytes | None:
    r = subprocess.run(["git", "cat-file", "blob", f"{rev}:{path}"],
                       cwd=ROOT, capture_output=True)
    return r.stdout if r.returncode == 0 else None


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def lf(p: Path) -> bytes:
    return p.read_bytes().replace(b"\r\n", b"\n")


def add(tar: tarfile.TarFile, arcname: str, data: bytes, mode: int = 0o644) -> None:
    info = tarfile.TarInfo(f"{NAME}/{arcname}")
    info.size, info.mode = len(data), mode
    tar.addfile(info, io.BytesIO(data))


def server_bases() -> dict[str, str]:
    """{path: base sha256 or 'NEW'} from the server's sha256sum list."""
    have = {}
    for line in SERVER_HASHES.read_text(encoding="utf8").splitlines():
        h, p = line.split(None, 1)
        have[p[2:] if p.startswith("./") else p] = h
    tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True,
                             check=True).stdout.splitlines()
    bases = {}
    for f in tracked:
        if not f.endswith(HASHED_EXT) or f.startswith(PRUNED) or f.startswith(NOT_DEPLOYED):
            continue
        new = blob(NEW, f)
        if new is not None and have.get(f) != sha(new):
            bases[f] = have.get(f, "NEW")
    return bases


def main() -> None:
    rows = []
    out = HERE / "dist" / f"{NAME}.tar.gz"
    out.parent.mkdir(exist_ok=True)
    bases = server_bases() if SERVER_HASHES else {}
    files = sorted(set(FILES) | set(bases))
    with tarfile.open(out, "w:gz") as tar:
        for f in files:
            new = blob(NEW, f)
            if new is None:
                sys.exit(f"{f} not found at {NEW}")
            if f in bases:
                base = bases[f]
            elif SERVER_HASHES and f.endswith(HASHED_EXT) and not f.startswith(PRUNED):
                continue                                # server already has the NEW version
            else:
                old = blob(BASE, f)
                base = sha(old) if old is not None else "NEW"
            rows.append(f"{f}\t{base}\t{sha(new)}")
            add(tar, f"payload/{f}", new)
        add(tar, "MANIFEST.tsv", ("\n".join(rows) + "\n").encode())
        for s in ("apply_advisor.sh", "rollback_advisor.sh"):
            add(tar, s, lf(HERE / s), 0o755)
        add(tar, "README.md", lf(HERE / "README.md"))
    print("\n".join(rows))
    src = f"server hashes {SERVER_HASHES.name}" if SERVER_HASHES else f"base {BASE}"
    print(f"wrote {out} ({out.stat().st_size / 1024:.0f} KiB, {len(rows)} files) from {NEW} ({src})")


if __name__ == "__main__":
    main()

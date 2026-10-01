"""Build the GPU-box deployment bundle for the CCR advisor change.

Payload files are taken from git blobs of NEW (LF line endings, exactly what
is committed); MANIFEST.tsv records each file's sha256 at BASE (the version
the server is expected to run) and at NEW. Output:
    ops/2026-09-25-advisor/dist/ccr-advisor-20260925.tar.gz

    python ops/2026-09-25-advisor/build_bundle.py [NEW_COMMIT]
"""
from __future__ import annotations

import hashlib
import io
import subprocess
import sys
import tarfile
from pathlib import Path

BASE = "6763a01"                                    # feature/digital-twin-ml tip = server
NEW = sys.argv[1] if len(sys.argv) > 1 else "HEAD"
FILES = [
    "backend/main.py",
    "backend/decisions.py",
    "backend/static/ccr-kiln3-advisor.html",
    "twin/optimize.py",
    "twin/train_gbt.py",
    "frontend/app/twin/page.tsx",
    "frontend/components/Mimic.tsx",
    "frontend/lib/api.ts",
]
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
NAME = "ccr-advisor-20260925"


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


def main() -> None:
    rows = []
    out = HERE / "dist" / f"{NAME}.tar.gz"
    out.parent.mkdir(exist_ok=True)
    with tarfile.open(out, "w:gz") as tar:
        for f in FILES:
            new = blob(NEW, f)
            if new is None:
                sys.exit(f"{f} not found at {NEW}")
            old = blob(BASE, f)
            rows.append(f"{f}\t{sha(old) if old is not None else 'NEW'}\t{sha(new)}")
            add(tar, f"payload/{f}", new)
        add(tar, "MANIFEST.tsv", ("\n".join(rows) + "\n").encode())
        for s in ("apply_advisor.sh", "rollback_advisor.sh"):
            add(tar, s, lf(HERE / s), 0o755)
        add(tar, "README.md", lf(HERE / "README.md"))
    print("\n".join(rows))
    print(f"wrote {out} ({out.stat().st_size / 1024:.0f} KiB) from {NEW} (base {BASE})")


if __name__ == "__main__":
    main()

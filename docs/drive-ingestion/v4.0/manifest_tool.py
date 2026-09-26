"""Verify or regenerate docs/drive-ingestion/v4.0/MANIFEST.sha256.

The repository has no generator for this file, so it is hand maintained and
silently rots as soon as any listed file is edited. This tool makes the drift
visible and can rewrite the manifest deliberately.

Usage:
    python manifest_tool.py check     # report mismatches, non-zero exit on drift
    python manifest_tool.py regen     # rewrite MANIFEST.sha256 from the tree
"""

import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MANIFEST = ROOT / "MANIFEST.sha256"


def entries() -> list[tuple[str, str]]:
    out = []
    for line in MANIFEST.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, rel = line.split(None, 1)
        out.append((digest.strip(), rel.strip()))
    return out


def digest_of(rel: str) -> str | None:
    """Hash the committed (LF) form of a file.

    MANIFEST.sha256 records the hash of the git blob, i.e. LF content. On a
    Windows checkout with core.autocrlf=true the working tree holds CRLF, so
    hashing raw working-tree bytes reports a false mismatch for every entry.
    Line endings are therefore normalized to LF before hashing.
    """
    target = ROOT / rel
    if not target.is_file():
        return None
    raw = target.read_bytes()
    return hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest()


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "check"
    listed = entries()

    if mode == "regen":
        lines = [f"{digest_of(rel)}  {rel}" for _digest, rel in listed]
        MANIFEST.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"rewrote {MANIFEST.name} with {len(lines)} entries")
        return 0

    if mode != "check":
        print(__doc__)
        return 2

    ok = mismatch = missing = 0
    problems = []
    for expected, rel in listed:
        actual = digest_of(rel)
        if actual is None:
            missing += 1
            problems.append(f"MISSING  {rel}")
        elif actual != expected:
            mismatch += 1
            problems.append(f"CHANGED  {rel}\n  expected {expected}\n  actual   {actual}")
        else:
            ok += 1

    on_disk = {str(p.relative_to(ROOT)).replace("\\", "/") for p in ROOT.rglob("*") if p.is_file()}
    unlisted = sorted(
        name
        for name in on_disk
        if name not in {rel for _d, rel in listed}
        and "__pycache__" not in name
        and not name.startswith(".venv")
        and name != "MANIFEST.sha256"
    )

    print(f"ok={ok} mismatch={mismatch} missing={missing}")
    for line in problems:
        print(line)
    print(f"unlisted files ({len(unlisted)}):")
    for name in unlisted:
        print(f"  {name}")
    return 1 if (mismatch or missing) else 0


if __name__ == "__main__":
    raise SystemExit(main())

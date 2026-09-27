"""Verify or regenerate the Drive ingestion contract manifest.

`MANIFEST.sha256` pins the reference contracts so a reviewer can tell that a
document, a schema or a validation script did not change underneath a
verification report. This tool checks that pinning and can rewrite it.

What it hashes, and the limit of that:

    The digest is of the file's bytes with CRLF normalised to LF. The manifest
    records the committed (LF) form, and this repository has no
    `.gitattributes` while `core.autocrlf` may be true, so a Windows checkout
    holds CRLF where the blob holds LF. Normalising is what makes the two
    comparable. It is lossy in one direction: a file whose committed content
    genuinely contains a literal carriage return can never match its own
    recorded digest. No file in this tree does, and the check reports that as a
    mismatch rather than passing silently.

    This tool does not read git objects, so it also verifies uncommitted
    working-tree state. It is the pre-commit signal, not a commit attestation.

    The manifest is a mutable file inside the tree it attests to, so it cannot
    attest to itself: regenerating it against a bad tree and committing both
    will pass. Detecting that needs an anchor outside the tree.

Usage:
    python manifest_tool.py check     # report drift, non-zero exit on any finding
    python manifest_tool.py regen     # rewrite MANIFEST.sha256 from the tree

Exit codes:
    0  everything listed is present and unchanged, nothing unlisted
    1  drift: changed, missing, or an unlisted file
    2  usage error or unreadable manifest
"""

import hashlib
import sys
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent
MANIFEST = ROOT / "MANIFEST.sha256"
MANIFEST_NAME = "MANIFEST.sha256"

# Directories never part of the contract set. Matched on path components, not
# as a substring of the joined path, so a file named pycache_notes.md is kept.
SKIP_DIR_PARTS = {"__pycache__"}


def _skipped(rel: str) -> bool:
    parts = PurePosixPath(rel).parts[:-1]
    return any(p in SKIP_DIR_PARTS or p.startswith(".venv") for p in parts)


def entries() -> list[tuple[str, str]]:
    """Parse the manifest into (digest, relative path) pairs."""
    out: list[tuple[str, str]] = []
    for number, line in enumerate(MANIFEST.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        parts = line.split(None, 1)
        if len(parts) != 2 or len(parts[0]) != 64:
            raise ValueError(f"{MANIFEST_NAME}:{number} is not a digest and a path")
        out.append((parts[0], parts[1].strip()))
    return out


def digest_of(rel: str) -> str | None:
    """SHA-256 of the file with CRLF normalised to LF, or None if absent."""
    target = ROOT / rel
    if not target.is_file():
        return None
    return hashlib.sha256(target.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def tree_files() -> list[str]:
    """Every file under the contract tree, manifest itself excluded."""
    out = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(ROOT).as_posix()
        if rel == MANIFEST_NAME or _skipped(rel):
            continue
        out.append(rel)
    return sorted(out)


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "check"
    if mode not in {"check", "regen"}:
        print(__doc__)
        return 2

    try:
        listed = entries()
    except (OSError, ValueError) as exc:
        print(f"::error::{exc}")
        return 2

    on_disk = tree_files()
    listed_paths = {rel for _digest, rel in listed}
    unlisted = sorted(set(on_disk) - listed_paths)
    missing = sorted(listed_paths - set(on_disk))

    if mode == "regen":
        # A manifest that records a deletion is a corruption the next check
        # cannot tell apart from ordinary drift, so refuse rather than write it.
        if missing:
            print("::error::refusing to regenerate, these listed files are gone:")
            for rel in missing:
                print(f"  {rel}")
            print("Remove them from the manifest by hand if the deletion is intended.")
            return 1
        digests: list[tuple[str, str]] = []
        for rel in on_disk:
            digest = digest_of(rel)
            if digest is None:
                print(f"::error::refusing to regenerate, {rel} became unreadable")
                return 2
            digests.append((digest, rel))
        MANIFEST.write_text(
            "\n".join(f"{digest}  {rel}" for digest, rel in digests) + "\n", encoding="utf-8"
        )
        print(f"rewrote {MANIFEST_NAME} with {len(digests)} entries")
        return 0

    ok = 0
    changed: list[str] = []
    for expected, rel in listed:
        actual = digest_of(rel)
        if actual is None:
            continue
        if actual == expected:
            ok += 1
        else:
            changed.append(rel)

    print(f"ok={ok} changed={len(changed)} missing={len(missing)} unlisted={len(unlisted)}")
    for rel in changed:
        print(f"CHANGED  {rel}")
    for rel in missing:
        print(f"MISSING  {rel}")
    for rel in unlisted:
        print(f"UNLISTED {rel}")

    # Guidance has to be per case. regen adopts new files but refuses deletions,
    # so telling a user to run it for a MISSING entry would send them into a
    # command that errors out.
    if unlisted:
        print("add them with: python manifest_tool.py regen")
    if changed:
        print("restore the file, or accept the change with: python manifest_tool.py regen")
    if missing:
        print(
            "regen refuses a deletion on purpose; if the removal is intended, "
            "delete these lines from MANIFEST.sha256 by hand"
        )

    # An unlisted file is the one drift class regen exists to fix, so it has to
    # fail the gate too rather than being printed and discarded.
    return 1 if (changed or missing or unlisted) else 0


if __name__ == "__main__":
    raise SystemExit(main())

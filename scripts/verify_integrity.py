"""Prove that integrating the two OCR services did not change either of them.

    python scripts/verify_integrity.py                       # checks both rules below
    python scripts/verify_integrity.py --charge-sheet-repo C:\\path\\to\\CHARGE_SHEET_OCR   # also compare with the original repo
    python scripts/verify_integrity.py --write-manifest C:\\path\\to\\CHARGE_SHEET_OCR      # (re)create the vendor manifest

1. **IR_OCR (the host).** Every file that existed at the base commit recorded in ``unified/provenance.json`` is identical
   in the working tree (compared by git, so line-ending conversion does not matter). Files may only be *added*.
2. **CHARGE_SHEET_OCR (the vendored snapshot).** Every file in ``vendor/charge_sheet_ocr`` has exactly the bytes of the
   blob recorded in ``unified/vendor_manifest.json`` (the blob ids of the source commit), nothing is missing, and the only
   extra files are the ones listed as local additions (``.gitattributes`` pins the bytes; ``.env`` is a local copy of the
   original's settings and is git-ignored).

Exit code 0 = unchanged. Prints every difference otherwise. Uses only git and the standard library.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROVENANCE = ROOT / "unified" / "provenance.json"
MANIFEST = ROOT / "unified" / "vendor_manifest.json"
VENDOR = ROOT / "vendor" / "charge_sheet_ocr"
# present in the vendor folder but not part of the source commit
LOCAL_ADDITIONS = {".gitattributes", ".env"}
IGNORED_DIRS = {"__pycache__", ".pytest_cache", ".venv", "data", "uploads", "logs"}


def git(*args: str, cwd: Path | None = None) -> str:
    out = subprocess.run(["git", *args], cwd=cwd or ROOT, capture_output=True, text=True, check=False)
    if out.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {out.stderr.strip()}")
    return out.stdout


def blob_ids(repo: Path, commit: str) -> dict:
    ids = {}
    for line in git("ls-tree", "-r", commit, cwd=repo).splitlines():
        meta, path = line.split("\t", 1)
        ids[path] = meta.split()[2]
    return ids


def write_manifest(repo: Path) -> None:
    commit = json.loads(PROVENANCE.read_text(encoding="utf-8"))["charge_sheet_ocr"]["commit"]
    files = blob_ids(repo, commit)
    MANIFEST.write_text(json.dumps({"commit": commit, "files": files}, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {MANIFEST.relative_to(ROOT)}: {len(files)} files of {commit[:10]}")


def check_host() -> list:
    base = json.loads(PROVENANCE.read_text(encoding="utf-8"))["ir_ocr"]["commit"]
    problems = []
    # working tree (staged + unstaged) against the base commit: only additions may appear
    for line in git("diff", "--name-status", "--diff-filter=DMRTUXB", base, "--").splitlines():  # everything but Added
        status, path = line.split("\t", 1)[0], line.split("\t")[-1]
        problems.append(f"IR_OCR file changed ({status}): {path}")
    return problems


def check_vendor(charge_sheet_repo: Path | None) -> list:
    problems = []
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    expected = manifest["files"]
    present = set()
    for path in VENDOR.rglob("*"):
        rel = path.relative_to(VENDOR)
        if path.is_dir() or IGNORED_DIRS & set(rel.parts) or rel.suffix in (".pyc", ".log"):
            continue  # caches and what running the service creates (databases, uploads, logs)
        present.add(rel.as_posix())
    for rel, blob in expected.items():
        file = VENDOR / rel
        if not file.is_file():
            problems.append(f"vendored file missing: {rel}")
        elif git("hash-object", "--no-filters", str(file)).strip() != blob:
            problems.append(f"vendored file differs from commit {manifest['commit'][:10]}: {rel}")
    for rel in sorted(present - set(expected) - LOCAL_ADDITIONS):
        problems.append(f"unexpected file in the vendored snapshot: {rel}")
    if charge_sheet_repo is not None:
        commit = manifest["commit"]
        if blob_ids(charge_sheet_repo, commit) != expected:
            problems.append("the manifest does not match the original CHARGE_SHEET_OCR repository at that commit")
        dirty = git("status", "--porcelain", cwd=charge_sheet_repo).strip()
        if dirty:
            problems.append("the original CHARGE_SHEET_OCR working tree has uncommitted changes:\n" + dirty)
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--charge-sheet-repo", type=Path, help="also compare with the original CHARGE_SHEET_OCR repository")
    ap.add_argument("--write-manifest", type=Path, metavar="CS_REPO", help="create unified/vendor_manifest.json from the original repository")
    args = ap.parse_args()
    if args.write_manifest:
        write_manifest(args.write_manifest)
        return 0
    problems = check_host() + check_vendor(args.charge_sheet_repo)
    for p in problems:
        print("FAIL", p)
    if not problems:
        base = json.loads(PROVENANCE.read_text(encoding="utf-8"))
        print(f"OK  IR_OCR files identical to {base['ir_ocr']['commit'][:10]} (additions only)")
        print(f"OK  vendored CHARGE_SHEET_OCR identical to {base['charge_sheet_ocr']['commit'][:10]}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

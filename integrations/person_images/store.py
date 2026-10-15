"""Where person images live, and the manifest that describes them.

    <output>/<uploaded file name>_<ext>/person_images/<file name> image.jpeg
                                                     /<file name> image (2).jpeg
                                                     /rerun_2/<file name> image.jpeg      (a re-run keeps its own folder)
                                                     /manifest.json
    <output>/<uploaded file name>_<ext>/report_result.json
    <output>/.person_images_index.json            document id -> folder name

The folder is named after the file that was uploaded (like the original image extractor: ``<name>_<ext>``, so a.pdf and
a.docx stay apart). Names are sanitised (no separators, no reserved names, bounded length); if another document already
uses the name the folder gets `` (2)``, `` (3)`` ... The document id -> folder link is kept in the index file and in each
manifest, so images always belong to the right document. Folders created by earlier versions (named by document id,
images under ``runs/<run_id>``) are still found and shown.

Nothing is ever deleted: a re-run gets its own sub-folder and the manifest keeps the history. Only files listed in the
manifest are ever served, and every resolved path must stay inside the run folder.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

MANIFEST_SCHEMA_VERSION = "1.1.0"
INDEX_NAME = ".person_images_index.json"
MAX_NAME = 90          # longest sanitised file-name part used in a folder or image name
MAX_PATH_CHARS = 240   # keep full paths below the classic Windows limit

DOC_ID = re.compile(r"^[0-9a-fA-F-]{36}$")  # same shape the OCR application uses for document ids
RUN_ID = re.compile(r"^run_\d{8}T\d{6}Z_[0-9a-f]{6}$")
RUN_FOLDER = re.compile(r"^(?:rerun_\d{1,4}|runs/run_\d{8}T\d{6}Z_[0-9a-f]{6})?$")  # "" = the base folder
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".webp"}
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}

# statuses of one extraction run
QUEUED, PROCESSING = "queued", "processing"
SUCCEEDED, PARTIAL, NO_IMAGES, FAILED, UNSUPPORTED = "succeeded", "partial", "no_person_images", "failed", "unsupported"
ACTIVE = (QUEUED, PROCESSING)
USABLE = (SUCCEEDED, PARTIAL, NO_IMAGES)  # a run whose images can be shown

_lock = threading.RLock()


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def output_root() -> Path:
    """``IR_PERSON_IMAGES_OUTPUT`` (tests / deployments) or ``<IR_OCR>/output``."""
    env = os.environ.get("IR_PERSON_IMAGES_OUTPUT")
    root = Path(env) if env else Path(__file__).resolve().parents[2] / "output"
    return root.resolve()


def new_run_id() -> str:
    return f"run_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{secrets.token_hex(3)}"


# ---------------------------------------------------------------------------
# names
# ---------------------------------------------------------------------------

_ALLOWED_PUNCT = set("._-()@,+&'")


def sanitize_name(text: str, limit: int = MAX_NAME) -> str:
    """A file-system-safe version of a name: letters (any script, including combining marks), digits, space and
    ``. _ - ( ) @ , + & '`` only; everything else (separators, ``: * ? " < > |``, control characters) becomes ``_``."""
    kept = []
    for ch in str(text or ""):
        if ch.isalnum() or ch in _ALLOWED_PUNCT or ch.isspace() or unicodedata.category(ch).startswith("M"):
            kept.append(" " if ch.isspace() else ch)
        else:
            kept.append("_")
    cleaned = re.sub(r" +", " ", "".join(kept)).strip(" .")
    cleaned = cleaned[:limit].strip(" .")
    if not cleaned:
        return "document"
    if cleaned.split(".")[0].upper() in _RESERVED:
        cleaned = f"_{cleaned}"
    return cleaned


def _split_name(original_filename: str):
    """(stem, extension) of an uploaded file name, parsed by hand: ``pathlib`` would read ``a:b.docx`` as a drive."""
    name = str(original_filename or "").replace("\\", "/").split("/")[-1]
    stem, dot, ext = name.rpartition(".")
    return (stem, ext.lower()) if dot else (name, "")


def folder_name_for(original_filename: str, limit: int = MAX_NAME) -> str:
    """``Report-12 (draft).docx`` -> ``Report-12 (draft)_docx``  (same convention as the original extractor).

    ``limit`` bounds the file-name part; ``ensure_dir`` lowers it when the output location itself is deep."""
    stem, ext = _split_name(original_filename)
    stem = sanitize_name(stem, limit)
    ext = re.sub(r"[^0-9a-z]", "", ext)[:8]
    return f"{stem}_{ext}" if ext else stem


def _folder_limit(root: Path) -> int:
    """How long the folder's file-name part may be, so that ``<root>/<folder>/person_images/rerun_9/<image>`` stays
    under the classic Windows path limit (a short output path leaves the full MAX_NAME)."""
    reserve = len("\\person_images\\rerun_9\\") + 20 + len(" image (999).jpeg") + 5 + 8 + 10
    return max(12, min(MAX_NAME, MAX_PATH_CHARS - len(str(root)) - reserve))


def image_base_name(original_filename: str, directory: Path) -> str:
    """The file-name part of the image names (``<base> image.jpeg``), shortened so the full path stays bounded."""
    stem, _ = _split_name(original_filename)
    room = MAX_PATH_CHARS - len(str(directory)) - len(" image (999).jpeg") - 1
    return sanitize_name(stem, max(20, min(MAX_NAME, room)))


def image_file_name(base: str, n: int, ext: str) -> str:
    """``<base> image.jpeg`` for the first image, ``<base> image (2).jpeg`` for the second, ..."""
    return f"{base} image{'' if n == 1 else f' ({n})'}{ext}"


# ---------------------------------------------------------------------------
# document folder <-> document id
# ---------------------------------------------------------------------------

def _index_path() -> Path:
    return output_root() / INDEX_NAME


def _read_index() -> Dict[str, str]:
    try:
        data = json.loads(_index_path().read_text(encoding="utf-8"))
        docs = data.get("documents", {})
        return {str(k): str(v) for k, v in docs.items()} if isinstance(docs, dict) else {}
    except (OSError, ValueError, AttributeError):
        return {}


def _write_index(index: Dict[str, str]) -> None:
    write_json_atomic(_index_path(), {"schema_version": "1.0.0", "documents": index})


def _folder_document_id(folder: Path) -> Optional[str]:
    for candidate in (folder / "person_images" / "manifest.json", folder / "report_result.json"):
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
            if isinstance(data, dict) and DOC_ID.match(str(data.get("document_id", ""))):
                return str(data["document_id"])
        except (OSError, ValueError):
            continue
    return None


def _inside_root(name: str) -> Optional[Path]:
    """The folder for an index entry, only when it is a plain name that exists directly under the output root."""
    if not name or name != Path(name).name or name in (".", ".."):
        return None
    folder = output_root() / name
    return folder if folder.is_dir() else None


def find_dir(document_id: str) -> Optional[Path]:
    """The document's output folder, or None when it has none yet. Also finds folders made by earlier versions."""
    if not DOC_ID.match(document_id or ""):
        raise ValueError("invalid document id")
    with _lock:
        folder = _inside_root(_read_index().get(document_id, ""))
        if folder is not None:
            return folder
        root = output_root()
        if not root.is_dir():
            return None
        for child in root.iterdir():  # index missing / stale: recover from the folders themselves
            if child.is_dir() and _folder_document_id(child) == document_id:
                index = _read_index()
                index[document_id] = child.name
                _write_index(index)
                return child
    return None


def ensure_dir(document_id: str, original_filename: str) -> Path:
    """The document's output folder, created (named after the uploaded file) if it does not exist yet."""
    found = find_dir(document_id)
    if found is not None:
        return found
    with _lock:
        root = output_root()
        index = _read_index()
        taken = set(index.values())
        base = folder_name_for(original_filename, _folder_limit(root))
        n = 1
        while True:
            name = base if n == 1 else f"{base} ({n})"
            path = root / name
            if name not in taken and not path.exists():
                break
            owner = _folder_document_id(path) if path.is_dir() else None
            if owner == document_id:
                break
            n += 1
        path.mkdir(parents=True, exist_ok=True)
        index[document_id] = name
        _write_index(index)
        return path


def images_dir(document_id: str) -> Optional[Path]:
    d = find_dir(document_id)
    return d / "person_images" if d is not None else None


def run_dir(document_id: str, run: Dict[str, Any]) -> Optional[Path]:
    """The folder a run's images are in (base folder, ``rerun_N``, or the old ``runs/<run_id>``)."""
    base = images_dir(document_id)
    if base is None:
        return None
    folder = run.get("folder")
    if folder is None:  # written by an earlier version
        folder = f"runs/{run.get('run_id')}"
    if not RUN_FOLDER.match(folder):
        return None
    return base / folder if folder else base


def relative_ref(folder: str, name: str) -> str:
    """Path of an image relative to the document's folder (never absolute)."""
    return f"person_images/{folder + '/' if folder else ''}{name}"


def resolve_image(document_id: str, run_id: str, filename: str) -> Optional[Path]:
    """The image file, or None. Only files the manifest lists for that run are ever served."""
    if not (DOC_ID.match(document_id or "") and RUN_ID.match(run_id or "")) or not filename:
        return None
    manifest = read_manifest(document_id)
    run = next((r for r in (manifest or {}).get("runs", []) if r.get("run_id") == run_id), None)
    if run is None or filename not in {im.get("name") for im in run.get("images") or []}:
        return None
    folder = run_dir(document_id, run)
    if folder is None:
        return None
    folder = folder.resolve()
    candidate = (folder / filename).resolve()
    if candidate.parent != folder or not candidate.is_file() or candidate.is_symlink():
        return None
    return candidate


def write_json_atomic(path: Path, data: Any) -> None:
    """Write via a temp file and an atomic replace. Writers are serialised; on Windows a replace can be refused for a
    moment while another thread has the target open, so it is retried briefly."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    with _lock:
        for attempt in range(25):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == 24:
                    tmp.unlink(missing_ok=True)
                    raise
                time.sleep(0.02)


# ---------------------------------------------------------------------------
# manifest
# ---------------------------------------------------------------------------

def manifest_path(document_id: str) -> Optional[Path]:
    base = images_dir(document_id)
    return base / "manifest.json" if base is not None else None


def read_manifest(document_id: str) -> Optional[Dict[str, Any]]:
    """The manifest, or None when there is none yet (or it cannot be read: it is then left untouched)."""
    path = manifest_path(document_id)
    if path is None:
        return None
    try:
        with _lock:
            data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("runs"), list) else None


def _blank(document_id: str, original_filename: str) -> Dict[str, Any]:
    return {"schema_version": MANIFEST_SCHEMA_VERSION, "document_id": document_id,
            "source_file_name": original_filename, "runs": []}


def add_run(document_id: str, run: Dict[str, Any], original_filename: str) -> Dict[str, Any]:
    """Record a new run. Its folder is chosen here, under the lock: the first run saves straight into
    ``person_images/``, later runs into ``person_images/rerun_<n>/`` so earlier images are never touched."""
    with _lock:
        ensure_dir(document_id, original_filename)
        manifest = read_manifest(document_id) or _blank(document_id, original_filename)
        n = len(manifest["runs"]) + 1
        run = {**run, "folder": "" if n == 1 else f"rerun_{n}"}
        manifest["runs"].append(run)
        manifest["schema_version"] = MANIFEST_SCHEMA_VERSION
        write_json_atomic(manifest_path(document_id), manifest)
        return run


def update_run(document_id: str, run_id: str, **fields: Any) -> Dict[str, Any]:
    with _lock:
        manifest = read_manifest(document_id)
        if manifest is None:
            raise ValueError("no manifest for this document")
        for run in manifest["runs"]:
            if run.get("run_id") == run_id:
                run.update(fields)
                break
        else:
            manifest["runs"].append({"run_id": run_id, **fields})
        write_json_atomic(manifest_path(document_id), manifest)
        return manifest


def summarize(manifest: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """latest run (its status is the status), and the newest run whose images can be shown."""
    runs: List[Dict[str, Any]] = (manifest or {}).get("runs") or []
    latest = runs[-1] if runs else None
    current = next((r for r in reversed(runs) if r.get("status") in USABLE), None)
    return {"latest": latest, "current": current, "runs": runs}


def interrupted_runs() -> List[tuple]:
    """(document_id, run_id) of runs left queued/processing by a restart: they can never finish."""
    found: List[tuple] = []
    root = output_root()
    if not root.is_dir():
        return found
    for child in root.iterdir():
        if not child.is_dir():
            continue
        document_id = _folder_document_id(child)
        manifest = read_manifest(document_id) if document_id else None
        for run in (manifest or {}).get("runs", []):
            if run.get("status") in ACTIVE and RUN_ID.match(str(run.get("run_id"))):
                found.append((document_id, run["run_id"]))
    return found

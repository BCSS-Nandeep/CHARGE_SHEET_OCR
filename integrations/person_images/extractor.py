"""Extract images of people from a PDF, DOCX or XLSX report (copied from REPORT_IMAGE_EXTRACTOR).

Every embedded image is checked for a face; only images that contain a person are
saved. All other images are skipped (never written to disk).

PROVENANCE
    Copied from  REPORT_IMAGE_EXTRACTOR/extract_images.py
    (sha256 8444F8D4BC57342EBCAF39FAF9EFA523452439EE987001DA2E5C8DD7899B27A1)
    with the YuNet model  face_detection_yunet_2023mar.onnx
    (sha256 8F2383E4DD3CFBB4553EA8718107FC0423210DC964F9F4280604804ED2552FA4), copied unchanged to ./models.

    VERBATIM (extraction and detection logic, constants, thresholds, rotation order):
        constants, extract_pdf, _extract_office, extract_docx, extract_xlsx, get_detector,
        _count_faces, ROTATIONS, detect_faces
    ADAPTED (only what IR_OCR needs; the selection rules are unchanged):
        MODEL path (this package's own copy), and save_person_images:
          * it no longer deletes an existing folder (the original ran shutil.rmtree on the output
            folder). The caller passes a per-run folder; one that already holds images is refused, and each
            file is opened with "x" so an existing file can never be overwritten;
          * files are named "<report name> image.<ext>", "<report name> image (2).<ext>", ... (the original used
            "image 2"); the caller passes the already-sanitised report name (no separators, bounded length);
          * images that cannot be decoded are counted ("unreadable") instead of being skipped silently;
          * each saved image also reports its source member name and a sha256.
    REMOVED: the command line interface (this module is only called from the integration service).
"""
import hashlib
import io
import zipfile
from pathlib import Path

import cv2
import numpy as np
import pymupdf
from PIL import Image, ImageOps

BASE = Path(__file__).parent
MODEL = BASE / "models" / "face_detection_yunet_2023mar.onnx"
MIN_IMAGE_SIZE = 40      # skip images smaller than this (px) - icons, lines
SCORE_THRESHOLD = 0.7    # face confidence needed to count as a person
ROTATED_SCORE_THRESHOLD = 0.8  # stricter when the image had to be rotated to find the face
MAX_SIDE = 1600          # larger images are downscaled before detection

DOCX_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".webp"}
SUPPORTED_EXTENSIONS = (".pdf", ".docx", ".xlsx")


# ---------- extraction (in memory): returns [(name, page, bytes)] ----------
def extract_pdf(data: bytes):
    found, seen = [], set()
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        for page_no, page in enumerate(doc, start=1):
            for idx, img in enumerate(page.get_images(full=True), start=1):
                xref = img[0]
                if xref in seen:  # same image reused on several pages
                    continue
                seen.add(xref)
                try:
                    info = doc.extract_image(xref)
                    blob, ext = info["image"], info["ext"]
                    # Convert formats viewers handle poorly (jpx, jb2, cmyk) to PNG.
                    if ext not in ("png", "jpg", "jpeg") or info.get("colorspace") == 4:
                        pix = pymupdf.Pixmap(doc, xref)
                        if pix.n - pix.alpha >= 4:
                            pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
                        blob, ext = pix.tobytes("png"), "png"
                except Exception:
                    continue
                if info["width"] < MIN_IMAGE_SIZE or info["height"] < MIN_IMAGE_SIZE:
                    continue
                found.append((f"page{page_no:03d}_img{idx:02d}.{ext}", page_no, blob))
    return found


def _extract_office(data: bytes, media_prefix: str):
    """DOCX / XLSX files are zip archives that keep their pictures under <word|xl>/media/."""
    found = []
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for member in sorted(z.namelist()):
            p = Path(member)
            if member.startswith(media_prefix) and p.suffix.lower() in DOCX_EXTS:
                found.append((p.name, None, z.read(member)))
    return found


def extract_docx(data: bytes):
    return _extract_office(data, "word/media/")


def extract_xlsx(data: bytes):
    return _extract_office(data, "xl/media/")


# ---------- person (face) detection ----------
_detector = None


def get_detector():
    global _detector
    if _detector is None:
        _detector = cv2.FaceDetectorYN.create(str(MODEL), "", (320, 320), SCORE_THRESHOLD, 0.3, 5000)
    return _detector


def _count_faces(bgr, threshold) -> int:
    h, w = bgr.shape[:2]
    scale = min(1.0, MAX_SIDE / max(h, w))
    small = cv2.resize(bgr, (int(w * scale), int(h * scale))) if scale < 1 else bgr
    det = get_detector()
    det.setScoreThreshold(threshold)
    det.setInputSize((small.shape[1], small.shape[0]))
    _, faces = det.detect(small)
    return 0 if faces is None else len(faces)


# Sideways / upside-down photos: try the other orientations when the upright one finds nothing.
ROTATIONS = [(0, None), (90, cv2.ROTATE_90_CLOCKWISE), (270, cv2.ROTATE_90_COUNTERCLOCKWISE), (180, cv2.ROTATE_180)]


def detect_faces(blob: bytes):
    """Return (width, height, number_of_faces) for one image, whatever its orientation."""
    with Image.open(io.BytesIO(blob)) as im:
        im = ImageOps.exif_transpose(im)  # honour the camera's rotation tag
        bgr = cv2.cvtColor(np.array(im.convert("RGB")), cv2.COLOR_RGB2BGR)
    h, w = bgr.shape[:2]
    if h < MIN_IMAGE_SIZE or w < MIN_IMAGE_SIZE:
        return w, h, 0
    for degrees, code in ROTATIONS:
        img = bgr if code is None else cv2.rotate(bgr, code)
        # rotated attempts need higher confidence, to avoid false faces in text/stamps
        n = _count_faces(img, SCORE_THRESHOLD if degrees == 0 else ROTATED_SCORE_THRESHOLD)
        if n:
            return w, h, n
    return w, h, 0


# ---------- main entry (adapted: never deletes, collision-safe names, counts unreadable images) ----------
def save_person_images(name: str, data: bytes, out_dir: Path, base_name: str = None) -> dict:
    """Extract images from the report bytes and save only those showing a person.

    Files are named ``<base_name> image.<ext>``, ``<base_name> image (2).<ext>``, ... (``base_name`` defaults to
    ``person``; the caller passes the uploaded file's name, already sanitised).

    Returns {"total": n_images_found, "unreadable": n_undecodable,
             "saved": [ {name, page, width, height, faces, size, source_member, sha256} ]}.
    ``out_dir`` is a per-run folder: it must not already hold image files, and nothing is ever removed or overwritten.
    The folder is only created when at least one person image is saved.
    """
    ext = Path(name).suffix.lower()
    if ext == ".pdf":
        extracted = extract_pdf(data)
    elif ext == ".docx":
        extracted = extract_docx(data)
    elif ext == ".xlsx":
        extracted = extract_xlsx(data)
    else:
        raise ValueError("Only .pdf, .docx and .xlsx files are supported.")

    if out_dir.is_dir() and any(p.suffix.lower() in DOCX_EXTS for p in out_dir.iterdir()):
        raise FileExistsError("output folder already holds images; refusing to overwrite previous results")

    saved = []
    unreadable = 0
    for img_name, page, blob in extracted:
        try:
            w, h, faces = detect_faces(blob)
        except Exception:
            unreadable += 1  # undecodable image (e.g. emf/wmf) - cannot contain a detectable person
            continue
        if faces == 0:
            continue  # not a person image - skipped
        n = len(saved) + 1
        # "<report> image.jpeg", then "<report> image (2).jpeg", "<report> image (3).jpeg", ...
        save_name = f"{base_name or 'person'} image{'' if n == 1 else f' ({n})'}{Path(img_name).suffix.lower()}"
        out_dir.mkdir(parents=True, exist_ok=True)
        target = out_dir / save_name
        with open(target, "xb") as fh:  # "x": fails instead of overwriting an existing file
            fh.write(blob)
        saved.append({"name": save_name, "page": page, "width": w, "height": h, "faces": faces, "size": len(blob),
                      "source_member": img_name, "sha256": hashlib.sha256(blob).hexdigest()})
    return {"total": len(extracted), "unreadable": unreadable, "saved": saved}

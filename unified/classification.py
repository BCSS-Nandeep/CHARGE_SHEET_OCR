"""Decide what kind of document an upload is: ``charge_sheet``, ``interrogation_report`` or ``unknown``.

Signals (none of them decides alone):

1. the file's real type, from its content (never its name or declared type);
2. the text of the first pages: native text layer, or OCR of the first pages when there is none (done by the
   charge-sheet worker's own OCR service through ``/internal/probe``);
3. the two existing ``DocumentDetector`` classes - IR_OCR's in this process, CHARGE_SHEET_OCR's inside the worker;
4. document-structure markers added here (title-region phrases and section headings of each form).

A type is chosen automatically only when its score is high and clearly ahead of the other. Anything else - no readable
text, both forms plausible, neither plausible - is ``needs_manual``: the caller must pick a type, the document is never
silently sent to an extractor.
"""

from __future__ import annotations

import hashlib
import io
import re
import zipfile
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional

from . import CHARGE_SHEET, INTERROGATION_REPORT, UNKNOWN

KIND_EXT = {"pdf": ".pdf", "docx": ".docx", "png": ".png", "jpeg": ".jpg", "tiff": ".tif"}
KIND_MEDIA = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "png": "image/png", "jpeg": "image/jpeg", "tiff": "image/tiff",
}
_KIND_EXTS = {"pdf": (".pdf",), "docx": (".docx",), "png": (".png",), "jpeg": (".jpg", ".jpeg"), "tiff": (".tif", ".tiff")}
ACCEPTED = "PDF, DOCX, PNG, JPEG or TIFF"
NATIVE_MIN_CHARS = 80


class UnsupportedFile(ValueError):
    """The upload is not one of the accepted file types (message is safe to show)."""


def sniff_kind(data: bytes) -> Optional[str]:
    """File type from content. DOCX is any ZIP that holds ``word/document.xml``."""
    if b"%PDF" in data[:1024]:
        return "pdf"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    if data.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                if "word/document.xml" in z.namelist():
                    return "docx"
        except zipfile.BadZipFile:
            return None
    return None


def check_file(data: bytes, filename: str) -> str:
    """The file's kind, or UnsupportedFile with a message that tells the user what to do."""
    if not data:
        raise UnsupportedFile("Uploaded file is empty")
    kind = sniff_kind(data)
    if kind is not None:
        return kind
    if data.startswith(b"\xd0\xcf\x11\xe0"):
        raise UnsupportedFile("Legacy Word (.doc) files are not accepted: save the document as DOCX or PDF and upload that.")
    if data.startswith(b"\x00\x05\x16\x07") or data.startswith(b"\x00\x05\x16\x00"):
        raise UnsupportedFile("This is a macOS metadata file (a '._' shadow copy), not the document. "
                              "Upload the real file without the '._' prefix.")
    raise UnsupportedFile(f"Unsupported or invalid file: accepted types are {ACCEPTED}")


def service_filename(filename: str, kind: str) -> str:
    """The name handed to an extraction service: the original name, with the real extension added when it is missing
    or wrong (the services' own validators look at the extension)."""
    name = (filename or "document").replace("\\", "/").split("/")[-1] or "document"
    return name if name.lower().endswith(_KIND_EXTS[kind]) else name + KIND_EXT[kind]


def file_facts(data: bytes, filename: str, declared_content_type: Optional[str], kind: str) -> Dict[str, Any]:
    ext = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
    return {
        "kind": kind, "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
        "declared_content_type": declared_content_type, "extension": ext,
        "extension_matches_content": ext in _KIND_EXTS[kind],
    }


# ---------------------------------------------------------------------------------------------------------- markers
_HEAD_CHARS = 1500

# "Strong" markers are phrases that define the form itself. Spaces are optional (`\s*`) because text layers of scanned
# and re-typed police PDFs often come out without them ("FinalReport/Chargesheet"). Phrases that other police forms
# share (section 173 BNSS appears on a FIR) are deliberately not defining.
_IR_STRONG = [
    ("interrogation report", r"interrogation\s*report"),
    ("cctns", r"\bcctns\b"),
    ("mo criminal database", r"\bmo\s*criminal\s*database\b"),
    ("unique identifiers", r"\bunique\s*identifiers?\b"),
]
_IR_SUPPORT = [
    ("individual particulars", r"individual\s*particulars"),
    ("modus operandi", r"modus\s*operandi"),
    ("arrest particulars", r"arrest\s*particulars"),
    ("physical features", r"ph[yv]sical\s*features|phystcal\s*features"),
    ("family history", r"family\s*history"),
    ("socio economic profile", r"socio\s*/?\s*economic\s*profile"),
    ("previous cases involved", r"previous\s*cases\s*involved"),
    ("aadhaar", r"\baadhaa?r\b"),
]
_CS_STRONG = [
    ("charge sheet", r"charge\s*-?\s*sheet"),
    ("final report", r"\bfinal\s*report\b"),
    ("section 173 crpc / 193 bnss", r"173\s*,?\s*\(?\s*cr\.?\s*p\.?\s*c|\bbnss\s*(?:section\s*)?193\b|\bsection\s*193\s*bnss\b"),
    ("challan", r"\bchallan\b"),
]
_CS_SUPPORT = [
    ("particulars of the accused", r"particulars\s*of\s*(?:the\s*)?accused"),
    ("list of witnesses", r"list\s*of\s*witnesses|particulars\s*of\s*witnesses"),
    ("property seized", r"property\s*seized"),
    ("acts and sections", r"acts?\s*and\s*sections?"),
    ("brief facts", r"brief\s*facts"),
    ("court", r"hon'?ble\s*(?:judicial\s*)?(?:court|magistrate)|\bmagistrate\b"),
    ("fir", r"\bf\.?i\.?r\.?\b"),
]
# Other police forms we know of that are neither supported form: named in the reason so the reviewer understands.
_OTHER_FORMS = [("First Information Report (FIR)", r"first\s*information\s*report")]


def other_form(text: str) -> Optional[str]:
    head = (text or "").lower()[:_HEAD_CHARS]
    for name, rx in _OTHER_FORMS:
        if re.search(rx, head):
            return name
    return None


def markers(text: str, strong, support) -> Dict[str, Any]:
    low = (text or "").lower()
    head = low[:_HEAD_CHARS]
    strong_hits = [name for name, rx in strong if re.search(rx, low)]
    head_hits = [name for name, rx in strong if re.search(rx, head)]
    support_hits = [name for name, rx in support if re.search(rx, low)]
    score = 0.0
    if head_hits:
        score += 0.45
    score += 0.12 * min(len(strong_hits), 3)
    score += 0.06 * min(len(support_hits), 5)
    return {"score": round(min(score, 0.99), 3), "strong": strong_hits, "title_region": head_hits, "support": support_hits}


# ------------------------------------------------------------------------------------------------------ native text
def native_text(data: bytes, kind: str, pages: int = 2) -> str:
    """Text of the first pages straight from the file (used when the worker's probe cannot answer)."""
    try:
        if kind == "pdf":
            import fitz

            doc = fitz.open(stream=data, filetype="pdf")
            try:
                return "\n".join((doc[i].get_text() or "") for i in range(min(pages, len(doc)))).strip()
            finally:
                doc.close()
        if kind == "docx":
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                xml = z.read("word/document.xml").decode("utf-8", "ignore")
            xml = re.sub(r"</w:p>", "\n", xml)
            return re.sub(r"<[^>]+>", "", xml)[:12000].strip()
    except Exception:  # noqa: BLE001 - classification then reports "no readable text"
        return ""
    return ""


# ----------------------------------------------------------------------------------------------------------- decide
@dataclass
class Classification:
    document_type: str
    confidence: float
    needs_manual: bool
    decision: str                      # auto | manual_required | manual_selected
    facts: Dict[str, Any]
    text: Dict[str, Any]
    signals: Dict[str, Any]
    candidates: List[Dict[str, Any]]
    reasons: List[str]
    warnings: List[str]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "document_type": self.document_type, "confidence": self.confidence, "needs_manual": self.needs_manual,
            "decision": self.decision, "file": self.facts, "text": self.text, "signals": self.signals,
            "candidates": self.candidates, "reasons": self.reasons, "warnings": self.warnings,
        }


WEAK_CAP = 0.4  # a form with no defining phrase can never reach the automatic-routing threshold


def form_score(detector_confidence: float, mk: Dict[str, Any]) -> float:
    """One form's score: detector verdict and markers, but without a defining phrase it is capped (detector hints such
    as 'accused' or 'complainant' occur in many police documents)."""
    score = max(detector_confidence, mk["score"])
    return round(score if mk["strong"] else min(score, WEAK_CAP), 3)


def decide(ir_score: float, cs_score: float, ir_strong: bool, cs_strong: bool, has_text: bool,
           min_confidence: float, margin: float, other: Optional[str] = None):
    """(document_type, needs_manual, reasons)"""
    if not has_text:
        return UNKNOWN, True, ["no readable text was found (scanned/image document and OCR was unavailable or empty)"]
    forms = {INTERROGATION_REPORT: (ir_score, ir_strong), CHARGE_SHEET: (cs_score, cs_strong)}
    best_type, other_type = sorted(forms, key=lambda t: forms[t][0], reverse=True)
    best, best_strong = forms[best_type]
    rival, rival_strong = forms[other_type]
    if best >= min_confidence and best_strong and (not rival_strong or best - rival >= margin):
        return best_type, False, [f"{best_type} score {best:.2f} (defining phrase found) is ahead of {other_type} {rival:.2f}"]
    if other and not (ir_strong or cs_strong):
        return UNKNOWN, True, [f"the document looks like a {other}, which neither extractor handles"]
    if best_strong and rival_strong and best >= min_confidence:
        return UNKNOWN, True, [f"both forms are plausible ({best_type} {best:.2f} vs {other_type} {rival:.2f}; "
                               f"a margin of {margin:.2f} is needed)"]
    return UNKNOWN, True, [f"neither form is recognised with enough confidence (best {best_type} {best:.2f}, "
                           f"needs a defining phrase and {min_confidence:.2f})"]


ProbeFn = Callable[[bytes, str], Awaitable[Optional[Dict[str, Any]]]]


async def classify(data: bytes, filename: str, declared_content_type: Optional[str], *, probe: Optional[ProbeFn],
                   min_confidence: float = 0.55, margin: float = 0.25, pages: int = 2) -> Classification:
    kind = check_file(data, filename)
    facts = file_facts(data, filename, declared_content_type, kind)
    warnings: List[str] = []
    if not facts["extension_matches_content"]:
        warnings.append(f"the file name's extension does not match its content ({kind}); the content was used")

    probed = None
    if probe is not None:
        probed = await probe(data, service_filename(filename, kind))
    if probed is not None:
        text = probed.get("text") or ""
        text_info = {"source": probed.get("text_source", "none"), "chars": probed.get("text_chars", len(text)),
                     "pages_examined": probed.get("pages_examined"), "page_count": probed.get("page_count"),
                     "ocr_requested": probed.get("ocr_requested"), "ocr_available": probed.get("ocr_available"),
                     "ocr_error": probed.get("ocr_error")}
        cs_detector = probed.get("detector")
    else:
        text = native_text(data, kind, pages)
        text_info = {"source": "native" if len(text) >= NATIVE_MIN_CHARS else "none", "chars": len(text),
                     "pages_examined": pages, "page_count": None, "ocr_requested": False, "ocr_available": None,
                     "ocr_error": None}
        cs_detector = None
        warnings.append("the charge-sheet worker was not available for the classification probe; "
                        "only the text layer and the in-process detector were used (no OCR)")
        if kind in ("png", "jpeg", "tiff"):
            text = ""

    has_text = len(text.strip()) >= 40

    from app.services.document_detector import DocumentDetector  # IR_OCR's detector, in this process

    ir_verdict = DocumentDetector().detect(text) if has_text else None
    ir_detector = None if ir_verdict is None else {
        "document_type": ir_verdict.document_type, "confidence": ir_verdict.confidence,
        "section_hits": ir_verdict.section_hits, "uncertain": ir_verdict.uncertain, "notes": ir_verdict.notes}
    ir_markers = markers(text, _IR_STRONG, _IR_SUPPORT)
    cs_markers = markers(text, _CS_STRONG, _CS_SUPPORT)

    # a detector only counts when it recognised its own form; its "unknown" carries no evidence for the other form
    ir_det_score = ir_detector["confidence"] if ir_detector and ir_detector["document_type"] == INTERROGATION_REPORT else 0.0
    cs_det_score = cs_detector["confidence"] if cs_detector and cs_detector["document_type"] == CHARGE_SHEET else 0.0
    ir_score = form_score(ir_det_score, ir_markers)
    cs_score = form_score(cs_det_score, cs_markers)

    document_type, needs_manual, reasons = decide(
        ir_score, cs_score, bool(ir_markers["strong"]), bool(cs_markers["strong"]), has_text, min_confidence, margin,
        other_form(text))
    if not needs_manual and kind in ("png", "jpeg", "tiff") and document_type == INTERROGATION_REPORT:
        # the IR_OCR service does not accept images; say so now instead of failing later
        warnings.append("IR_OCR accepts PDF and DOCX only: an interrogation report image cannot be extracted")
    candidates = sorted(
        [{"document_type": INTERROGATION_REPORT, "score": ir_score}, {"document_type": CHARGE_SHEET, "score": cs_score}],
        key=lambda c: c["score"], reverse=True)
    return Classification(
        document_type=document_type, confidence=max(ir_score, cs_score), needs_manual=needs_manual, decision="manual_required" if needs_manual else "auto", facts=facts, text=text_info,
        signals={
            INTERROGATION_REPORT: {"detector": ir_detector, "markers": ir_markers, "score": ir_score},
            CHARGE_SHEET: {"detector": cs_detector, "markers": cs_markers, "score": cs_score},
        },
        candidates=candidates, reasons=reasons, warnings=warnings,
    )

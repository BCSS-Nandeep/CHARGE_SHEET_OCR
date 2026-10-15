"""Accepted upload types and their conversion to a processing PDF.

The uploaded file is stored untouched. For images and DOCX a *derived* PDF is
written next to it (``<stored name>.pages.pdf``) and everything downstream —
text-layer routing, OCR/VLM page images, the page preview — works on that PDF.

* Images (PNG / JPEG / TIFF): one PDF page per image / TIFF frame. The EXIF
  orientation is applied, odd colour modes are converted to RGB and only
  oversized images are scaled down. Page size follows the pixel size at 200 dpi.
* DOCX: paragraphs and tables are kept in reading order and embedded pictures stay
  at their place in the flow; the result is laid out by PyMuPDF. **Page numbers
  therefore refer to this rendition, not to Word's own pagination** — treat them as
  logical sections. Headers, footers, text boxes and floating shapes are not carried over.
"""

from __future__ import annotations

import html
import io
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import fitz  # PyMuPDF

from app.config import get_settings
from app.logging_utils import get_logger

logger = get_logger(__name__)

IMAGE_DPI = 200
MAX_IMAGE_SIDE = 5000  # px; larger images are scaled down (memory), nothing else is resized


@dataclass(frozen=True)
class Kind:
    name: str
    ext: str
    media_type: str


KINDS: Dict[str, Kind] = {
    "pdf": Kind("pdf", ".pdf", "application/pdf"),
    "png": Kind("png", ".png", "image/png"),
    "jpeg": Kind("jpeg", ".jpg", "image/jpeg"),
    "tiff": Kind("tiff", ".tif", "image/tiff"),
    "docx": Kind("docx", ".docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
}
ACCEPTED_DESCRIPTION = "PDF, PNG, JPEG, TIFF or DOCX"
_EXT_TO_KIND = {".pdf": "pdf", ".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg", ".tif": "tiff", ".tiff": "tiff",
                ".docx": "docx"}


def detect_kind(data: bytes, filename: str = "") -> Optional[str]:
    """Kind of an upload from its content (the extension is only used to tell DOCX from other zips)."""
    if b"%PDF" in data[:1024]:
        return "pdf"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    if data.startswith(b"PK\x03\x04") and filename.lower().endswith(".docx"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                if "word/document.xml" in z.namelist():
                    return "docx"
        except zipfile.BadZipFile:
            return None
    return None


def kind_of_path(path: Path) -> str:
    return _EXT_TO_KIND.get(path.suffix.lower(), "pdf")


def derived_path(stored: Path) -> Path:
    return stored.with_name(stored.name + ".pages.pdf")


def prepare_source(path: Path) -> Path:
    """The PDF to process / preview for a stored upload (the upload itself when it is a PDF)."""
    kind = kind_of_path(path)
    if kind == "pdf":
        return path
    out = derived_path(path)
    if out.exists() and out.stat().st_mtime >= path.stat().st_mtime:
        return out
    tmp = out.with_name(out.name + ".tmp")
    try:
        if kind == "docx":
            docx_to_pdf(path, tmp)
        else:
            images_to_pdf(path, tmp)
        tmp.replace(out)
    finally:
        if tmp.exists():
            tmp.unlink()
    return out


def source_note(path: Path) -> Optional[str]:
    kind = kind_of_path(path)
    if kind == "docx":
        return ("DOCX_RENDITION: page numbers refer to a PDF rendition of the Word document made for "
                "processing (paragraphs and tables in reading order); they may not match Word's pages. "
                "Headers, footers and text boxes are not included.")
    if kind != "pdf":
        return "IMAGE_SOURCE: the uploaded image was converted to a one-page-per-image PDF for processing; the original is unchanged."
    return None


# ---------------------------------------------------------------- images


def images_to_pdf(src: Path, out: Path) -> int:
    from PIL import Image, ImageOps, ImageSequence

    settings = get_settings()
    pdf = fitz.open()
    try:
        with Image.open(src) as im:
            frames = ImageSequence.Iterator(im)
            for n, frame in enumerate(frames):
                if n >= settings.max_pages:
                    raise ValueError(f"image has more than {settings.max_pages} frames (MAX_PAGES)")
                f = ImageOps.exif_transpose(frame.copy())
                if f.mode not in ("RGB", "L"):
                    if f.mode in ("RGBA", "LA", "P"):
                        f = f.convert("RGBA")
                        bg = Image.new("RGB", f.size, (255, 255, 255))
                        bg.paste(f, mask=f.split()[-1])
                        f = bg
                    else:
                        f = f.convert("RGB")
                if max(f.size) > MAX_IMAGE_SIDE:
                    scale = MAX_IMAGE_SIDE / max(f.size)
                    f = f.resize((max(1, int(f.width * scale)), max(1, int(f.height * scale))), Image.LANCZOS)
                buf = io.BytesIO()
                f.save(buf, "PNG")
                w, h = f.width * 72.0 / IMAGE_DPI, f.height * 72.0 / IMAGE_DPI
                page = pdf.new_page(width=w, height=h)
                page.insert_image(page.rect, stream=buf.getvalue())
        if len(pdf) == 0:
            raise ValueError("image contains no pages")
        pdf.save(str(out))
        return len(pdf)
    finally:
        pdf.close()


# ---------------------------------------------------------------- DOCX

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_CSS = """
body { font-family: sans-serif; font-size: 10pt; }
h1 { font-size: 15pt; } h2 { font-size: 13pt; } h3 { font-size: 11.5pt; }
p { margin: 2pt 0; }
table { border-collapse: collapse; width: 100%; margin: 4pt 0; }
td { border: 0.6pt solid #444; padding: 2pt 3pt; vertical-align: top; }
"""
_PAGE_W_PT = 595 - 100
_NOWRAP_CHARS = 45  # cells with no more text than this are never wrapped


class _DocxHtml:
    def __init__(self, document) -> None:
        self.doc = document
        self.images: Dict[str, bytes] = {}
        self.warnings: List[str] = []

    def body(self) -> str:
        return "".join(self._blocks(self.doc.element.body))

    def _blocks(self, container) -> List[str]:
        out: List[str] = []
        for el in container.iterchildren():
            if el.tag == _W + "p":
                out.append(self._paragraph(el))
            elif el.tag == _W + "tbl":
                out.append(self._table(el))
        return out

    def _paragraph(self, p) -> str:
        parts: List[str] = []
        for run in p.iter(_W + "r"):
            for child in run.iterchildren():
                if child.tag == _W + "t" and child.text:
                    parts.append(html.escape(child.text))
                elif child.tag == _W + "tab":
                    parts.append("&nbsp;&nbsp;&nbsp;&nbsp;")
                elif child.tag in (_W + "br", _W + "cr"):
                    parts.append("<br/>")
            for blip in run.iter(_A + "blip"):
                img = self._image(blip.get(_R + "embed"))
                if img:
                    parts.append(img)
        inner = "".join(parts)
        if not inner.strip():
            return "<p>&nbsp;</p>" if p.find(f".//{_W}br") is not None else ""
        style = ""
        ppr = p.find(_W + "pPr")
        if ppr is not None:
            ps = ppr.find(_W + "pStyle")
            style = (ps.get(_W + "val") or "") if ps is not None else ""
        tag = {"Title": "h1", "Heading1": "h1", "Heading2": "h2", "Heading3": "h3"}.get(style, "p")
        return f"<{tag}>{inner}</{tag}>"

    def _table(self, tbl) -> str:
        # PyMuPDF's HTML engine sizes columns to their content and ignores widths, so a short
        # label can wrap by a hair ("Police / Station"); short cells are kept on one line.
        rows: List[str] = []
        for tr in tbl.iterchildren(_W + "tr"):
            cells: List[str] = []
            for tc in tr.iterchildren(_W + "tc"):
                span = 1
                tcpr = tc.find(_W + "tcPr")
                if tcpr is not None and tcpr.find(_W + "gridSpan") is not None:
                    try:
                        span = int(tcpr.find(_W + "gridSpan").get(_W + "val") or 1)
                    except ValueError:
                        span = 1
                content = "".join(self._blocks(tc)) or "&nbsp;"
                plain = html.unescape(re.sub(r"<[^>]+>", "", content))
                attrs = f' colspan="{span}"' if span > 1 else ""
                if len(plain) <= _NOWRAP_CHARS:
                    attrs += ' style="white-space:nowrap"'
                cells.append(f"<td{attrs}>{content}</td>")
            rows.append("<tr>" + "".join(cells) + "</tr>")
        return "<table>" + "".join(rows) + "</table>"

    def _image(self, rid: Optional[str]) -> str:
        if not rid:
            return ""
        from PIL import Image

        try:
            part = self.doc.part.related_parts[rid]
            with Image.open(io.BytesIO(part.blob)) as im:
                im.load()
                w, h = im.size
                buf = io.BytesIO()
                im.convert("RGB").save(buf, "PNG")
        except Exception:
            self.warnings.append("an embedded picture in a format that cannot be read was skipped")
            return ""
        name = f"img{len(self.images) + 1}.png"
        self.images[name] = buf.getvalue()
        width = min(_PAGE_W_PT, w * 0.75)  # px → pt, capped to the text width
        return f'<img src="{name}" width="{width:.0f}" height="{width * h / w:.0f}"/>'


def docx_to_pdf(src: Path, out: Path) -> int:
    from docx import Document

    settings = get_settings()
    try:
        document = Document(str(src))
    except Exception as exc:
        raise ValueError(f"Unable to open DOCX: {type(exc).__name__}") from None
    conv = _DocxHtml(document)
    body = conv.body()
    archive = fitz.Archive()
    for name, blob in conv.images.items():
        archive.add(blob, name)
    story = fitz.Story(html=f"<html><body>{body or '<p>&nbsp;</p>'}</body></html>", user_css=_CSS, archive=archive)
    writer = fitz.DocumentWriter(str(out))
    mediabox = fitz.paper_rect("a4")
    where = mediabox + (50, 50, -50, -50)
    pages, more = 0, True
    try:
        while more:
            if pages >= settings.max_pages:
                raise ValueError(f"DOCX renders to more than {settings.max_pages} pages (MAX_PAGES)")
            dev = writer.begin_page(mediabox)
            more, _ = story.place(where)
            story.draw(dev)
            writer.end_page()
            pages += 1
    finally:
        writer.close()
    return pages

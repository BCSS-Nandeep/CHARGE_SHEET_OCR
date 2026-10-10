"""Staged local extraction pipeline (hybrid stack).

Gate0/1 PyMuPDF router → native and/or PaddleOCR(+PP-Structure) → Docling
assist on digital forms → Tesseract fallback → optional local VL → layout
items → field list (labels and values as printed in this PDF, reading order).

There is no fixed field list: what the result holds is decided by the
uploaded PDF. Every stage records a StageResult; ``extraction_status`` is
``failed`` when no usable text was obtained and ``partial`` when pages or
stages failed. Nothing is invented.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.config import get_settings
from app.logging_utils import audit, get_logger, safe_exc_message
from app.schemas.charge_sheet import (
    ChargeSheetData,
    DocumentTypeInfo,
    ExtractionStatus,
    PageSummary,
    StageResult,
    StageStatus,
    UnresolvedContentReport,
    UnresolvedItem,
)
from app.schemas.raw import RawDocument, RawLine, RawPage
from app.services.docling_service import DoclingService
from app.services.document_detector import DocumentDetector
from app.services.field_builder import build_fields
from app.services.form_parser import DocItem, drop_page_furniture
from app.services.layout_service import (
    Box,
    PageLayout,
    boxes_from_ocr,
    header_pairs_from_boxes,
    layout_from_boxes,
    page_skew,
    reading_order,
    rows_from_pdf_table,
)
from app.services.ocr_service import OcrResult, OcrService
from app.services.page_router import count_label_hits, need_docling_assist, ocr_accept
from app.services.pdf_service import PageContent, PdfExtractionResult, PdfService
from app.services.structure_service import StructureService
from app.services.template_service import TemplateService
from app.services.text_normalize import soften_pdf_text
from app.services.vl_service import VlService

logger = get_logger(__name__)

MIN_DOCUMENT_CHARS = 40


class ExtractionService:
    def __init__(self, db: Optional[Session] = None):
        self.settings = get_settings()
        self.pdf = PdfService()
        self.ocr = OcrService()
        self.structure = StructureService()
        self.docling = DoclingService()
        self.vl = VlService()
        self.detector = DocumentDetector()
        self.db = db
        self.last_page_texts: Dict[int, str] = {}
        self.last_ocr_text: Dict[int, str] = {}  # raw OCR lines per page (evaluation / optional persist)
        self.last_native_text: Dict[int, str] = {}
        self._progress: Optional[Callable[[str], None]] = None
        self._structure_tables: Dict[int, List[List[str]]] = {}
        self._ocr_results: Dict[int, OcrResult] = {}
        self.last_raw: RawDocument = RawDocument()  # every line as read (stored separately from fields)

    # ------------------------------------------------------------------
    # Stage helpers
    # ------------------------------------------------------------------

    def _run_page_pipeline(
        self, path: Path, pdf_result: PdfExtractionResult, warnings: List[str], started: float
    ) -> Tuple[Dict[int, PageLayout], List[PageSummary], StageResult]:
        """Gates 1–4: acquire text/layout per page via the hybrid stack."""
        layouts: Dict[int, PageLayout] = {}
        summaries: List[PageSummary] = []
        failed: List[str] = []
        needed = [p for p in pdf_result.pages if p.route in ("ocr", "both")]
        budget = self.settings.processing_timeout_seconds
        store_text = self.settings.store_page_text
        docling_pages = structure_pages = vl_pages = 0

        for page in pdf_result.pages:
            self.last_native_text[page.page_number] = page.native_text or page.text
            summary = PageSummary(
                page_number=page.page_number,
                route=page.route,
                route_reasons=list(page.route_reasons),
                text_source="native",
                page_status="native_ok",
                char_count=page.char_count,
                native_text=(page.native_text if store_text else None),
            )
            if page.route == "native":
                if need_docling_assist(
                    page.signals, page.route, enabled=self.settings.docling_enabled
                ) if page.signals else False:
                    self._report(f"Docling assist PDF page {page.page_number}")
                    dl = self.docling.analyze_pdf_page(path, page.page_number)
                    summary.docling_used = bool(dl.markdown and not dl.error)
                    if summary.docling_used:
                        docling_pages += 1
                        # Prefer Docling markdown when native tables look weak; keep native as primary text
                        note = "Docling structured assist applied"
                        summary.note = note
                    elif dl.error:
                        summary.note = f"Docling skipped: {dl.error}"
                summaries.append(summary)
                continue

            # route ocr | both
            reason: Optional[str] = None
            if not self.settings.ocr_enabled:
                reason = "OCR disabled (OCR_ENABLED=false)"
            elif not self.ocr.is_available():
                reason = "no local OCR engine available (install PaddleOCR or Tesseract)"
            elif budget and time.monotonic() - started > budget:
                reason = f"processing time budget of {budget}s exceeded before OCR"

            if reason is not None:
                if page.route == "both" and (page.native_text or "").strip():
                    summary.text_source = "native"
                    summary.page_status = "partial"
                    summary.note = f"OCR skipped ({reason}); using native text"
                    summaries.append(summary)
                else:
                    failed.append(f"page {page.page_number}: {reason}")
                    summary.text_source = "none"
                    summary.page_status = "failed"
                    summary.note = reason
                    summaries.append(summary)
                continue

            try:
                done = sum(1 for s in summaries if s.route in ("ocr", "both"))
                self._report(f"OCR page {done + 1} of {len(needed)} (PDF page {page.page_number})")
                png = self.pdf.render_page_image(path, page.page_number)
                res = self.ocr.recognize_png(png)
                self._ocr_results[page.page_number] = res
                ocr_text = "\n".join(l.text for l in res.lines) if res.lines else ""
                mean_conf = res.mean_confidence if res.lines else 0.0
                low_ratio = (
                    sum(1 for l in res.lines if l.confidence < 0.5) / len(res.lines) if res.lines else 1.0
                )
                label_hits = count_label_hits(ocr_text)
                accept, gate3_reasons = ocr_accept(
                    ocr_char_count=len(ocr_text.strip()),
                    mean_confidence=mean_conf,
                    low_conf_box_ratio=low_ratio,
                    ocr_label_hits=label_hits,
                    min_chars=max(self.settings.ocr_min_text_chars, 80),
                    min_confidence=self.settings.ocr_accept_min_confidence,
                )

                # Gate 3 fallback: Tesseract when Paddle empty/failed (OcrService already falls back),
                # or re-try signal via structure + VL later.
                if res.error and not res.lines:
                    reason = res.error
                elif not accept and not ocr_text.strip():
                    reason = "OCR produced insufficient text: " + "; ".join(gate3_reasons)
                else:
                    # PP-Structure assist on OCR / both routes
                    struct = self.structure.analyze_png(png)
                    if struct.tables and not struct.error:
                        structure_pages += 1
                        summary.structure_engine = struct.engine
                        self._structure_tables[page.page_number] = [
                            row for t in struct.tables for row in t.rows
                        ]
                    elif struct.error and self.settings.ppstructure_enabled:
                        summary.note = (summary.note + "; " if summary.note else "") + f"PP-Structure: {struct.error}"

                    boxes, skew = boxes_from_ocr(res.lines, res.image_width, res.image_height)
                    self.last_ocr_text[page.page_number] = ocr_text
                    layout = layout_from_boxes(boxes, page.page_number)
                    layouts[page.page_number] = layout

                    if page.route == "both" and (page.native_text or "").strip():
                        # Keep both; prefer OCR layout for geometry, merge text for mapping
                        merged = soften_pdf_text(page.native_text) + "\n\n" + soften_pdf_text(layout.text)
                        page.text = merged
                        summary.text_source = "both"
                        summary.page_status = "both_ok" if accept else "partial"
                    else:
                        page.text = layout.text
                        summary.text_source = "ocr" if layout.text.strip() else "none"
                        summary.page_status = "ocr_ok" if (accept and layout.text.strip()) else (
                            "partial" if layout.text.strip() else "failed"
                        )
                    page.char_count = len(page.text)
                    summary.char_count = page.char_count
                    summary.ocr_engine = res.engine
                    summary.ocr_confidence = round(mean_conf, 3) if res.lines else None
                    if store_text:
                        summary.ocr_text = ocr_text
                    notes = []
                    if skew:
                        notes.append(f"page rotated {skew:+.1f}°, straightened before layout analysis")
                    if gate3_reasons and not accept:
                        notes.append("OCR quality soft-fail: " + "; ".join(gate3_reasons))
                    if not layout.text.strip():
                        notes.append("OCR found no text (blank page?)")
                    summary.note = "; ".join(notes) or summary.note

                    if summary.page_status == "failed":
                        failed.append(f"page {page.page_number}: OCR empty")
                    summaries.append(summary)
                    continue
            except Exception as exc:
                reason = f"OCR error: {safe_exc_message(exc)}"

            if page.route == "both" and (page.native_text or "").strip():
                summary.text_source = "native"
                summary.page_status = "partial"
                summary.note = f"OCR failed ({reason}); using native text"
                summaries.append(summary)
            else:
                failed.append(f"page {page.page_number}: {reason}")
                summary.text_source = "none"
                summary.page_status = "failed"
                summary.note = reason
                summaries.append(summary)

        detail_bits = []
        if needed:
            detail_bits.append(f"{len(needed)} page(s) routed to OCR")
        if structure_pages:
            detail_bits.append(f"PP-Structure on {structure_pages}")
        if docling_pages:
            detail_bits.append(f"Docling on {docling_pages}")
        if vl_pages:
            detail_bits.append(f"VL on {vl_pages}")
        if not needed:
            return layouts, summaries, StageResult(
                stage="hybrid_page_acquire",
                status=StageStatus.SKIPPED,
                detail="all pages used native text only",
            )
        if failed:
            warnings.append(
                "OCR_REQUIRED: " + "; ".join(failed[:5])
                + ("" if len(failed) <= 5 else f" (+{len(failed) - 5} more)")
                + ". No fields were invented for these pages."
            )
            status = StageStatus.FAILED if len(failed) == len(needed) else StageStatus.DEGRADED
            return layouts, summaries, StageResult(
                stage="hybrid_page_acquire",
                status=status,
                detail=f"{len(failed)}/{len(needed)} OCR page(s) failed; " + ", ".join(detail_bits),
            )
        return layouts, summaries, StageResult(
            stage="hybrid_page_acquire",
            status=StageStatus.OK,
            detail=", ".join(detail_bits) or f"OCR via {self.ocr.engine_name()}",
        )

    def _maybe_vl_escalate(
        self,
        path: Path,
        pdf_result: PdfExtractionResult,
        data: ChargeSheetData,
        page_summaries: List[PageSummary],
        unresolved: UnresolvedContentReport,
    ) -> None:
        """Gate 4: local VL on hard pages when GPU/engine available."""
        if not self.settings.vl_enabled or not unresolved.needs_review:
            return
        if not self.vl.is_available():
            unresolved.items.append(
                UnresolvedItem(
                    code="vl_unavailable",
                    detail="Hard-page flags set but local PaddleOCR-VL/GPU not available; marked needs_review",
                    severity="review",
                )
            )
            return
        hard_pages = {
            it.page for it in unresolved.items if it.page and it.severity == "critical"
        }
        if not hard_pages:
            hard_pages = {s.page_number for s in page_summaries if s.page_status in ("partial", "failed")}
        for page_no in sorted(hard_pages)[:5]:
            self._report(f"VL escalate PDF page {page_no}")
            try:
                png = self.pdf.render_page_image(path, page_no)
                vr = self.vl.analyze_png(png)
                for s in page_summaries:
                    if s.page_number == page_no:
                        s.vl_used = bool(vr.text and not vr.error)
                        if s.vl_used:
                            s.note = ((s.note + "; ") if s.note else "") + "VL assist applied"
                            # Append VL text for downstream re-parse is too heavy; keep as unresolved evidence
                            unresolved.items.append(
                                UnresolvedItem(
                                    code="vl_assist",
                                    page=page_no,
                                    detail=f"VL extracted {len(vr.text or '')} chars (review recommended)",
                                    severity="review",
                                )
                            )
                        elif vr.error:
                            unresolved.items.append(
                                UnresolvedItem(code="vl_error", page=page_no, detail=vr.error, severity="review")
                            )
                        break
            except Exception as exc:
                unresolved.items.append(
                    UnresolvedItem(
                        code="vl_error",
                        page=page_no,
                        detail=safe_exc_message(exc),
                        severity="review",
                    )
                )

    @staticmethod
    def _page_items(page: PageContent, layout: Optional[PageLayout]) -> List[DocItem]:
        items: List[DocItem] = []
        if layout is not None:
            for row in layout.rows:
                items.append(DocItem(page.page_number, row.bbox[1] if row.bbox else 0.0, " ".join(row.cells), row=row))
            for hp in layout.header_pairs:
                items.append(DocItem(page.page_number, layout.header_y, f"{hp.label}: {hp.value}", header=hp))
            for y, text in layout.free_lines:
                items.append(DocItem(page.page_number, y, text))
        else:
            for table in page.tables:
                for row, top in rows_from_pdf_table(page.page_number, table.rows, table.row_tops):
                    items.append(DocItem(page.page_number, top, " ".join(row.cells), row=row))
            free = [ln for ln in page.lines if not ln.in_table]
            boxes = [Box(ln.text, *ln.bbox) for ln in free]
            pairs, consumed = header_pairs_from_boxes(boxes, page.page_number)
            consumed_text = {b.text for b in boxes if id(b) in consumed}
            for hp in pairs:
                y = min((b.y0 for b in boxes if id(b) in consumed), default=0.0)
                items.append(DocItem(page.page_number, y, f"{hp.label}: {hp.value}", header=hp))
            for ln, box in zip(free, boxes):
                if id(box) in consumed or (pairs and ln.text in consumed_text):
                    continue
                items.append(DocItem(page.page_number, ln.bbox[1], ln.text))
        items.sort(key=lambda it: it.y)
        return items

    # ------------------------------------------------------------------

    def _report(self, step: str) -> None:
        """Pass a progress step (never document content) to the caller's callback."""
        if self._progress is not None:
            try:
                self._progress(step)
            except Exception:
                pass

    def process(self, path: Path, progress: Optional[Callable[[str], None]] = None) -> ChargeSheetData:
        started = time.monotonic()
        self.last_ocr_text = {}
        self._structure_tables = {}
        self._ocr_results = {}
        self.last_raw = RawDocument()
        self._progress = progress
        audit("extraction_start")
        self._report("Reading the PDF text layer")
        stage_results: List[StageResult] = []
        warnings: List[str] = []

        pdf_result = self.pdf.extract(path)  # unreadable/encrypted PDFs raise → document FAILED
        warnings.extend(pdf_result.warnings)
        native_pages = sum(1 for p in pdf_result.pages if p.route == "native")
        both_pages = sum(1 for p in pdf_result.pages if p.route == "both")
        ocr_pages = sum(1 for p in pdf_result.pages if p.route == "ocr")
        stage_results.append(
            StageResult(
                stage="pdf_text_layout",
                detail=(
                    f"{pdf_result.page_count} page(s); route native={native_pages} "
                    f"ocr={ocr_pages} both={both_pages}"
                ),
            )
        )

        layouts, page_summaries, acquire_stage = self._run_page_pipeline(path, pdf_result, warnings, started)
        stage_results.append(acquire_stage)
        self.last_raw = self._raw_document(pdf_result, page_summaries)
        self.last_page_texts = {p.page_number: p.text for p in pdf_result.pages}

        def _page_usable(p: PageContent) -> bool:
            if not (p.text or "").strip():
                return False
            if p.route in ("native", "both"):
                return True  # "both": native text retained even if OCR failed
            return p.page_number in layouts

        usable = sum(len(p.text.strip()) for p in pdf_result.pages if _page_usable(p))
        if usable < MIN_DOCUMENT_CHARS:
            return self._failed_result(pdf_result, page_summaries, stage_results, warnings)

        self._report("Detecting labels and values")
        items_by_page = [self._page_items(p, layouts.get(p.page_number)) for p in pdf_result.pages if _page_usable(p)]
        items_by_page, furniture = drop_page_furniture(items_by_page)

        data = ChargeSheetData(page_summaries=page_summaries)
        # OCR confidence per page (None for text-layer pages) — reported, never used as accuracy
        page_conf = {s.page_number: s.ocr_confidence if s.text_source == "ocr" else None for s in page_summaries}
        try:
            data.fields = build_fields(items_by_page, page_conf)
            labelled = sum(1 for f in data.fields if f.label)
            stage_results.append(
                StageResult(
                    stage="field_detection",
                    detail=f"{len(data.fields)} field(s), {labelled} with a label"
                    + (f"; {furniture} watermark / repeated header-footer line(s) ignored" if furniture else ""),
                )
            )
        except Exception as exc:
            logger.warning("stage_failed stage=field_detection err=%s", safe_exc_message(exc))
            stage_results.append(StageResult(stage="field_detection", status=StageStatus.FAILED, detail=safe_exc_message(exc)))

        full_text = "\n\n".join(soften_pdf_text(p.text) for p in pdf_result.pages if p.text)
        detection = self.detector.detect(full_text, [f.label for f in data.fields if f.label])
        data.document_info = DocumentTypeInfo(
            document_type=detection.document_type,
            classification_confidence=detection.confidence,
            classification_notes=detection.notes,
            template_fingerprint=detection.fingerprint,
        )
        if detection.uncertain:
            warnings.append("Document could not be classified confidently")
        self._remember_template(data, detection.fingerprint, detection.document_type, warnings)

        unresolved = self._build_unresolved(page_summaries)
        self._maybe_vl_escalate(path, pdf_result, data, page_summaries, unresolved)
        data.unresolved_content = unresolved
        stage_results.append(
            StageResult(stage="unresolved_content", detail=f"{len(unresolved.items)} item(s); needs_review={unresolved.needs_review}")
        )

        failed_stages = [s for s in stage_results if s.status in (StageStatus.FAILED, StageStatus.DEGRADED)]
        bad_pages = [s for s in page_summaries if s.page_status in ("failed", "partial")]
        inventory_ok = len(page_summaries) == pdf_result.page_count
        if not inventory_ok:
            warnings.append(f"Page inventory mismatch: summaries={len(page_summaries)} pdf_pages={pdf_result.page_count}")
        complete = inventory_ok and not bad_pages and not failed_stages and not unresolved.needs_review
        data.extraction_status = ExtractionStatus.COMPLETE if complete else ExtractionStatus.PARTIAL
        data.extraction_warnings = warnings
        ocr_confs = [s.ocr_confidence for s in page_summaries if s.ocr_confidence is not None]
        data.overall_confidence = round(sum(ocr_confs) / len(ocr_confs), 3) if ocr_confs else None
        data.stage_results = stage_results
        data.pipeline_stages = [s.stage for s in stage_results]
        data.requires_human_review = True

        audit(
            "extraction_complete",
            pages=pdf_result.page_count,
            is_scanned=pdf_result.is_scanned,
            status=data.extraction_status.value,
            fields=len(data.fields),
            unresolved=len(unresolved.items),
        )
        return data

    def _raw_document(self, pdf_result: PdfExtractionResult, page_summaries: List[PageSummary]) -> RawDocument:
        """Every text line read from each page, as read: text layer and/or OCR, nothing dropped."""
        summaries = {s.page_number: s for s in page_summaries}
        pages: List[RawPage] = []
        for page in pdf_result.pages:
            summary = summaries.get(page.page_number)
            lines: List[RawLine] = []
            if page.route in ("native", "both") and page.lines:
                native = [ln for ln in page.lines if ln.text.strip()]
                for i in reading_order([tuple(ln.bbox) for ln in native]):
                    lines.append(RawLine(line=0, text=native[i].text, source="native"))
            res = self._ocr_results.get(page.page_number)
            if res is not None and res.lines:
                ocr = [ln for ln in res.lines if ln.text.strip()]
                skew = page_skew([getattr(ln, "poly", None) for ln in ocr])
                for i in reading_order([tuple(ln.bbox) for ln in ocr], skew):
                    lines.append(RawLine(line=0, text=ocr[i].text, source="ocr", confidence=round(ocr[i].confidence, 3)))
            for n, ln in enumerate(lines, start=1):
                ln.line = n
            pages.append(
                RawPage(
                    page=page.page_number,
                    text_source=summary.text_source if summary else ("native" if lines else "none"),
                    ocr_engine=res.engine if res is not None else None,
                    ocr_confidence=round(res.mean_confidence, 3) if res is not None and res.lines else None,
                    lines=lines,
                    note=None if lines else (summary.note if summary else "no text found on this page"),
                )
            )
        return RawDocument(page_count=pdf_result.page_count, pages=pages)

    @staticmethod
    def _build_unresolved(page_summaries: List[PageSummary]) -> UnresolvedContentReport:
        """Pages that could not be read (fully or partly). Missing *fields* are not judged:
        there is no fixed field list."""
        items: List[UnresolvedItem] = []
        for s in page_summaries:
            if s.page_status == "failed":
                items.append(UnresolvedItem(code="page_failed", page=s.page_number,
                                            detail=s.note or "page produced no usable text", severity="critical"))
            elif s.page_status == "partial":
                items.append(UnresolvedItem(code="page_partial", page=s.page_number,
                                            detail=s.note or "partial page acquisition", severity="review"))
        return UnresolvedContentReport(items=items, needs_review=any(i.severity == "critical" for i in items))

    def _failed_result(
        self,
        pdf_result: PdfExtractionResult,
        page_summaries: List[PageSummary],
        stage_results: List[StageResult],
        warnings: List[str],
    ) -> ChargeSheetData:
        ocr_ran_empty = page_summaries and all(p.ocr_engine and p.text_source in ("none", "ocr") for p in page_summaries)
        if ocr_ran_empty:
            reason = "local OCR ran but found no readable text (blank or unreadable scan)"
            advice = "check the scan quality / page orientation and re-upload"
        else:
            reason = "pages have no text layer and OCR could not process them"
            advice = "install or fix the local OCR engine (see /health) and reprocess"
        warnings.append(
            f"EXTRACTION_FAILED: no usable text could be obtained from this document "
            f"({pdf_result.page_count} page(s)): {reason}. Nothing was extracted; {advice}."
        )
        stage_results.append(StageResult(stage="field_detection", status=StageStatus.SKIPPED, detail="no text"))
        audit("extraction_failed", pages=pdf_result.page_count, reason="no_text")
        return ChargeSheetData(
            document_info=DocumentTypeInfo(document_type="unknown", classification_confidence=0.0,
                                           classification_notes="no text available"),
            extraction_status=ExtractionStatus.FAILED,
            extraction_warnings=warnings,
            stage_results=stage_results,
            page_summaries=page_summaries,
            pipeline_stages=[s.stage for s in stage_results],
        )

    def _remember_template(self, data: ChargeSheetData, fingerprint: str, label: str, warnings: List[str]) -> None:
        if self.db is None:
            return
        try:
            svc = TemplateService(self.db)
            match = svc.match(fingerprint)
            row = svc.remember(fingerprint, label=label)
            data.document_info.template_id = row.id
            data.document_info.previously_seen = match is not None
        except Exception as exc:
            warnings.append(f"Template store error: {safe_exc_message(exc)}")

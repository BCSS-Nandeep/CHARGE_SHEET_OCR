"""Staged local extraction pipeline.

PDF text/layout → OCR (local, per page) → layout/form parse → field mapping
→ entities → legal provisions → conflicts → validation.

Every stage records a StageResult; the overall ``extraction_status`` is
``failed`` when no usable text was obtained and ``partial`` when pages or
stages failed. A document with failed extraction is never reported as a
complete extraction.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.config import get_settings
from app.logging_utils import audit, get_logger, safe_exc_message
from app.schemas.charge_sheet import (
    AccusedPerson,
    CaseIdentifiers,
    ChargeSheetData,
    DocumentTypeInfo,
    ExtractedField,
    ExtractionStatus,
    LabResult,
    LegalSection,
    PageSummary,
    PersonDetails,
    ReviewStatus,
    SeizedItem,
    SourceConflict,
    StageResult,
    StageStatus,
    ValidationStatus,
    Witness,
)
from app.services.conflict_service import apply_conflicts_to_fields, detect_person_conflicts
from app.services.document_detector import DocumentDetector
from app.services.entity_extractor import (
    PersonEntity,
    extract_accused,
    extract_witnesses,
    find_person_mentions,
    parse_officers,
    parse_person,
)
from app.services.field_mapping_service import best_canonical_match
from app.services.form_parser import (
    DocItem,
    FormParseResult,
    SelectedField,
    parse_document,
    select_fields,
)
from app.services.layout_service import Box, PageLayout, header_pairs_from_boxes, layout_from_boxes, rows_from_pdf_table
from app.services.legal_section_parser import LegalEntry, Provision, consolidate, parse_provisions
from app.services.ocr_service import OcrService
from app.services.pdf_service import PageContent, PdfExtractionResult, PdfService
from app.services.template_service import TemplateService
from app.services.text_normalize import clean_value, sentence_bounds, soften_pdf_text
from app.services.validation_service import ValidationService, normalize_simple

logger = get_logger(__name__)

_LAB_MENTION = re.compile(r"(?i)\b(?:S?FSL|forensic|chemical\s+examin\w*|laboratory)\b")
_LAB_FINDING = re.compile(r"(?i)\bopin\w*|ascertain\w*|\bfound\s+to\s+be|\bdetected|\banaly[sz]ed\s+as")

CASE_KEYS = [
    "district",
    "police_station",
    "year",
    "fir_number",
    "crime_number",
    "fir_date",
    "charge_sheet_number",
    "submission_date",
    "court_name",
    "jurisdiction",
    "final_report_type",
    "supplementary_or_original",
]
MIN_DOCUMENT_CHARS = 40
NATIVE_CONFIDENCE = 0.85  # extraction confidence for text taken from a PDF text layer


def _field(
    canonical: str,
    *,
    value: Optional[str],
    label: Optional[str] = None,
    page: Optional[int] = None,
    source_text: Optional[str] = None,
    extraction_confidence: float = 0.7,
    mapping_confidence: float = 0.7,
    alternate_values: Optional[List[str]] = None,
    notes: Optional[str] = None,
    location: Optional[str] = None,
) -> ExtractedField:
    return ExtractedField(
        canonical_name=canonical,
        original_label=label,
        original_value=value,
        normalized_value=normalize_simple(value),
        source_page=page,
        source_text=(source_text or "")[:400] or None,
        source_location=location,
        extraction_confidence=round(extraction_confidence, 3),
        mapping_confidence=round(min(max(mapping_confidence, 0.0), 1.0), 3),
        validation_status=ValidationStatus.PENDING,
        review_status=ReviewStatus.UNREVIEWED,
        alternate_values=list(alternate_values or []),
        notes=notes,
    )


class ExtractionService:
    def __init__(self, db: Optional[Session] = None):
        self.settings = get_settings()
        self.pdf = PdfService()
        self.ocr = OcrService()
        self.detector = DocumentDetector()
        self.validator = ValidationService()
        self.db = db
        self._page_conf: Dict[int, float] = {}
        self.last_page_texts: Dict[int, str] = {}
        self.last_ocr_text: Dict[int, str] = {}  # raw OCR lines per page (evaluation only, never persisted)

    def _ext_conf(self, page: Optional[int]) -> float:
        return self._page_conf.get(page or 0, NATIVE_CONFIDENCE)

    # ------------------------------------------------------------------
    # Stage helpers
    # ------------------------------------------------------------------

    def _run_ocr(
        self, path: Path, pdf_result: PdfExtractionResult, warnings: List[str], started: float
    ) -> Tuple[Dict[int, PageLayout], List[PageSummary], StageResult]:
        layouts: Dict[int, PageLayout] = {}
        summaries: List[PageSummary] = []
        needed = [p for p in pdf_result.pages if p.needs_ocr]
        failed: List[str] = []
        budget = self.settings.processing_timeout_seconds
        for page in pdf_result.pages:
            if not page.needs_ocr:
                summaries.append(PageSummary(page_number=page.page_number, text_source="native", char_count=page.char_count))
                continue
            reason: Optional[str] = None
            if not self.settings.ocr_enabled:
                reason = "OCR disabled (OCR_ENABLED=false)"
            elif not self.ocr.is_available():
                reason = "no local OCR engine available (install PaddleOCR or Tesseract)"
            elif budget and time.monotonic() - started > budget:
                reason = f"processing time budget of {budget}s exceeded before OCR"
            if reason is None:
                try:
                    png = self.pdf.render_page_image(path, page.page_number)
                    res = self.ocr.recognize_png(png)
                    if res.error:
                        reason = res.error
                    else:
                        boxes = [Box(l.text, *l.bbox, conf=l.confidence) for l in res.lines]
                        self.last_ocr_text[page.page_number] = "\n".join(l.text for l in res.lines)
                        layout = layout_from_boxes(boxes, page.page_number)
                        layouts[page.page_number] = layout
                        page.text = layout.text
                        page.char_count = len(layout.text)
                        summaries.append(
                            PageSummary(
                                page_number=page.page_number,
                                text_source="ocr" if layout.text.strip() else "none",
                                char_count=page.char_count,
                                ocr_engine=res.engine,
                                ocr_confidence=round(res.mean_confidence, 3) if res.lines else None,
                                note=None if layout.text.strip() else "OCR found no text (blank page?)",
                            )
                        )
                        continue
                except Exception as exc:
                    reason = f"OCR error: {safe_exc_message(exc)}"
            failed.append(f"page {page.page_number}: {reason}")
            summaries.append(
                PageSummary(page_number=page.page_number, text_source="none", char_count=page.char_count, note=reason)
            )
        if not needed:
            return layouts, summaries, StageResult(stage="ocr_local", status=StageStatus.SKIPPED, detail="all pages have a text layer")
        if failed:
            warnings.append(
                "OCR_REQUIRED: " + "; ".join(failed[:5]) + ("" if len(failed) <= 5 else f" (+{len(failed) - 5} more)")
                + ". No fields were invented for these pages."
            )
            status = StageStatus.FAILED if len(failed) == len(needed) else StageStatus.DEGRADED
            return layouts, summaries, StageResult(stage="ocr_local", status=status, detail=f"{len(failed)}/{len(needed)} page(s) could not be OCR'd")
        return layouts, summaries, StageResult(
            stage="ocr_local", status=StageStatus.OK, detail=f"{len(needed)} page(s) OCR'd with {self.ocr.engine_name()}"
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

    def process(self, path: Path) -> ChargeSheetData:
        started = time.monotonic()
        self.last_ocr_text = {}
        audit("extraction_start")
        stage_results: List[StageResult] = []
        warnings: List[str] = []

        pdf_result = self.pdf.extract(path)  # unreadable/encrypted PDFs raise → document FAILED
        warnings.extend(pdf_result.warnings)
        native_pages = sum(1 for p in pdf_result.pages if not p.needs_ocr)
        stage_results.append(
            StageResult(stage="pdf_text_layout", detail=f"{pdf_result.page_count} page(s); {native_pages} with a text layer")
        )

        layouts, page_summaries, ocr_stage = self._run_ocr(path, pdf_result, warnings, started)
        stage_results.append(ocr_stage)
        # In-memory only (evaluation / debugging); page text is never persisted or logged
        self.last_page_texts = {p.page_number: p.text for p in pdf_result.pages}
        # Extraction confidence per page: native text vs. measured OCR confidence
        self._page_conf = {
            s.page_number: (NATIVE_CONFIDENCE if s.text_source == "native" else round(NATIVE_CONFIDENCE * (s.ocr_confidence or 0.0), 3))
            for s in page_summaries
        }

        usable = sum(len(p.text.strip()) for p in pdf_result.pages if p.text and (not p.needs_ocr or p.page_number in layouts))
        if usable < MIN_DOCUMENT_CHARS:
            return self._failed_result(pdf_result, page_summaries, stage_results, warnings)

        items_by_page = [self._page_items(p, layouts.get(p.page_number)) for p in pdf_result.pages if not p.needs_ocr or p.page_number in layouts]
        full_text = "\n\n".join(soften_pdf_text(p.text) for p in pdf_result.pages if p.text)

        parsed = FormParseResult()
        try:
            parsed = parse_document(items_by_page)
            stage_results.append(
                StageResult(
                    stage="form_layout_parse",
                    detail=f"{len(parsed.candidates)} field candidates; narrative from page {parsed.narrative_start_page or '—'}",
                )
            )
        except Exception as exc:
            stage_results.append(StageResult(stage="form_layout_parse", status=StageStatus.FAILED, detail=safe_exc_message(exc)))

        data = ChargeSheetData()
        conflicts: List[SourceConflict] = []
        selected = select_fields(parsed.candidates)

        def run(stage: str, fn: Callable[[], Optional[str]]) -> None:
            try:
                detail = fn()
                stage_results.append(StageResult(stage=stage, detail=detail))
            except Exception as exc:
                logger.warning("stage_failed stage=%s err=%s", stage, safe_exc_message(exc))
                stage_results.append(StageResult(stage=stage, status=StageStatus.FAILED, detail=safe_exc_message(exc)))

        run("semantic_field_mapping", lambda: self._map_case_fields(data, parsed, selected, conflicts))
        run("entity_extraction", lambda: self._map_entities(data, parsed, selected, conflicts))
        run("legal_section_parse", lambda: self._map_legal(data, parsed, conflicts))
        run("narrative_and_evidence", lambda: self._map_narrative(data, parsed, selected))

        detection = self.detector.detect(full_text, [c.label for c in parsed.candidates if c.label])
        data.document_info = DocumentTypeInfo(
            document_type=_field(
                "document_type",
                value=detection.document_type,
                extraction_confidence=detection.confidence,
                mapping_confidence=detection.confidence,
            ),
            classification_confidence=detection.confidence,
            classification_notes=detection.notes,
            template_fingerprint=detection.fingerprint,
        )
        if detection.uncertain:
            warnings.append("Document could not be classified confidently")
        self._remember_template(data, detection.fingerprint, detection.document_type, warnings)

        failed_stages = [s for s in stage_results if s.status in (StageStatus.FAILED, StageStatus.DEGRADED)]
        data.extraction_status = ExtractionStatus.PARTIAL if failed_stages else ExtractionStatus.COMPLETE
        data.extraction_warnings = warnings + list(data.extraction_warnings)
        data.page_summaries = page_summaries
        data = apply_conflicts_to_fields(data, conflicts)
        stage_results.append(StageResult(stage="conflict_detection", detail=f"{len(data.source_conflicts)} unresolved conflict(s)"))
        data = self.validator.validate(data)
        stage_results.append(StageResult(stage="validation"))
        data.stage_results = stage_results
        data.pipeline_stages = [s.stage for s in stage_results]

        audit(
            "extraction_complete",
            pages=pdf_result.page_count,
            is_scanned=pdf_result.is_scanned,
            status=data.extraction_status.value,
            confidence=data.overall_confidence,
            accused=len(data.accused_persons),
            witnesses=len(data.witnesses),
            conflicts=len(data.source_conflicts or []),
        )
        return data

    def _failed_result(
        self,
        pdf_result: PdfExtractionResult,
        page_summaries: List[PageSummary],
        stage_results: List[StageResult],
        warnings: List[str],
    ) -> ChargeSheetData:
        ocr_ran_empty = page_summaries and all(
            p.ocr_engine and p.text_source in ("none", "ocr") for p in page_summaries
        )
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
        stage_results.append(StageResult(stage="form_layout_parse", status=StageStatus.SKIPPED, detail="no text"))
        data = ChargeSheetData(
            document_info=DocumentTypeInfo(
                document_type=_field("document_type", value="unknown", extraction_confidence=0.0, mapping_confidence=0.0),
                classification_confidence=0.0,
                classification_notes="no text available",
            ),
            extraction_status=ExtractionStatus.FAILED,
            extraction_warnings=warnings,
            stage_results=stage_results,
            page_summaries=page_summaries,
            pipeline_stages=[s.stage for s in stage_results],
        )
        data = self.validator.validate(data)
        audit("extraction_failed", pages=pdf_result.page_count, reason="no_text")
        return data

    # ------------------------------------------------------------------
    # Mapping stages
    # ------------------------------------------------------------------

    def _map_case_fields(
        self,
        data: ChargeSheetData,
        parsed: FormParseResult,
        selected: Dict[str, SelectedField],
        conflicts: List[SourceConflict],
    ) -> str:
        case = CaseIdentifiers()
        for key in CASE_KEYS:
            sel = selected.get(key)
            if not sel:
                continue
            c = sel.candidate
            value = clean_value(c.value)
            if key in {"fir_date", "submission_date"}:
                # "02.03.2024 at 1500 hrs" → the date token; the full text stays in source_text
                dm = re.search(r"\d{1,2}\s*[./\-]\s*\d{1,2}\s*[./\-]\s*\d{2,4}", value)
                if dm:
                    value = dm.group(0)
            setattr(
                case,
                key,
                _field(
                    key,
                    value=value,
                    label=c.label,
                    page=c.source_page,
                    source_text=c.source_text,
                    mapping_confidence=c.mapping_confidence,
                    extraction_confidence=self._ext_conf(c.source_page),
                    alternate_values=[clean_value(a.value) for a in sel.alternates],
                    notes=c.note,
                    location=f"{c.origin}{':' + c.form_hint if c.form_hint else ''}",
                ),
            )
            if sel.conflict:
                conflicts.append(sel.conflict)

        primary = re.sub(r"\s+", "", case.fir_number.original_value) if case.fir_number else None
        refs = parsed.case_references
        if primary is None:
            # No FIR number in the form: a registration statement in the narrative is the next best source
            reg = next((r for r in refs if r.registration), None)
            if reg:
                case.fir_number = _field(
                    "fir_number",
                    value=reg.number,
                    label="registration statement",
                    page=reg.page,
                    source_text=reg.context,
                    mapping_confidence=0.6,
                    extraction_confidence=self._ext_conf(reg.page),
                    notes="Not in the form section; taken from the narrative case-registration statement",
                    location="narrative_registration",
                )
                primary = reg.number
        if primary:
            for r in refs:
                if r.registration and r.number != primary:
                    conflicts.append(
                        SourceConflict(
                            field_name="fir_number",
                            values=[primary, r.number],
                            source_pages=[p for p in [case.fir_number.source_page, r.page] if p],
                            evidence=f"Narrative registration statement cites a different number: {r.context[:160]!r}",
                            resolution="unresolved",
                        )
                    )
        seen = {primary}
        for r in refs:
            if r.registration or r.number in seen:
                continue
            seen.add(r.number)
            data.related_case_references.append(
                _field(
                    f"related_case[{len(data.related_case_references)}]",
                    value=r.number,
                    label="FIR / Crime No. mentioned in narrative",
                    page=r.page,
                    source_text=r.context,
                    mapping_confidence=0.9,
                    extraction_confidence=self._ext_conf(r.page),
                    notes="Different case referenced in the narrative — not the primary FIR",
                )
            )

        if case.year is None and case.fir_number:
            m = re.search(r"/\s*((?:19|20)\d{2})\b", str(case.fir_number.original_value))
            if m:
                case.year = _field(
                    "year",
                    value=m.group(1),
                    label="derived from FIR number",
                    page=case.fir_number.source_page,
                    source_text=case.fir_number.source_text,
                    mapping_confidence=0.6,
                    extraction_confidence=self._ext_conf(case.fir_number.source_page),
                    notes="No year field in the source; derived from the FIR number suffix",
                    location="derived",
                )
        data.case = case

        # Unmapped form fields: preserved, never forced into schema fields
        used = {(c.label, c.value) for s in selected.values() for c in [s.candidate]}
        for i, u in enumerate(parsed.unmapped[:40]):
            if (u.label, u.value) in used or not clean_value(u.value):
                continue
            canon_hint = best_canonical_match(u.label, include_hints=True)[0]
            name = (canon_hint or "").replace("hint:", "") or re.sub(r"[^a-z0-9]+", "_", u.label.lower()).strip("_")[:40] or "field"
            data.unmapped_fields.append(
                _field(
                    f"unmapped[{len(data.unmapped_fields)}].{name}",
                    value=clean_value(u.value),
                    label=u.label,
                    page=u.source_page,
                    source_text=u.source_text,
                    mapping_confidence=float(u.mapping_confidence or 0.0),
                    extraction_confidence=0.5,
                    location=f"{u.origin}{':' + u.form_hint if u.form_hint else ''}",
                )
            )
        filled = [k for k in CASE_KEYS if getattr(case, k) is not None]
        return f"{len(filled)} case field(s); {len(data.related_case_references)} related case reference(s)"

    def _person_fields(self, prefix: str, ent: PersonEntity) -> dict:
        ext_conf = self._ext_conf(ent.source_page)
        src = ent.raw[:300]

        def f(attr: str, value: Optional[str], mapping: float = 0.85) -> Optional[ExtractedField]:
            if not value:
                return None
            return _field(f"{prefix}.{attr}", value=value, page=ent.source_page, source_text=src,
                          extraction_confidence=ext_conf, mapping_confidence=mapping)

        extra = []
        if ent.alias:
            extra.append(f("alias_of_name", ent.alias))
        if ent.caste:
            extra.append(f("caste", ent.caste))
        if ent.details:
            extra.append(f("unlabelled_details", ent.details, 0.5))
        return {
            "name": f("name", ent.name, 0.9),
            "parent_or_spouse": f("parent_or_spouse", ent.parent_or_spouse),
            "age": f("age", ent.age),
            "occupation": f("occupation", ent.occupation),
            "address": f("address", ent.address),
            "identification": f("identification", ent.identification),
            "contact": f("contact", ent.contact),
            "additional_fields": [x for x in extra if x],
        }

    def _map_entities(
        self,
        data: ChargeSheetData,
        parsed: FormParseResult,
        selected: Dict[str, SelectedField],
        conflicts: List[SourceConflict],
    ) -> str:
        accused = extract_accused(parsed.lists.get("accused_charged", []))
        witnesses = extract_witnesses(parsed.lists.get("witnesses", []))
        for i, ent in enumerate(accused):
            fields = self._person_fields(f"accused[{i}]", ent)
            data.accused_persons.append(
                AccusedPerson(
                    serial_no=_field(f"accused[{i}].serial_no", value=ent.serial_no, page=ent.source_page,
                                     mapping_confidence=0.9, extraction_confidence=self._ext_conf(ent.source_page)) if ent.serial_no else None,
                    arrest_status=_field(f"accused[{i}].arrest_status", value=ent.arrest_status, page=ent.source_page,
                                         source_text=ent.raw[:300], mapping_confidence=0.85,
                                         extraction_confidence=self._ext_conf(ent.source_page)) if ent.arrest_status else None,
                    **fields,
                )
            )
        for i, ent in enumerate(witnesses):
            fields = self._person_fields(f"witness[{i}]", ent)
            data.witnesses.append(
                Witness(
                    serial_no=_field(f"witness[{i}].serial_no", value=ent.serial_no, page=ent.source_page,
                                     mapping_confidence=0.9, extraction_confidence=self._ext_conf(ent.source_page)) if ent.serial_no else None,
                    witness_type=_field(f"witness[{i}].witness_type", value=ent.role, page=ent.source_page,
                                        source_text=ent.raw[:300], mapping_confidence=0.85,
                                        extraction_confidence=self._ext_conf(ent.source_page), notes=ent.role_note) if ent.role else None,
                    **fields,
                )
            )

        io = selected.get("io_name")
        if io:
            officers = parse_officers(io.candidate.value, io.candidate.source_page)
            for i, ent in enumerate(officers):
                fields = self._person_fields(f"io[{i}]", ent)
                fields["name"].original_label = io.candidate.label
                data.investigating_officers.append(
                    PersonDetails(role=_field(f"io[{i}].role", value="Investigating Officer", label=io.candidate.label,
                                              page=ent.source_page), **fields)
                )
            if data.investigating_officers:
                data.investigating_officer = data.investigating_officers[0]
        for key, attr, prefix in (("complainant_name", "complainant", "complainant"), ("informant_name", "informant", "informant")):
            sel = selected.get(key)
            if not sel:
                continue
            c = sel.candidate
            ent = parse_person(c.value, page=c.source_page)
            value = clean_value(c.value)
            person = PersonDetails(
                name=_field(f"{prefix}.name", value=value, label=c.label, page=c.source_page, source_text=c.source_text,
                            mapping_confidence=c.mapping_confidence, extraction_confidence=self._ext_conf(c.source_page)),
                parent_or_spouse=_field(f"{prefix}.parent_or_spouse", value=ent.parent_or_spouse, page=c.source_page,
                                        source_text=c.source_text) if ent.parent_or_spouse else None,
            )
            setattr(data, attr, person)

        mentions = find_person_mentions(parsed.narrative.segments)
        conflicts.extend(detect_person_conflicts(accused, mentions, "accused"))
        conflicts.extend(detect_person_conflicts(witnesses, mentions, "witness"))
        if not parsed.lists.get("accused_charged"):
            data.extraction_warnings.append("No accused list found in the form section")
        return f"{len(accused)} accused, {len(witnesses)} witness(es), {len(data.investigating_officers)} IO(s)"

    def _map_legal(self, data: ChargeSheetData, parsed: FormParseResult, conflicts: List[SourceConflict]) -> str:
        provisions: List[Provision] = []
        for page, text in parsed.acts_texts:
            provisions.extend(parse_provisions(text, zone="form", page=page, default_role="charged"))
        # Form-zone free text (titles such as "u/s 173 CrPC") — never charges by default
        acts_set = {t for _, t in parsed.acts_texts}
        for page, text in parsed.form_lines:
            if text in acts_set:
                continue
            for p in parse_provisions(text, zone="form", page=page):
                if p.role in {"procedural", "reference"}:
                    provisions.append(p)
        narrative = parsed.narrative
        provisions.extend(parse_provisions(narrative.text, zone="narrative", page_at=narrative.page_at))
        charged, others, legal_conflicts = consolidate(provisions)
        conflicts.extend(legal_conflicts)

        def to_schema(entries: List[LegalEntry], prefix: str) -> List[LegalSection]:
            out = []
            for i, e in enumerate(entries):
                page = e.pages[0] if e.pages else None
                out.append(
                    LegalSection(
                        act=_field(f"{prefix}[{i}].act", value=e.act, page=page, source_text=e.source_text, mapping_confidence=0.9)
                        if e.act else None,
                        section=_field(f"{prefix}[{i}].section", value=e.section, page=page, source_text=e.source_text,
                                       mapping_confidence=0.9),
                        provision_role=e.role,
                        source_pages=e.pages,
                    )
                )
            return out

        data.acts_and_sections = to_schema(charged, "legal")
        data.other_legal_references = to_schema(others, "legal_ref")
        if charged and all(e.role == "registered" for e in charged):
            data.extraction_warnings.append(
                "No charged sections found; showing sections from the case-registration statement"
            )
        return f"{len(charged)} applicable provision(s), {len(others)} other reference(s)"

    def _map_narrative(self, data: ChargeSheetData, parsed: FormParseResult, selected: Dict[str, SelectedField]) -> str:
        narrative = parsed.narrative
        sections: Dict[str, List[Tuple[int, str]]] = {"brief_facts": [], "investigation_narrative": [], "final_conclusions": []}
        current = "brief_facts"
        for page, line in narrative.segments:
            if (page, line) in parsed.consumed_narrative:
                continue
            head = line.split(":", 1)[0] if ":" in line[:60] else line
            canon, score = best_canonical_match(head) if len(head) <= 60 else (None, 0.0)
            if canon in sections and score >= 0.88:
                current = canon
                rest = line.split(":", 1)[1].strip() if ":" in line[:60] else ""
                if rest:
                    sections[current].append((page, rest))
                continue
            sections[current].append((page, line))
        for key, parts in sections.items():
            text = re.sub(r"\s+", " ", " ".join(t for _, t in parts)).strip()
            # Drop headings / court address / salutation that precede the facts
            # (anything before the salutation is address/heading, never facts)
            text = re.sub(
                r"(?is)^.{0,240}?(?:hon'?\s*ble|honou?red|respected)\s*sir(?:\s*/\s*madam)?\s*,?\s*", "", text
            ).strip()
            for _ in range(2):
                text = re.sub(
                    r"(?i)^(?:\d{0,2}\s*(?:the\s*)?brief\s*facts\s*of\s*the\s*case\s*(?:are|is)?\s*(?:that)?\.?"
                    r"|in\s*the\s*court\s*of\s+[^.]{0,160}\.)\s*",
                    "",
                    text,
                ).strip()
            if len(text) < 15:
                sel = selected.get(key)
                if sel:
                    text, parts = clean_value(sel.candidate.value), [(sel.candidate.source_page, sel.candidate.value)]
                else:
                    continue
            setattr(
                data,
                key,
                _field(key, value=text[:6000], label=key.replace("_", " "), page=parts[0][0],
                       source_text=text[:200], mapping_confidence=0.85, extraction_confidence=0.7),
            )

        sel = selected.get("seized_property")
        if sel:
            c = sel.candidate
            data.seized_property.append(
                SeizedItem(description=_field("seized[0].description", value=clean_value(c.value), label=c.label,
                                              page=c.source_page, source_text=c.source_text,
                                              mapping_confidence=c.mapping_confidence))
            )
        sel = selected.get("lab_results")
        if sel:
            c = sel.candidate
            data.lab_results.append(
                LabResult(findings=_field("lab[0].findings", value=clean_value(c.value), label=c.label, page=c.source_page,
                                          source_text=c.source_text, mapping_confidence=c.mapping_confidence))
            )
        # FSL / chemical examiner opinion: the first sentence that names a lab and reports a finding
        text = narrative.text
        for m in _LAB_MENTION.finditer(text):
            start, end = sentence_bounds(text, m.start())
            sentence = re.sub(r"\s+", " ", text[start:end]).strip()
            if _LAB_FINDING.search(sentence):
                data.lab_results.append(
                    LabResult(findings=_field(f"lab[{len(data.lab_results)}].findings", value=sentence[:800],
                                              page=narrative.page_at(m.start()), source_text=sentence[:200],
                                              mapping_confidence=0.75, extraction_confidence=0.6,
                                              notes="Sentence from the investigation narrative"))
                )
                break
        return f"narrative {len(text)} chars; {len(data.lab_results)} lab finding(s)"

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

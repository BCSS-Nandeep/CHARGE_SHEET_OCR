"""Staged local extraction pipeline: PDF → OCR → parse → map → entities → conflicts → validate."""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Optional

from sqlalchemy.orm import Session

from app.config import get_settings
from app.logging_utils import audit, get_logger, safe_exc_message
from app.schemas.charge_sheet import (
    AccusedPerson,
    CaseIdentifiers,
    ChargeSheetData,
    DocumentTypeInfo,
    ExtractedField,
    LabResult,
    LegalSection,
    PersonDetails,
    ReviewStatus,
    SeizedItem,
    ValidationStatus,
    Witness,
)
from app.services.conflict_service import apply_conflicts_to_fields, detect_person_conflicts
from app.services.document_detector import DetectionResult, DocumentDetector
from app.services.entity_extractor import (
    extract_accused_from_block,
    extract_witnesses_from_block,
    find_name_age_mentions,
)
from app.services.form_parser import dedupe_raw, parse_document_pages
from app.services.legal_section_parser import merge_section_lists, parse_legal_sections
from app.services.ocr_service import OcrService
from app.services.pdf_service import PdfService
from app.services.template_service import TemplateService
from app.services.validation_service import ValidationService, normalize_simple

logger = get_logger(__name__)


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
) -> ExtractedField:
    norm = normalize_simple(value)
    return ExtractedField(
        canonical_name=canonical,
        original_label=label,
        original_value=value,
        normalized_value=norm,
        source_page=page,
        source_text=source_text,
        extraction_confidence=extraction_confidence,
        mapping_confidence=mapping_confidence,
        validation_status=ValidationStatus.PENDING,
        review_status=ReviewStatus.UNREVIEWED,
        alternate_values=list(alternate_values or []),
    )


class ExtractionService:
    def __init__(self, db: Optional[Session] = None):
        self.settings = get_settings()
        self.pdf = PdfService()
        self.ocr = OcrService()
        self.detector = DocumentDetector()
        self.validator = ValidationService()
        self.db = db

    def process(self, path: Path) -> ChargeSheetData:
        audit("extraction_start", path_name=path.name)
        stages: List[str] = []
        warnings: List[str] = []

        # --- Stage 1: PDF text/layout ---
        pdf_result = self.pdf.extract(path)
        warnings.extend(pdf_result.warnings)
        stages.append("pdf_text_layout")

        # --- Stage 2: OCR (local only) ---
        for page in pdf_result.pages:
            if not page.needs_ocr:
                continue
            if not self.settings.ocr_enabled:
                warnings.append(f"Page {page.page_number} has little text; OCR disabled")
                continue
            if not self.ocr.is_available():
                warnings.append(
                    f"Page {page.page_number} needs OCR but no local OCR engine is installed "
                    f"(PaddleOCR or Tesseract). See README."
                )
                continue
            try:
                png = self.pdf.render_page_image(path, page.page_number)
                text, conf = self.ocr.recognize_png(png)
                if text.strip():
                    page.text = text.strip()
                    page.char_count = len(page.text)
                    page.needs_ocr = False
                    page.blocks.append({"bbox": None, "text": page.text, "ocr_confidence": conf})
                else:
                    warnings.append(f"OCR produced no text for page {page.page_number}")
            except Exception as exc:
                warnings.append(f"OCR failed for page {page.page_number}: {safe_exc_message(exc)}")
        stages.append("ocr_local")

        from app.services.text_normalize import soften_pdf_text

        pages = [(p.page_number, soften_pdf_text(p.text)) for p in pdf_result.pages]
        full_text = "\n\n".join(f"--- PAGE {n} ---\n{t}" for n, t in pages)
        ocr_needed = any(p.needs_ocr for p in pdf_result.pages)
        ocr_missing = ocr_needed and not self.ocr.is_available()

        # --- Stage 3: Form / layout candidate extraction ---
        parsed = parse_document_pages(pages)
        stages.append("form_layout_parse")

        # --- Stage 4: Semantic field mapping (deduped scalars) ---
        mapped = dedupe_raw(parsed.candidates)
        stages.append("semantic_field_mapping")

        case = CaseIdentifiers()
        case_keys = [
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
        for key in case_keys:
            c = mapped.get(key)
            if not c:
                continue
            if key == "court_name" and len(c.value) > 120:
                continue
            value = c.value
            source_text = c.source_text
            # Keep compound year line in source_text; store year digit as original extracted year
            if key == "year":
                ym = re.search(r"\b((?:19|20)\d{2})\b", value or "")
                if ym:
                    source_text = c.source_text or value
                    value = ym.group(1)
            setattr(
                case,
                key,
                _field(
                    key,
                    value=value,
                    label=c.label,
                    page=c.source_page,
                    source_text=source_text,
                    mapping_confidence=c.mapping_confidence,
                    extraction_confidence=0.8 if not pdf_result.is_scanned else 0.55,
                ),
            )

        # Prefer explicit Dt.dd.mm.yyyy near FIR header as fir_date
        if not case.fir_date or (
            case.submission_date
            and case.fir_date.original_value == case.submission_date.original_value
        ):
            for page_no, text in pages:
                dm = re.search(r"\bDt\.?\s*(\d{1,2}[./\-]\d{1,2}[./\-]\d{2,4})", text, re.I)
                if dm:
                    case.fir_date = _field(
                        "fir_date",
                        value=dm.group(1),
                        label="Dt.",
                        page=page_no,
                        source_text=dm.group(0),
                        mapping_confidence=0.9,
                    )
                    break
        # Alternate: "On 01.10.2021 at ..." near FIR registration line
        if not case.fir_date:
            for page_no, text in pages:
                dm = re.search(
                    r"(?i)(?:FIR\.?\s*No\.?[^\n]{0,40})\n?\s*On\s*(\d{1,2}[./\-]\d{1,2}[./\-]\d{2,4})",
                    text,
                )
                if dm:
                    case.fir_date = _field(
                        "fir_date",
                        value=dm.group(1),
                        label="On",
                        page=page_no,
                        source_text=dm.group(0)[:200],
                        mapping_confidence=0.85,
                    )
                    break

        # Final report type / original from section-ish candidates
        for c in parsed.candidates:
            low = (c.label or "").lower()
            if "type of final" in low or low.strip() in {"type of final result", "final report type"}:
                if c.value and not case.final_report_type:
                    case.final_report_type = _field(
                        "final_report_type",
                        value=c.value,
                        label=c.label,
                        page=c.source_page,
                        source_text=c.source_text,
                        mapping_confidence=0.9,
                    )
            if "supplementary" in low or "original" in low:
                if c.value and not case.supplementary_or_original:
                    case.supplementary_or_original = _field(
                        "supplementary_or_original",
                        value=c.value,
                        label=c.label,
                        page=c.source_page,
                        source_text=c.source_text,
                        mapping_confidence=0.9,
                    )

        # Heuristic: lines that are just "Charge sheet." / "Original." after type headings
        if not case.final_report_type:
            for page_no, text in pages:
                m = re.search(
                    r"type of final result\.?\s*\n\s*([A-Za-z ]{4,40})",
                    text,
                    re.I,
                )
                if m:
                    case.final_report_type = _field(
                        "final_report_type",
                        value=m.group(1).strip(" ."),
                        label="Type of Final Result",
                        page=page_no,
                        source_text=m.group(0)[:200],
                    )
                    break
        if not case.supplementary_or_original:
            for page_no, text in pages:
                m = re.search(
                    r"(?:supplementary or original)\.?\s*\n\s*(Original|Supplementary)",
                    text,
                    re.I,
                )
                if m:
                    case.supplementary_or_original = _field(
                        "supplementary_or_original",
                        value=m.group(1),
                        label="Supplementary or Original",
                        page=page_no,
                        source_text=m.group(0)[:200],
                    )
                    break

        # --- Stage 5: Entity extraction ---
        accused_block = parsed.section_blocks.get("accused_charged", "")
        witness_block = parsed.section_blocks.get("witnesses", "")
        # Fallback: search pages for accused/witness headings instead of whole-doc bleed
        if not accused_block:
            for page_no, text in pages:
                m = re.search(
                    r"((?:particulars\s+of\s+(?:the\s+)?accused|list\s+of\s+accused|accused)\b.{0,1200})",
                    text,
                    re.I | re.S,
                )
                if m:
                    accused_block = m.group(1)
                    break
        if not witness_block:
            for page_no, text in pages:
                m = re.search(
                    r"((?:list\s+of\s+witnesses|particulars\s+of\s+witness|witnesses)\b.{0,1500})",
                    text,
                    re.I | re.S,
                )
                if m:
                    witness_block = m.group(1)
                    break
        accused_ents = extract_accused_from_block(accused_block, page=1)
        witness_ents = extract_witnesses_from_block(witness_block, page=1)

        conflicts = []
        accused_persons: List[AccusedPerson] = []
        for i, ent in enumerate(accused_ents):
            cross = find_name_age_mentions(full_text, ent.name or "") if ent.name else None
            if cross:
                conflicts.extend(detect_person_conflicts(ent, cross))
                # Prefer form-column age as original; keep alternates
                alts = []
                if cross.name_variants:
                    alts = [v for v in cross.name_variants if v != ent.name]
                age_alts = [a for a in (cross.age_mentions or []) if a != ent.age]
            else:
                alts, age_alts = [], []

            accused_persons.append(
                AccusedPerson(
                    serial_no=_field(f"accused[{i}].serial_no", value=ent.serial_no, page=ent.source_page)
                    if ent.serial_no
                    else None,
                    name=_field(
                        f"accused[{i}].name",
                        value=ent.name,
                        page=ent.source_page,
                        source_text=ent.raw[:300],
                        extraction_confidence=0.85,
                        mapping_confidence=0.9,
                        alternate_values=alts,
                    )
                    if ent.name
                    else None,
                    parent_or_spouse=_field(
                        f"accused[{i}].parent_or_spouse",
                        value=ent.parent_or_spouse,
                        page=ent.source_page,
                        source_text=ent.raw[:300],
                    )
                    if ent.parent_or_spouse
                    else None,
                    age=_field(
                        f"accused[{i}].age",
                        value=ent.age,
                        page=ent.source_page,
                        source_text=ent.raw[:300],
                        alternate_values=age_alts,
                    )
                    if ent.age
                    else None,
                    address=_field(
                        f"accused[{i}].address",
                        value=ent.address,
                        page=ent.source_page,
                        source_text=ent.raw[:300],
                    )
                    if ent.address
                    else None,
                    occupation=_field(
                        f"accused[{i}].occupation",
                        value=ent.occupation,
                        page=ent.source_page,
                        source_text=ent.raw[:300],
                    )
                    if ent.occupation
                    else None,
                    identification=_field(
                        f"accused[{i}].identification",
                        value=ent.identification,
                        page=ent.source_page,
                        source_text=ent.raw[:300],
                    )
                    if ent.identification
                    else None,
                    additional_fields=[
                        _field(
                            f"accused[{i}].contact",
                            value=ent.contact,
                            page=ent.source_page,
                            source_text=ent.raw[:300],
                        )
                    ]
                    if ent.contact
                    else [],
                )
            )

        witnesses: List[Witness] = []
        for i, ent in enumerate(witness_ents):
            witnesses.append(
                Witness(
                    serial_no=_field(f"witness[{i}].serial_no", value=ent.serial_no, page=ent.source_page)
                    if ent.serial_no
                    else None,
                    name=_field(
                        f"witness[{i}].name",
                        value=ent.name,
                        page=ent.source_page,
                        source_text=ent.raw[:300],
                        extraction_confidence=0.8,
                        mapping_confidence=0.85,
                    )
                    if ent.name
                    else None,
                    parent_or_spouse=_field(
                        f"witness[{i}].parent_or_spouse",
                        value=ent.parent_or_spouse,
                        page=ent.source_page,
                        source_text=ent.raw[:300],
                    )
                    if ent.parent_or_spouse
                    else None,
                    age=_field(
                        f"witness[{i}].age",
                        value=ent.age,
                        page=ent.source_page,
                        source_text=ent.raw[:300],
                    )
                    if ent.age
                    else None,
                    address=_field(
                        f"witness[{i}].address",
                        value=ent.address,
                        page=ent.source_page,
                        source_text=ent.raw[:300],
                    )
                    if ent.address
                    else None,
                    occupation=_field(
                        f"witness[{i}].occupation",
                        value=ent.occupation,
                        page=ent.source_page,
                        source_text=ent.raw[:300],
                    )
                    if ent.occupation
                    else None,
                    witness_type=_field(
                        f"witness[{i}].witness_type",
                        value=ent.role,
                        page=ent.source_page,
                        source_text=ent.raw[:300],
                    )
                    if ent.role
                    else None,
                )
            )
        stages.append("entity_extraction")

        # People from mapped scalars
        complainant = None
        if mapped.get("complainant_name"):
            c = mapped["complainant_name"]
            complainant = PersonDetails(
                name=_field(
                    "complainant.name",
                    value=c.value,
                    label=c.label,
                    page=c.source_page,
                    source_text=c.source_text,
                    mapping_confidence=c.mapping_confidence,
                )
            )
        # Prefer block IO / complainant from candidates with better labels
        io = None
        for c in parsed.candidates:
            low = (c.label or "").lower()
            if ("investigating" in low or low.startswith("name and rank of i.o") or "i.o" in low) and c.value:
                io = PersonDetails(
                    name=_field(
                        "investigating_officer.name",
                        value=c.value,
                        label=c.label,
                        page=c.source_page,
                        source_text=c.source_text,
                        mapping_confidence=0.9,
                    )
                )
                break
        if not io and mapped.get("io_name"):
            c = mapped["io_name"]
            io = PersonDetails(
                name=_field(
                    "investigating_officer.name",
                    value=c.value,
                    label=c.label,
                    page=c.source_page,
                    source_text=c.source_text,
                    mapping_confidence=c.mapping_confidence,
                )
            )

        # --- Stage 6: Legal sections (form Act block only — avoid narrative noise) ---
        section_lists = []
        if parsed.section_blocks.get("acts"):
            section_lists.append(
                parse_legal_sections(parsed.section_blocks["acts"], 1, focused_only=True)
            )
        # Also scan page 1 header region only
        if pages:
            section_lists.append(parse_legal_sections(pages[0][1][:1200], pages[0][0], focused_only=True))
        legal = merge_section_lists(*section_lists)
        legal_sections = [
            LegalSection(
                act=_field("legal.act", value=s.act, page=s.source_page, source_text=s.source_text)
                if s.act
                else None,
                section=_field(
                    "legal.section",
                    value=s.section,
                    page=s.source_page,
                    source_text=s.source_text,
                    mapping_confidence=0.92,
                ),
            )
            for s in legal
        ]
        stages.append("legal_section_parse")

        # Narratives
        brief = None
        if parsed.section_blocks.get("brief_facts"):
            chunk = parsed.section_blocks["brief_facts"]
            chunk = re.sub(
                r"^.*?brief facts of the case\.?\s*",
                "",
                chunk,
                count=1,
                flags=re.I | re.S,
            )
            brief = _field(
                "brief_facts",
                value=chunk[:1500].strip(),
                label="Brief Facts",
                page=1,
                source_text=chunk[:200],
                extraction_confidence=0.7,
                mapping_confidence=0.9,
            )

        lab_results: List[LabResult] = []
        fsl_hit = re.search(
            r"(?:FSL[^\n]{0,160}.{0,240}?(?:opinion|ascertained|report|found)[^\n]{0,240}"
            r"|chemical examiner[^\n]{0,240})",
            full_text,
            re.I | re.S,
        )
        if fsl_hit:
            findings = re.sub(r"\s+", " ", fsl_hit.group(0)).strip()
            page_guess = 1
            for page_no, text in pages:
                if findings[:40].lower() in text.lower() or "fsl" in text.lower():
                    page_guess = page_no
                    break
            lab_results.append(
                LabResult(
                    findings=_field(
                        "lab[0].findings",
                        value=findings[:800],
                        page=page_guess,
                        source_text=findings[:200],
                        mapping_confidence=0.85,
                    ),
                )
            )

        seized: List[SeizedItem] = []
        m = re.search(
            r"seized\s+\d+\s*(?:liters?|lts?|grams?|kgs?|packets?).{0,140}",
            full_text,
            re.I | re.S,
        )
        if m:
            page_guess = 1
            for page_no, text in pages:
                if m.group(0)[:30].lower() in text.lower():
                    page_guess = page_no
                    break
            seized.append(
                SeizedItem(
                    description=_field(
                        "seized[0].description",
                        value=re.sub(r"\s+", " ", m.group(0)).strip(),
                        page=page_guess,
                        source_text=m.group(0)[:200],
                        mapping_confidence=0.85,
                    )
                )
            )

        # Unmapped: only meaningful leftovers not already represented
        used_labels = {(c.label or "").lower() for c in mapped.values()}
        used_values = {(c.value or "").strip().lower() for c in mapped.values() if c.value}
        unmapped: List[ExtractedField] = []
        skip_labels = {
            "caste",
            "occ",
            "occupation",
            "adhar",
            "aadhaar",
            "contact",
            "issued",
            "r/o",
            "s/o",
            "age",
        }
        for c in parsed.candidates:
            if c.canonical and c.canonical in mapped:
                continue
            if not c.value or c.value.strip() in {"-", "—", "---", "."}:
                continue
            lab = (c.label or "").lower()
            if any(s in lab for s in skip_labels):
                continue
            if lab in used_labels:
                continue
            if c.value.strip().lower() in used_values:
                continue
            if len(lab) < 2 or lab.isdigit():
                continue
            # Skip long narrative fragments as unmapped KV
            if len(c.label or "") > 60:
                continue
            unmapped.append(
                _field(
                    f"unmapped.{normalize_simple(c.label) or 'field'}",
                    value=c.value,
                    label=c.label,
                    page=c.source_page,
                    source_text=c.source_text,
                    mapping_confidence=float(c.mapping_confidence or 0),
                    extraction_confidence=0.5,
                )
            )
        unmapped = unmapped[:30]

        detection = self.detector.detect(
            full_text, [c.label for c in parsed.candidates if c.label]
        )
        template_id = None
        previously_seen = False
        if self.db is not None:
            try:
                tpl_svc = TemplateService(self.db)
                match = tpl_svc.match(detection.fingerprint)
                if match:
                    template_id = match.id
                    previously_seen = True
                    tpl_svc.remember(detection.fingerprint, label=detection.document_type)
                else:
                    remembered = tpl_svc.remember(
                        detection.fingerprint, label=detection.document_type
                    )
                    template_id = remembered.id
            except Exception as exc:
                warnings.append(f"Template store error: {safe_exc_message(exc)}")

        if detection.uncertain:
            warnings.append("Document could not be classified confidently")
        if ocr_missing:
            warnings.append(
                "OCR_REQUIRED: scanned/image pages detected but no local OCR engine is available. "
                "Install PaddleOCR (or Tesseract) locally — see README — then reprocess. "
                "No fields were invented from empty pages."
            )
            detection = DetectionResult(
                document_type="unknown",
                confidence=min(detection.confidence, 0.2),
                fingerprint=detection.fingerprint,
                notes=detection.notes + "; ocr_required",
                section_hits=detection.section_hits,
                uncertain=True,
            )

        data = ChargeSheetData(
            document_info=DocumentTypeInfo(
                document_type=_field(
                    "document_type",
                    value=detection.document_type,
                    extraction_confidence=detection.confidence,
                    mapping_confidence=detection.confidence,
                ),
                classification_confidence=detection.confidence,
                classification_notes=detection.notes,
                template_fingerprint=detection.fingerprint,
                template_id=template_id,
                previously_seen=previously_seen,
            ),
            case=case,
            acts_and_sections=legal_sections,
            complainant=complainant,
            investigating_officer=io,
            accused_persons=accused_persons,
            witnesses=witnesses,
            seized_property=seized,
            lab_results=lab_results,
            brief_facts=brief,
            unmapped_fields=unmapped,
            extraction_warnings=warnings,
            requires_human_review=True,
            pipeline_stages=stages
            + ["conflict_detection", "validation"],
        )

        # --- Stage 7: Conflicts (never auto-resolve) ---
        data = apply_conflicts_to_fields(data, conflicts)
        stages.append("conflict_detection")

        # --- Stage 8: Validation ---
        data = self.validator.validate(data)
        stages.append("validation")
        data.pipeline_stages = stages

        audit(
            "extraction_complete",
            pages=pdf_result.page_count,
            is_scanned=pdf_result.is_scanned,
            confidence=data.overall_confidence,
            accused=len(accused_persons),
            witnesses=len(witnesses),
            conflicts=len(data.source_conflicts or []),
        )
        return data

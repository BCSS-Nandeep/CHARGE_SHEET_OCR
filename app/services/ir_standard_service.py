"""Standard-template IR extraction (VLM) behind the existing pipeline.

DOCX reports are read natively (bold, ticks and bullets kept) and PDF/scanned
pages are read by the local VLM; either way the text is mapped onto the fixed
32-section template, validated in Python, and exposed both as the standard
JSON and as the field list the UI already renders.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from app.config import get_settings
from app.ir_standard.docx_markdown import docx_to_markdown
from app.ir_standard.mapper import map_markdown, transcribe_pages
from app.ir_standard.paths import flatten
from app.ir_standard.vlm_client import VlmClient, VlmError
from app.logging_utils import get_logger
from app.schemas.charge_sheet import PdfField, ReviewStatus

logger = get_logger(__name__)


class IrStandardService:
    def __init__(self, client: Optional[VlmClient] = None):
        self.settings = get_settings()
        self._client = client

    def enabled(self) -> bool:
        return bool(self.settings.ir_standard_enabled)

    def client(self) -> VlmClient:
        """The VLM client (raises VlmError when disabled, non-local or misconfigured)."""
        if self._client is None:
            self._client = VlmClient(self.settings)
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def ready(self) -> Optional[str]:
        """None when the VLM server can be used, else the reason it cannot."""
        try:
            client = self.client()
        except VlmError as exc:
            return str(exc)
        return None if client.is_up() else f"VLM server at {self.settings.llm_base_url} is not reachable"

    # ------------------------------------------------------------------

    def from_docx(self, path: Path, progress: Optional[Callable[[str], None]] = None) -> Tuple[Dict, List[str]]:
        """(standard JSON, [markdown of the document])."""
        markdown = docx_to_markdown(path)
        return map_markdown(markdown, self.client(), progress, evidence_source="docx_native"), [markdown]

    def from_pages(
        self, pngs: List[bytes], progress: Optional[Callable[[str], None]] = None
    ) -> Tuple[Dict, List[str]]:
        """(standard JSON, markdown the VLM read from each page)."""
        pages = transcribe_pages(self.client(), pngs, progress)
        # marks come from the VLM's own transcription here, so they are weaker evidence than a DOCX
        return map_markdown("\n\n".join(pages), self.client(), progress, evidence_source="vlm_transcription"), pages

    # ------------------------------------------------------------------

    @staticmethod
    def fields_from_standard(standard: Dict) -> List[PdfField]:
        """The existing field list (what the UI shows and corrects), one entry per template value."""
        flags: Dict[str, str] = {}
        for f in standard.get("review_flags", []):
            path, detail = f.get("path", ""), f.get("detail", "")
            flags[path] = f"{flags[path]}; {detail}" if path in flags else detail
        fields: List[PdfField] = []
        for flat in flatten(standard):
            note = flags.get(flat.id)
            fields.append(
                PdfField(
                    id=flat.id,
                    page=flat.section,
                    pages=[flat.section],
                    serial=flat.serial,
                    label=flat.label,
                    value=flat.value,
                    source="standard_template",
                    text_block=flat.long_text,
                    review_status=ReviewStatus.NEEDS_REVIEW if flat.flagged else ReviewStatus.UNREVIEWED,
                    notes=note,
                    section_title=flat.section_title or None,
                    group=flat.group,
                )
            )
        return fields

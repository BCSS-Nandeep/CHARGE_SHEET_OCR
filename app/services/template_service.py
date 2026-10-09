"""Optional reuse of verified template fingerprints/mappings."""

from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from app.logging_utils import get_logger
from app.repositories.template_repository import TemplateRepository
from app.services.document_detector import DocumentDetector

logger = get_logger(__name__)


class TemplateService:
    def __init__(self, db: Session):
        self.repo = TemplateRepository(db)
        self.detector = DocumentDetector()

    def match(self, fingerprint: str, min_similarity: float = 1.0) -> Optional[object]:
        """Reuse mapping only when fingerprint matches appropriately.

        Default requires exact fingerprint match. Fuzzy reuse is disabled
        unless min_similarity < 1.0 and a verified template is close enough.
        """
        exact = self.repo.get_by_fingerprint(fingerprint)
        if exact:
            return exact
        if min_similarity >= 1.0:
            return None
        best = None
        best_score = 0.0
        for row in self.repo.list_all():
            if not row.verified:
                continue
            score = self.detector.similarity(fingerprint, row.fingerprint)
            if score > best_score:
                best_score = score
                best = row
        if best and best_score >= min_similarity:
            logger.info(
                "template_fuzzy_match score=%.2f template_id=%s",
                best_score,
                best.id,
            )
            return best
        return None

    def remember(
        self,
        fingerprint: str,
        *,
        label: Optional[str] = None,
        mapping_json: Optional[str] = None,
        verified: bool = False,
    ):
        return self.repo.upsert(
            fingerprint,
            label=label,
            mapping_json=mapping_json,
            verified=verified,
        )

    def list_profiles(self):
        return self.repo.list_all()

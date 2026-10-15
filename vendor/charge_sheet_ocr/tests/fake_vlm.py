"""A stand-in for the local model server, used by the pipeline tests.

``FakeVlm`` has the surface of ``VlmClient`` the pipeline uses. Its "mapping model" is a
few lines of rules that read ``Label : value`` / ``Label | value`` lines and numbered
person lines — enough to exercise the pipeline end to end. It says nothing about how
accurate the real model is; accuracy is only measured against the real server.
"""

from __future__ import annotations

import re
from typing import Callable, Dict, List, Optional, Set

from app.services.vlm_client import VlmReply, VlmTimeout, VlmUnavailable

_CASE = {
    "district": "district", "police station": "police_station", "year": "year", "fir no": "fir_number",
    "fir number": "fir_number", "crime no": "crime_number", "date of fir": "fir_date",
    "charge sheet no": "charge_sheet_number", "charge-sheet no": "charge_sheet_number",
    "date of submission": "submission_date", "court": "court_name", "jurisdiction": "jurisdiction",
    "final report type": "final_report_type", "original / supplementary": "supplementary_or_original",
}
_LINE = re.compile(r"^(?:\d+\.\s*)?(?P<label>[^:|]+?)\s*[:|]\s*(?P<value>.+)$")
_PERSON = re.compile(r"^(?P<serial>\d+)\.?\s*[|]?\s*(?P<rest>.+)$")


def rule_model(text: str) -> dict:
    out: dict = {"case": {}}
    section: Optional[str] = None
    for raw in text.splitlines():
        line = raw.strip()
        low = line.lower().rstrip(":")
        if re.fullmatch(r"(particulars of the )?accused( persons?)?|list of accused", low):
            section = "accused"
            continue
        if re.fullmatch(r"(list of )?witnesses( to be examined)?", low):
            section = "witnesses"
            continue
        m = _LINE.match(line)
        if m:
            label, value = m["label"].strip().lower().rstrip("."), m["value"].strip()
            if label in _CASE and value:
                out["case"][_CASE[label]] = value
                continue
            if label in ("acts and sections", "act"):
                am = re.match(r"u/s\s+(?P<sec>[\d, ()a-zA-Z]+?)\s+(?P<act>[A-Z][A-Za-z ]+)$", value)
                if am:
                    out.setdefault("acts_and_sections", []).append({"act": am["act"].strip(), "section": am["sec"].strip()})
                continue
            if label == "complainant":
                out.setdefault("complainant", {})["name"] = value
                continue
            if label == "investigating officer":
                out.setdefault("investigating_officers", []).append({"name": value})
                continue
        pm = _PERSON.match(line)
        if pm and section:
            rest = pm["rest"]
            rec: Dict[str, str] = {"serial_no": pm["serial"]}
            if "|" in rest:  # table row: name | age 35 | address
                cells = [c.strip() for c in rest.split("|")]
                rec["name"] = re.sub(r"\s+(s/o|w/o|d/o)\s+.*$", "", cells[0])
                sm = re.search(r"(?:s/o|w/o|d/o)\s+(.+)$", cells[0])
                if sm:
                    rec["parent_or_spouse"] = sm[1]
                for c in cells[1:]:
                    am = re.match(r"age\s+(\d+)", c)
                    if am:
                        rec["age"] = am[1]
            else:
                name = re.split(r"\s+(?:s/o|w/o|d/o|age|\(|r/o)", rest)[0]
                name = re.sub(r"^(PW|LW)-?\d+\s+", "", name).strip()
                rec["name"] = name
                sm = re.search(r"(?:s/o|w/o|d/o)\s+(.+?)(?:\s+age|\s+r/o|$)", rest)
                if sm:
                    rec["parent_or_spouse"] = sm[1]
                am = re.search(r"age\s+(\d+)", rest)
                if am:
                    rec["age"] = am[1]
            out.setdefault(section, []).append(rec)
    return out


class FakeVlm:
    model = "fake-model"

    def __init__(
        self,
        *,
        transcripts: Optional[Dict[int, str]] = None,
        fail_map_pages: Optional[Set[int]] = None,
        fail_transcribe_pages: Optional[Set[int]] = None,
        unavailable: bool = False,
        answer: Optional[Callable[[str], dict]] = None,
    ):
        self.transcripts = transcripts or {}
        self.fail_map_pages = fail_map_pages or set()
        self.fail_transcribe_pages = fail_transcribe_pages or set()
        self.unavailable = unavailable
        self.answer = answer or rule_model
        self.map_calls: List[str] = []
        self.transcribe_calls = 0
        self._transcribe_seq = 0

    def __call__(self, settings=None):  # lets an instance replace the VlmClient class
        return self

    def config_problem(self):
        return None

    def health(self):
        if self.unavailable:
            return {"enabled": True, "configured": True, "reachable": False, "model": self.model,
                    "model_available": False, "supports_images": None, "error": "server not reachable (ConnectError)"}
        return {"enabled": True, "configured": True, "reachable": True, "model": self.model,
                "model_available": True, "supports_images": True, "error": None}

    def server_busy(self):
        return False

    def transcribe_image(self, png: bytes) -> VlmReply:
        self.transcribe_calls += 1
        self._transcribe_seq += 1
        assert png[:8] == b"\x89PNG\r\n\x1a\n", "the model must receive a PNG page image"
        if self.unavailable or self._transcribe_seq in self.fail_transcribe_pages:
            raise VlmTimeout("timed out waiting for the model server after 600s (VLM_TIMEOUT_SECONDS)")
        text = self.transcripts.get(self._transcribe_seq, self.transcripts.get(0, ""))
        return VlmReply(text=text, finish_reason="stop", seconds=1.0)

    def complete_json(self, system: str, user: str, schema: dict, *, max_tokens=None) -> dict:
        self.map_calls.append(user)
        page = int(re.match(r"PAGE (\d+) TEXT", user)[1])
        if self.unavailable:
            raise VlmUnavailable("model server not reachable (ConnectError)")
        if page in self.fail_map_pages:
            raise VlmTimeout("timed out waiting for the model server after 600s (VLM_TIMEOUT_SECONDS)")
        body = user.split("\n", 1)[1].split("\n\n--\n", 1)[0]
        return self.answer(body)

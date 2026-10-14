"""Evidence-based validation of the VLM's choices, and column-wise choice-table preprocessing.

The "model answers" below are the wrong answers the real Nanonets-OCR2-3B gave for the Physical
Features section of a real report (Height, Hair, Color, Beard, Deformities): the validator must
turn each of them into a review flag and must never replace the model's value with another option.
"""

from __future__ import annotations

import json
import re

import httpx
import pytest

from app.config import get_settings
from app.ir_standard.evidence import EvidenceIndex, html_tables_to_markdown, merge_wrapped, parse_mark
from app.ir_standard.mapper import map_markdown
from app.ir_standard.schema import SECTION_BY_NUMBER, SECTIONS
from app.ir_standard.validator import build_standard
from app.ir_standard.vlm_client import VlmClient
from app.services.ir_standard_service import IrStandardService

S3 = SECTION_BY_NUMBER[3]
S4 = SECTION_BY_NUMBER[4]
S5 = SECTION_BY_NUMBER[5]
S9 = SECTION_BY_NUMBER[9]
FIELD = {f.key: f for f in S3.fields}

# Section 3 as the DOCX reader prints it for a report that mixes tick symbols, bold and written "Yes".
S3_MD = """**3.PHYSICAL FEATURES**

| **Sex**<br>**(1)** | **Built**<br>**(2)** | **Height**<br>**(3)** | **Eyes**<br>**(4)** | **Hair**<br>**(5)** | **Face**<br>**(6)** | **Color**<br>**(7)** | **Teeth**<br>**(8)** |
| --- | --- | --- | --- | --- | --- | --- | --- |
| ☑Male: YES | ☐ Lean | ☐ 5’5’’ ft | ☑Black:Yes | ☑Black: Yes | ☑Round: Rund | ☐ White | ☑regular |
| ☐ Fe-male | ☑Stout | ☐ 4.1-4.5 ft | ☐ Cat | ☐ Brown | Oval | ☐ Fair | ☐ Discolored |
|  | ☐ Stiff | ☐ 4.6-5.0 ft | ☐ Squint | ☐ White | ☐ Wrinkled | ☐ Black: Yes | ☐ Broken |
|  | ☐ Erect | 5.1-5.5 ft | ☐ One eye | ☐ Bald | ☐ Square | ☑Brown | ☐ Double |
|  | ☐ Stooping | ☑ **5.6-6.0 ft** | ☐ Others | ☐ Long | ☐ Long | ☐ Sallow | ☐ Others : Yes |
|  | ☐ Hunch<br>Back | ☐ 6.1-6.5 ft |  | ☐ Curley | ☐ Others |  |  |
|  | ☐ Normal: Yes | ☐ 6.6-7.0 ft: Yeas |  | ☐ Back combed |  |  |  |
|  |  | ☐ 7.1-7.5 ft |  | ☐ Partly combed |  |  |  |
|  |  |  |  | ☐ Mixed hairs |  |  |  |

| **Nose**<br>**(9)** | **Beard**<br>**(10)** | **Mustah**<br>**Es**<br>**(11)** | **Ear**<br>**(12)** |  |  |
| --- | --- | --- | --- | --- | --- |
| ☐ Large | Trim | ☑small | Normal☑ |  |  |
| ☑Small: Yes | ☐ Long | ☐ Turnee up |  |  |  |
| ☐ Hooked | Short: Yes | ☐ Chipped |  |  |  |
| ☐ Snub | ☐ French cut | ☐ Dropping |  |  |  |
| ☐ Projected |  | ☐ No mustaches |  |  |  |
| Identification marks<br>(13) | Deformities/Peculiarities<br>(14) | Language/Dialect (15) |  |  |  |
| **A Mole on the front side below neck** | Nil | Telugu |  |  |  |
| Burn Marks<br>(16) | Leucodema<br>(17) | Mole<br>(18) | Scar<br>(19) | Tattoo<br>(20) |  |
| Nil | Nil | **A Mole on the chest** | Nil | --- |  |
"""

# What the real model returned for it (all valid options, so the old validator raised no flag).
WRONG = {
    "sex": "Male", "built": "Stout", "height": "5.1-5.5 ft", "eyes": "Black", "hair": "Brown", "face": "Round",
    "color": "White", "teeth": "Regular", "nose": "Small", "beard": "Trim", "mustaches": "Small", "ear": "Normal",
    "identification_marks": "A Mole on the front side below neck", "deformities": "A Mole on the chest",
    "language_dialect": "Telugu", "burn_marks": "Nil", "leucoderma": "Nil", "mole": "A Mole on the chest",
    "scar": "Nil", "tattoo": "Nil",
}


def run(raw: dict, md: str = S3_MD, spec=S3, source: str = "docx_native"):
    """(validated section, flags by path) for one section of model output checked against ``md``."""
    index = EvidenceIndex(spec, md, source)
    std = build_standard({spec.key: raw}, indexes={spec.key: index})
    by_path: dict = {}
    for f in std["review_flags"]:
        if f["section"] == spec.key:  # other sections are empty in these tests (their required-field flags are noise)
            by_path.setdefault(f["path"], []).append(f)
    return std["sections"][spec.key], by_path


def codes(by_path: dict, key: str, spec=S3) -> set:
    return {f["code"] for f in by_path.get(f"{spec.key}.{key}", [])}


# ---------------------------------------------------------------------------
# 1. choice tables are restructured column by column, nothing is lost
# ---------------------------------------------------------------------------

def test_choice_tables_become_one_block_per_printed_column():
    text = EvidenceIndex(S3, S3_MD).restructured
    assert "| " not in text  # no markdown table rows left for the choice tables
    height = text.split("COLUMN Height (3)")[1].split("COLUMN Eyes")[0]
    lines = [ln.strip() for ln in height.splitlines()[1:] if ln.strip()]
    assert lines == [  # original order, box / bold / written Yes untouched
        "1. ☐ 5’5’’ ft", "2. ☐ 4.1-4.5 ft", "3. ☐ 4.6-5.0 ft", "4. 5.1-5.5 ft", "5. ☑ **5.6-6.0 ft**",
        "6. ☐ 6.1-6.5 ft", "7. ☐ 6.6-7.0 ft: Yeas", "8. ☐ 7.1-7.5 ft",
    ]
    assert re.search(r"COLUMN Height \(3\) \[t0\.b0\.c2\]:", text)  # coordinate kept on the block


def test_each_text_column_keeps_its_own_value_so_deformities_cannot_borrow_a_neighbour():
    text = EvidenceIndex(S3, S3_MD).restructured

    def block(name: str) -> str:
        return re.search(rf"COLUMN {re.escape(name)}[^\n]*:\n((?:  \d+\. [^\n]*\n?)+)", text).group(1)

    assert block("Deformities/Peculiarities (14)").split() == ["1.", "Nil"]
    assert "Mole on the chest" in block("Mole (18)")
    assert "Mole on the chest" not in block("Deformities/Peculiarities (14)")
    assert "front side below neck" in block("Identification marks (13)")
    assert block("Language/Dialect (15)").split() == ["1.", "Telugu"]


def test_wrapped_option_is_rejoined_but_separate_options_are_not():
    assert merge_wrapped(["☐ Hunch", "Back"], FIELD["built"]) == ["☐ Hunch Back"]
    assert merge_wrapped(["Alone", "☐ In Hotel", "Hut"], S4.fields[0]) == ["Alone", "☐ In Hotel", "Hut"]
    assert "☐ Hunch Back" in EvidenceIndex(S3, S3_MD).restructured


def test_marks_keep_box_bold_and_yes_as_separate_attributes_with_coordinates():
    index = EvidenceIndex(S3, S3_MD)
    by_label = {m.label: m for m in index.marks_for(FIELD["height"])}
    ticked = by_label["5.6-6.0 ft"]
    assert (ticked.box, ticked.bold, ticked.yes) == ("ticked", True, False) and ticked.state == "selected"
    yes_only = by_label["6.6-7.0 ft"]
    assert (yes_only.box, yes_only.bold, yes_only.yes) == ("empty", False, True) and yes_only.state == "uncertain"
    plain = by_label["5.1-5.5 ft"]
    assert (plain.box, plain.bold, plain.yes) == ("none", False, False) and plain.state == "unselected"
    assert ticked.at.startswith("t0.b0.c2.")
    hair_black = next(m for m in index.marks_for(FIELD["hair"]) if m.label == "Black")
    assert (hair_black.box, hair_black.yes, hair_black.bold) == ("ticked", True, False)
    bold_only = parse_mark("☐ **Farmer**")
    assert (bold_only.box, bold_only.bold, bold_only.state) == ("empty", True, "uncertain")  # bold != selected
    assert parse_mark("**☑** No").bold is False  # a bold tick symbol is not bold text
    assert parse_mark("Yes").yes is False and parse_mark("Daily Labour -yes").yes is True


# ---------------------------------------------------------------------------
# 2. the known Physical Features errors are now flagged, never silently replaced
# ---------------------------------------------------------------------------

def test_height_picked_without_any_mark_is_unsupported_and_names_the_marked_option():
    sec, flags = run(WRONG)
    h = sec["height"]
    assert h["selected"] is None  # not marked in the report: left empty, and NOT swapped for another option
    assert h["verification"]["model_answer"] == ["5.1-5.5 ft"]  # what the model said stays for the reviewer
    assert h["verification"]["chosen"][0]["state"] == "unselected"  # no box, not bold, no Yes
    # the report ticks 5.6-6.0 and also writes "Yeas" beside 6.6-7.0: both are shown, neither is applied
    assert [(c["option"], c["state"]) for c in h["verification"]["candidates"]] == [
        ("5.6-6.0 ft", "selected"), ("6.6-7.0 ft", "uncertain")]
    assert h["verification"]["status"] in ("unsupported", "conflicting")
    assert {"unsupported_selection", "conflicting_evidence"} <= codes(flags, "height")
    assert "5.6-6.0 ft" in flags["s03_physical_features.height"][0]["detail"]


def test_hair_picked_with_an_empty_box_is_unsupported():
    sec, flags = run(WRONG)
    assert sec["hair"]["selected"] is None and sec["hair"]["verification"]["model_answer"] == ["Brown"]
    assert sec["hair"]["verification"]["status"] == "unsupported"
    assert [c["option"] for c in sec["hair"]["verification"]["candidates"]] == ["Black"]
    assert "unsupported_selection" in codes(flags, "hair")


@pytest.mark.parametrize("pick", ["White", "Brown", "Black"])
def test_color_is_ambiguous_and_flagged_whichever_option_the_model_picks(pick):
    """Source: ☐ Black: Yes (written Yes, box empty) and ☑ Brown. Two signals disagree -> a human decides."""
    sec, flags = run({**WRONG, "color": pick})
    # an unmarked pick is left empty; a ticked or weakly marked one is kept but flagged. Never resolved for the reviewer.
    assert sec["color"]["selected"] == (None if pick == "White" else pick)
    assert sec["color"]["verification"]["status"] in ("conflicting", "unsupported")
    assert codes(flags, "color") & {"conflicting_evidence", "unsupported_selection"}
    states = {c["option"]: c["state"] for c in sec["color"]["verification"]["candidates"]}
    assert states == {"Brown": "selected", "Black": "uncertain"}  # both candidates shown to the reviewer


def test_color_picking_the_ticked_option_still_reports_the_conflicting_yes():
    sec, flags = run({**WRONG, "color": "Brown"})
    assert sec["color"]["verification"]["status"] == "conflicting"
    assert "conflicting_evidence" in codes(flags, "color")


def test_color_picking_the_yes_option_is_weak_evidence_and_conflicting():
    sec, flags = run({**WRONG, "color": "Black"})
    assert {"weak_evidence", "conflicting_evidence"} <= codes(flags, "color")
    assert sec["color"]["verification"]["chosen"][0]["state"] == "uncertain"


def test_beard_trim_has_no_mark_and_short_is_only_a_written_yes():
    sec, flags = run(WRONG)  # the model said Trim
    assert sec["beard"]["selected"] is None and sec["beard"]["verification"]["model_answer"] == ["Trim"]
    assert sec["beard"]["verification"]["status"] == "unsupported"
    cand = sec["beard"]["verification"]["candidates"]
    assert [(c["option"], c["state"], c["yes"]) for c in cand] == [("Short", "uncertain", True)]
    sec2, flags2 = run({**WRONG, "beard": "Short"})
    assert sec2["beard"]["selected"] == "Short"
    assert sec2["beard"]["verification"]["status"] == "uncertain"  # a written Yes alone is not a ticked box
    assert "weak_evidence" in codes(flags2, "beard")


def test_deformities_taken_from_the_mole_column_is_flagged_as_not_in_its_own_column():
    sec, flags = run(WRONG)
    # the model took the Mole column's text; the Deformities column prints "Nil": the report's own cell is used, and flagged
    assert sec["deformities"] == "Nil"
    assert "filled_from_report_text" in codes(flags, "deformities")
    detail = flags["s03_physical_features.deformities"][0]["detail"]
    assert "'Nil'" in detail and "A Mole on the chest" in detail  # both shown, so the reviewer sees what changed
    _, flags_ok = run({**WRONG, "deformities": "Nil"})
    assert "deformities" not in {p.split(".")[-1] for p in flags_ok}


def test_correct_choices_are_supported_and_raise_no_flag():
    right = {**WRONG, "height": "5.6-6.0 ft", "hair": "Black", "color": "Brown", "beard": "Short", "deformities": "Nil"}
    sec, flags = run(right)
    for key in ("sex", "eyes", "face", "hair", "nose", "mustaches"):
        assert sec[key]["verification"]["status"] == "supported", key
        assert not flags.get(f"s03_physical_features.{key}"), key
    # the report itself writes stray "Yes" notes beside unticked options of these fields: the chosen
    # option is ticked, but the conflicting mark is surfaced for a human instead of being ignored
    for key in ("built", "height", "teeth"):
        assert sec[key]["verification"]["chosen"][0]["state"] == "selected", key
        assert sec[key]["verification"]["status"] == "conflicting", key
        assert codes(flags, key) == {"conflicting_evidence"}, key
    assert sec["height"]["verification"]["chosen"][0]["bold"] is True  # bold kept as evidence, not as the reason


def test_only_the_wrong_fields_of_the_real_run_are_flagged():
    _, flags = run(WRONG)
    flagged = {p.split(".")[-1] for p in flags}
    assert {"height", "hair", "color", "beard", "deformities"} <= flagged
    # built / teeth are flagged only because the report writes "Yes" beside an unticked option
    assert not flagged & {"sex", "eyes", "face", "nose", "mustaches", "ear", "language_dialect", "tattoo"}


# ---------------------------------------------------------------------------
# 3. other evidence rules
# ---------------------------------------------------------------------------

OCC_MD = """**4.SOCIO / ECONOMIC PROFILE**

|  | Occupation | ☐ Govt Job<br>☐ Student | ☐ Pvt Job<br>☐ **Farmer** | ☐ Finance |
| --- | --- | --- | --- | --- |
"""


def test_a_sole_bold_option_with_an_empty_box_is_the_selection():
    """Many reports mark the choice by bold alone: with nothing ticked, one bold option is the selection."""
    sec, flags = run({"occupation": {"selected": "Farmer", "evidence": "bold"}}, OCC_MD, S4)
    v = sec["occupation"]["verification"]
    assert sec["occupation"]["selected"] == "Farmer"
    assert v["status"] == "supported" and v["chosen"][0]["state"] == "selected" and v["chosen"][0]["via"] == "bold"
    assert v["chosen"][0]["box"] == "empty" and v["chosen"][0]["bold"] is True  # the evidence stays visible
    assert not codes(flags, "occupation", S4)


def test_bold_that_is_not_unambiguous_is_still_flagged_not_assumed():
    two_bold = OCC_MD.replace("☐ Govt Job", "☐ **Govt Job**")
    sec, flags = run({"occupation": {"selected": "Farmer", "evidence": "bold"}}, two_bold, S4)
    assert sec["occupation"]["verification"]["status"] in ("conflicting", "uncertain")
    assert codes(flags, "occupation", S4) & {"conflicting_evidence", "weak_evidence"}
    ticked_elsewhere = OCC_MD.replace("☐ Student", "☑ Student")
    sec2, flags2 = run({"occupation": {"selected": "Farmer", "evidence": "bold"}}, ticked_elsewhere, S4)
    # a ticked box elsewhere: bold does not override it. The model's pick is kept but flagged, never resolved.
    assert sec2["occupation"]["selected"] == "Farmer"
    assert sec2["occupation"]["verification"]["status"] == "conflicting"
    assert {c["option"]: c["state"] for c in sec2["occupation"]["verification"]["candidates"]} == {
        "Student": "selected", "Farmer": "uncertain"}
    assert codes(flags2, "occupation", S4) & {"conflicting_evidence", "weak_evidence"}


def test_a_bold_option_for_an_any_of_field_and_a_written_yes_alone():
    md = "**9.REGULAR HABITS**\n\n| ☐ **Liquor** | ☐ Toddy | ☐ **Smoking** | ☐ Gambling |\n| --- | --- | --- | --- |\n"
    sec, flags = run({"habits": ["Liquor", "Smoking"]}, md, S9)
    assert sec["habits"]["selected"] == ["Liquor", "Smoking"] and sec["habits"]["verification"]["status"] == "supported"
    yes_only = "**4.X**\n\n|  | Marital Status | ☐ Married: Yes | ☐ Unmarried |\n| --- | --- | --- | --- |\n"
    sec2, flags2 = run({"marital_status": "Married"}, yes_only, S4)
    assert sec2["marital_status"]["verification"]["status"] == "uncertain"  # a written Yes alone is still weak
    assert "weak_evidence" in codes(flags2, "marital_status", S4)


def test_model_claiming_a_tick_where_the_report_only_has_bold_is_flagged():
    md = "**4.SOCIO**\n\n|  | Occupation | ☐ Govt Job | ☐ **Farmer** |\n| --- | --- | --- | --- |\n"
    _, flags = run({"occupation": {"selected": "Farmer", "evidence": "tick"}}, md, S4)
    assert "evidence_mismatch" in codes(flags, "occupation", S4)


def test_two_ticks_in_a_single_choice_field_are_flagged_even_if_the_model_returned_one():
    md = "**4.X**\n\n|  | Marital Status | ☑ Married | ☑ Unmarried |\n| --- | --- | --- | --- |\n"
    sec, flags = run({"marital_status": "Married"}, md, S4)
    assert sec["marital_status"]["selected"] == "Married"
    assert "multiple_selected" in codes(flags, "marital_status", S4)
    assert sec["marital_status"]["verification"]["status"] == "conflicting"


def test_vlm_returning_two_options_for_a_single_field_keeps_the_first_and_flags():
    _, flags = run({**WRONG, "face": ["Round", "Oval"]})
    assert "multiple_selected" in codes(flags, "face")


def test_ticked_option_the_model_left_out_is_flagged_not_filled_in():
    sec, flags = run({**WRONG, "sex": None})
    assert sec["sex"]["selected"] is None  # not filled in for the reviewer
    assert sec["sex"]["verification"]["status"] == "missed"
    assert "missed_selection" in codes(flags, "sex")
    assert [c["option"] for c in sec["sex"]["verification"]["candidates"]] == ["Male"]


def test_multiple_choice_field_flags_a_ticked_option_the_model_missed():
    md = "**9.REGULAR HABITS**\n\n| ☑ Liquor | ☐ Toddy | ☑ Smoking | ☐ Gambling |\n| --- | --- | --- | --- |\n"
    sec, flags = run({"habits": ["Liquor"]}, md, S9)
    assert sec["habits"]["selected"] == ["Liquor"]
    assert "missed_selection" in codes(flags, "habits", S9)
    sec_ok, flags_ok = run({"habits": ["Liquor", "Smoking"]}, md, S9)
    assert sec_ok["habits"]["verification"]["status"] == "supported" and not flags_ok


def test_nothing_marked_and_nothing_chosen_is_empty_not_flagged():
    sec, flags = run({**WRONG, "sex": None}, S3_MD.replace("☑Male: YES", "☐ Male"))
    assert sec["sex"]["verification"]["status"] == "empty"
    assert "sex" not in {p.split(".")[-1] for p in flags}


def test_empty_text_field_with_one_clean_value_in_the_report_is_filled_from_it_and_flagged():
    sec, flags = run({**WRONG, "identification_marks": None})
    assert sec["identification_marks"] == "A Mole on the front side below neck"
    assert "filled_from_report_text" in codes(flags, "identification_marks")
    assert "the model returned nothing" in flags["s03_physical_features.identification_marks"][0]["detail"]


def test_a_printed_nil_is_never_copied_into_an_empty_field():
    sec, flags = run({**WRONG, "deformities": None})  # the report prints "Nil" for Deformities
    assert sec["deformities"] is None and not codes(flags, "deformities") - {"missed_value"}


def test_the_row_label_returned_as_the_value_is_replaced_by_the_value_printed_beside_it():
    s2 = SECTION_BY_NUMBER[2]
    md = ("2.UNIQUE IDENTIFIERS\n\n| Sl<br>No. | Identification type |  |\n| --- | --- | --- |\n"
          "| 1. | Mobile number | 8008948701 |\n| 2. | Email IDs |  |\n| 4. | Aadhar Number | 6203 6054 7497 |\n")
    index = EvidenceIndex(s2, md, "docx_native")
    std = build_standard({s2.key: {"mobile_number": "Mobile number", "email_ids": "Email IDs",
                                   "aadhaar_number": "Aadhar Number"}}, indexes={s2.key: index})
    sec = std["sections"][s2.key]
    assert sec["mobile_number"] == "8008948701" and sec["aadhaar_number"] == "6203 6054 7497"
    assert sec["email_ids"] is None  # the report's cell is blank: the echoed label is dropped, nothing is invented
    codes_by = {f["path"].split(".")[-1]: f["code"] for f in std["review_flags"] if f["section"] == s2.key}
    assert codes_by["mobile_number"] == "filled_from_report_text"
    assert codes_by["email_ids"] == "label_echo_removed"
    filled = next(f for f in std["review_flags"] if f["path"].endswith("aadhaar_number") and f["code"] == "filled_from_report_text")
    assert "6203 6054 7497" in filled["detail"]


def test_without_an_index_nothing_changes_for_existing_callers():
    std = build_standard({S3.key: WRONG})
    assert "verification" not in std["sections"][S3.key]["height"]
    assert not [f for f in std["review_flags"] if f["code"] in ("unsupported_selection", "text_not_in_source")]


# ---------------------------------------------------------------------------
# 4. reports are not always tables
# ---------------------------------------------------------------------------

def test_inline_options_on_a_label_line():
    md = "**3.PHYSICAL FEATURES**\nSex: ☑ Male   ☐ Female\nBuilt: ☐ Lean  ☑ **Stout**\n"
    sec, _ = run({"sex": "Male", "built": "Lean"}, md)
    assert sec["sex"]["verification"]["status"] == "supported"
    assert sec["built"]["verification"]["status"] == "unsupported"  # Lean has an empty box
    assert [c["option"] for c in sec["built"]["verification"]["candidates"]] == ["Stout"]
    sec2, _ = run({"sex": "Female"}, md)
    assert sec2["sex"]["verification"]["status"] == "unsupported" and sec2["sex"]["selected"] is None  # not ticked: left empty


def test_one_option_per_line_under_a_label():
    md = "**3.PHYSICAL FEATURES**\n\nHeight (3)\n☐ 4.1-4.5 ft\n☑ 5.6-6.0 ft\n☐ 6.1-6.5 ft\n\nHair:\n☐ Black\n☑ Brown\n"
    sec, _ = run({"height": "5.6-6.0 ft", "hair": "Black"}, md)
    assert sec["height"]["verification"]["status"] == "supported"
    assert sec["hair"]["verification"]["status"] == "unsupported"
    assert [c["option"] for c in sec["hair"]["verification"]["candidates"]] == ["Brown"]


def test_label_colon_value_lines_check_free_text_and_choices():
    md = "**3.PHYSICAL FEATURES**\nEar: Normal\nLanguage/Dialect: Telugu\nDeformities/Peculiarities: Nil\nSex: Male\n"
    sec, flags = run({"ear": "Normal", "language_dialect": "Hindi", "deformities": "Nil", "sex": "Male"}, md)
    assert {"ungrounded_value_removed", "filled_from_report_text"} <= codes(flags, "language_dialect")  # "Hindi" is not in the report
    assert sec["language_dialect"] == "Telugu"  # the report prints exactly one value for it: that is used
    assert not codes(flags, "ear") and not codes(flags, "deformities")
    # "Sex: Male" is a written answer, not an option list: taken from the report's own text
    assert sec["sex"]["selected"] == "Male" and sec["sex"]["evidence"] == "written"
    assert sec["sex"]["verification"]["status"] == "written" and not codes(flags, "sex")


def test_option_in_a_sentence_marked_bold_is_the_selection_and_the_unmarked_one_is_not():
    md = "**5.FAMILY HISTORY**\n\nDoes the family members stay together **Yes** No\n"
    sec, flags = run({"family_stay_together": "Yes"}, md, S5)
    assert sec["family_stay_together"]["selected"] == "Yes"
    assert sec["family_stay_together"]["verification"]["status"] == "supported"
    assert not codes(flags, "family_stay_together", S5)
    sec2, flags2 = run({"family_stay_together": "No"}, md, S5)  # "No" carries no mark: not assumed
    assert sec2["family_stay_together"]["selected"] is None
    assert "unsupported_selection" in codes(flags2, "family_stay_together", S5)


def test_prose_without_marks_cannot_be_verified_and_is_flagged():
    md = "**3.PHYSICAL FEATURES**\n\nThe accused is a stout man with black eyes and a round face.\n"
    sec, flags = run({"built": "Stout"}, md)
    assert sec["built"]["selected"] is None  # the report has no option / mark for it: not assumed
    assert sec["built"]["verification"]["model_answer"] == ["Stout"]
    assert sec["built"]["verification"]["status"] == "unverified"
    assert "evidence_unverified" in codes(flags, "built")


def test_html_tables_from_the_vlm_are_read_like_markdown_tables():
    html = ("**3.PHYSICAL FEATURES**\n<table><tr><th>Sex<br>(1)</th><th>Built<br>(2)</th></tr>"
            "<tr><td>☑ Male<br>☐ Female</td><td>☐ Lean<br>☑ Stout</td></tr></table>")
    md = html_tables_to_markdown(html)
    assert "<table" not in md and "| Sex<br>(1) | Built<br>(2) |" in md
    sec, _ = run({"sex": "Male", "built": "Stout"}, md)
    assert sec["sex"]["verification"]["status"] == "supported"
    assert sec["built"]["verification"]["status"] == "supported"
    assert "COLUMN Sex (1)" in EvidenceIndex(S3, md).restructured


def test_scan_evidence_is_labelled_as_coming_from_the_vlm_transcription():
    sec, _ = run({**WRONG, "sex": "Male"}, source="vlm_transcription")
    assert sec["sex"]["verification"]["source"] == "vlm_transcription"


# ---------------------------------------------------------------------------
# 5. end to end through the mapper with the model's real wrong answers replayed
# ---------------------------------------------------------------------------

def _replay_server(prompts: list):
    def handler(request: httpx.Request) -> httpx.Response:
        content = json.loads(request.content)["messages"][-1]["content"]
        prompts.append(content)
        answer = WRONG if "SECTION: 3." in content else {}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(answer)}}]})

    return httpx.MockTransport(handler)


def test_mapper_sends_column_blocks_and_flags_the_replayed_wrong_answers(monkeypatch):
    monkeypatch.setenv("LLM_ENABLED", "true")
    monkeypatch.setenv("LLM_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("LLM_MODEL", "fake")
    get_settings.cache_clear()
    prompts: list = []
    client = VlmClient(get_settings(), transport=_replay_server(prompts))
    std = map_markdown(S3_MD, client, evidence_source="docx_native")

    prompt = next(p for p in prompts if "SECTION: 3." in p)
    assert "COLUMN Height (3)" in prompt and "5. ☑ **5.6-6.0 ft**" in prompt
    assert "Bold text or a written 'Yes'" in prompt  # the rule no longer says bold means selected

    phys = std["sections"]["s03_physical_features"]
    assert phys["height"]["selected"] is None  # the unmarked 5.1-5.5 pick is left empty, not kept or swapped
    flagged = {f["path"].split(".")[-1]: f["code"] for f in std["review_flags"] if f["section"] == S3.key}
    assert {"height", "hair", "color", "beard", "deformities"} <= set(flagged)
    assert phys["height"]["verification"]["source"] == "docx_native"


def test_notes_shown_to_the_reviewer_include_every_flag_of_a_field():
    sec, flags = run({**WRONG, "color": "Black"})
    std = {
        "schema_version": "x",
        "sections": {s.key: (sec if s.key == S3.key else {}) for s in SECTIONS},
        "review_flags": [f for fl in flags.values() for f in fl],
    }
    fields = {f.id: f for f in IrStandardService.fields_from_standard(std)}
    note = fields["s03_physical_features.color"].notes
    assert "marked only by" in note and "marks several options" in note
    assert fields["s03_physical_features.color"].review_status.value == "needs_review"

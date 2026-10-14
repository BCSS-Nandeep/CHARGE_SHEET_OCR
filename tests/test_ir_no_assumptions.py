"""The pipeline must not assume or invent: what the report does not contain stays empty.

Layouts below are synthetic copies of what a real report with a different template looked like:
section 3 without the options grid, section 4 written as plain text, a blank associates table.
The "model answers" are the fabrications the real model produced for it.
"""

from __future__ import annotations

import json

from app.ir_standard.evidence import EvidenceIndex
from app.ir_standard.mapper import RULES, build_prompt, section_template
from app.ir_standard.schema import SECTION_BY_NUMBER, SECTIONS
from app.ir_standard.validator import build_standard

S1, S3, S4, S6, S18 = (SECTION_BY_NUMBER[n] for n in (1, 3, 4, 6, 18))

S3_NO_GRID = """3. PHYSICAL FEATURES

| Identification marks | A mole on the left side neck |
| --- | --- |
| Deformities/Peculiarities | -- |
| Language/Dialect | Telugu |
| Mole | A mole on the stomach |
"""

S4_WRITTEN = """4. SOCIO/ECONOMIC PROFILE

| 1. | Living status | Living with parents |
| --- | --- | --- |
| 2. | Marital status | Un married |
| 3. | Education details | Inter second year fail |
| 4. | Occupation | Mechanic in electric auto |
| 5. | Income group | Middle income group |
"""

S6_BLANK = """7. RELATIONSHIP WITH OTHER ASSOCIATES/GANG MEMBERS AND FRIENDS

| **S. No.** | **Gang** | **Member name** | **Address** | **Mobile No** | **Relationship** |
| --- | --- | --- | --- | --- | --- |
|  |  |  |  |  |  |
|  |  |  |  |  |  |
"""

S18_DASHES = """2. REGULARISATION OF PT WARRANT/NBW
A) Regularization of PT Warrant

| S. No. | Dist./Div. | PS | Cr. No. | Sec. of Law | SC/CC No. | Court | Date of regularization of PT warrant | Remarks |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1. | -- | -- | -- | -- | -- | -- | -- | -- |
"""


def run(spec, md, raw):
    index = EvidenceIndex(spec, md, "docx_native")
    std = build_standard({spec.key: raw}, indexes={spec.key: index})
    flags = [f for f in std["review_flags"] if f["section"] == spec.key]
    return std["sections"][spec.key], flags


def codes(flags, key=None):
    return {f["code"] for f in flags if key is None or f["path"].endswith(key)}


# ---- a field the report does not have ---------------------------------------------------------

def test_options_grid_missing_from_the_report_gives_empty_fields_not_guesses():
    fabricated = {"sex": "Female", "built": "Normal", "height": "5.1-5.5 ft", "eyes": "Black", "hair": "Brown",
                  "face": "Oval", "color": "Fair", "teeth": "Regular", "nose": "Small", "beard": "Short",
                  "mustaches": "Small"}
    real = {"language_dialect": "Telugu", "mole": "A mole on the stomach"}
    sec, flags = run(S3, S3_NO_GRID, {**fabricated, **real})
    for key in fabricated:
        assert sec[key]["selected"] is None and sec[key]["other"] is None, key
        assert sec[key]["verification"]["status"] == "unverified", key
        assert sec[key]["verification"]["model_answer"] == [fabricated[key]], key  # kept for audit only
        assert "evidence_unverified" in codes(flags, key), key
    assert sec["language_dialect"] == "Telugu" and sec["mole"] == "A mole on the stomach"  # real text is kept


def test_text_values_that_are_not_in_the_report_are_removed_and_flagged():
    sec, flags = run(S3, S3_NO_GRID, {"language_dialect": "Telugu", "tattoo": "Dragon on the arm",
                                      "scar": "Left cheek", "ear": "Normal"})
    assert sec["language_dialect"] == "Telugu"
    assert sec["tattoo"] is None and sec["scar"] is None and sec["ear"] is None
    assert codes(flags, "tattoo") == {"ungrounded_value_removed"}
    assert "Dragon" in next(f for f in flags if f["path"].endswith("tattoo"))["detail"]  # so a human can see what was dropped


def test_placeholder_text_copied_from_the_template_is_never_kept():
    sec, flags = run(S3, S3_NO_GRID, {"identification_marks": "<text or null>", "scar": "<one of: Male | Female>"})
    assert sec["scar"] is None  # the report has no Scar value: nothing to use
    assert "<" not in str(sec["identification_marks"])  # the placeholder is never kept ...
    assert sec["identification_marks"] == "A mole on the left side neck"  # ... the report's own value is used
    assert {"ungrounded_value_removed", "filled_from_report_text"} <= codes(flags, "identification_marks")


# ---- tables -----------------------------------------------------------------------------------

def test_invented_rows_for_a_blank_table_are_dropped():
    invented = [
        {"gang": "Gang A", "member_name": "Member 1", "address": "123 Main Street, City, State, ZIP",
         "mobile_no": "555-555-5555", "relationship": "Close friend"},
        {"gang": "Gang B", "member_name": "Member 2", "address": "456 Elm Street", "mobile_no": "666-666-6666",
         "relationship": "Co-worker"},
    ]
    sec, flags = run(S6, S6_BLANK, {"associates": invented})
    # a column titled "Member name" does not make "Member 1" real: the table stays, as one blank row
    assert len(sec["associates"]) == 1 and all(v is None for v in sec["associates"][0].values())
    assert codes(flags) >= {"ungrounded_rows_removed"}
    assert not [f for f in flags if f["code"] == "ungrounded_value_removed"]  # one summary, not a flag per cell


def test_placeholders_in_a_table_with_only_dashes_are_dropped():
    row = {k: "<text or null>" for k in ("dist_division", "ps", "cr_no", "section_of_law", "sc_cc_no", "court",
                                         "date_of_regularization", "remarks")}
    sec, flags = run(S18, S18_DASHES, {"pt_warrants": [row], "nbw_execution": [row]})
    for key in ("pt_warrants", "nbw_execution"):  # kept in the template, blank
        assert len(sec[key]) == 1 and all(v is None for v in sec[key][0].values()), key


def test_real_table_rows_survive_while_invented_cells_in_them_are_emptied():
    md = ("7. RELATIONSHIP WITH OTHER ASSOCIATES\n\n| S. No. | Gang | Member name | Address | Mobile No | Relationship |\n"
          "| --- | --- | --- | --- | --- | --- |\n| 1 | Alpha | Ravi Kumar | Main Road | 9876543210 | friend |\n")
    rows = [{"gang": "Alpha", "member_name": "Ravi Kumar", "address": "Main Road", "mobile_no": "9876543210",
             "relationship": "friend"},
            {"gang": "Beta", "member_name": "Somebody Else", "mobile_no": "1111111111"}]
    sec, flags = run(S6, md, {"associates": rows})
    assert len(sec["associates"]) == 1 and sec["associates"][0]["member_name"] == "Ravi Kumar"
    assert "ungrounded_rows_removed" in codes(flags)


# ---- a field the report writes as text, not as an option list -------------------------------------

def test_written_answers_are_used_as_written_and_no_option_is_assumed():
    model = {"living_status": "With Family", "marital_status": "Unmarried", "education": "Intermediate",
             "occupation": "Pvt Job", "income_group": "Middle income Rs.5000 to Rs.10000"}
    sec, flags = run(S4, S4_WRITTEN, model)
    # "Un married" names a template option: accepted as that option
    assert sec["marital_status"]["selected"] == "Unmarried" and sec["marital_status"]["evidence"] == "written"
    assert not codes(flags, "marital_status")
    # the other four are plain text that names no option: kept as written, nothing assumed
    for key, text in (("living_status", "Living with parents"), ("occupation", "Mechanic in electric auto"),
                      ("income_group", "Middle income group"), ("education", "Inter second year fail")):
        assert sec[key]["selected"] is None and sec[key]["other"] == text, key
        assert {"written_value_not_an_option", "choice_not_in_source"} <= codes(flags, key), key
        assert sec[key]["verification"]["status"] == "written"
        assert sec[key]["verification"]["model_answer"] in (["With Family"], ["Pvt Job"], ["Intermediate"],
                                                            ["Middle income Rs.5000 to Rs.10000"])


def test_written_answer_that_the_model_got_right_raises_no_conflict_flag():
    sec, flags = run(S4, S4_WRITTEN, {"marital_status": "Unmarried"})
    assert sec["marital_status"]["selected"] == "Unmarried" and "choice_not_in_source" not in codes(flags)


def test_blank_written_answer_stays_empty_even_if_the_model_filled_it():
    md = "4. SOCIO/ECONOMIC PROFILE\n\n| 1. | Marital status | -- |\n| --- | --- | --- |\n"
    sec, flags = run(S4, md, {"marital_status": "Married"})
    assert sec["marital_status"]["selected"] is None and sec["marital_status"]["other"] is None
    assert "choice_not_in_source" in codes(flags, "marital_status")


# ---- the prompt gives nothing to copy ----------------------------------------------------------------

def test_prompt_lists_fields_as_plain_text_with_nothing_to_copy():
    for spec in SECTIONS:
        if not spec.number:
            continue
        assert "<" not in json.dumps(section_template(spec)), spec.key  # no "<text or null>" placeholders anywhere
        prompt = build_prompt(spec, "x")
        assert "FIELDS (key: label [kind])" in prompt and "TEMPLATE" not in prompt, spec.key
        assert "<text or null>" not in prompt and "<one of" not in prompt, spec.key
    prompt = build_prompt(S3, "x")
    assert "- sex: Sex [choice]" in prompt and "- ear: Ear [text]" in prompt
    assert "- built (one of): Lean | Stout" in prompt and "Hunch Back" in prompt
    assert "ONLY the fields" in RULES and "Leave out every other key" in RULES
    assert "NEVER make up, guess or complete anything" in RULES
    assert "leave the key out" in RULES  # a blank table is left out, not filled with sample rows

def test_grounding_tolerates_a_scans_small_misreading_but_not_new_words():
    md = "1. INDIVIDUAL PARTICULARS\n\n| Father | Narayana Reddy |\n| --- | --- |\n"
    index = EvidenceIndex(S1, md, "vlm_transcription")
    assert index.is_grounded("Narayana Reddy")
    assert index.is_grounded("Narayana Readdy")  # one-letter OCR slip in a long word
    assert not index.is_grounded("Narayana Raju")  # a different word is not a slip
    assert not index.is_grounded("Narayana Reddy Junior")
    assert not index.is_grounded("<text or null>")


# ---- sections and sub-headings for the UI ---------------------------------------------------------

def test_fields_carry_their_section_title_and_sub_heading():
    from app.ir_standard.paths import flatten
    from app.ir_standard.validator import blank_standard

    std = blank_standard()
    std["sections"]["s05_family_history"]["members"][0]["name"] = "x"
    flat = {f.id: f for f in flatten(std)}
    sex, tattoo = flat["s03_physical_features.sex"], flat["s03_physical_features.tattoo"]
    assert sex.section_title == "3. PHYSICAL FEATURES" and sex.group.startswith("Build and features")
    assert tattoo.section_title == "3. PHYSICAL FEATURES" and tattoo.group.startswith("Identification marks")
    member = flat["s05_family_history.members.0.name"]
    assert member.section_title == "5. FAMILY HISTORY" and member.group == "Family members"
    assert flat["s01_individual_particulars.age"].group is None  # plain sections: title, then fields
    order = [f.section_title for f in flatten(std)]
    assert order == sorted(order, key=order.index)  # fields of a section are contiguous, in template order


def test_results_api_exposes_the_section_title_and_group_of_each_field():
    from app.schemas.charge_sheet import PdfField

    f = PdfField(id="s03_physical_features.sex", page=3, label="Sex", section_title="3. PHYSICAL FEATURES",
                 group="Build and features (columns 1\u201312)")
    assert f.model_dump()["section_title"] == "3. PHYSICAL FEATURES" and f.model_dump()["group"].startswith("Build")
    assert PdfField(id="p1-01", page=1).section_title is None  # existing (PDF) results are unchanged

"""Grounding, status rules, cross-page merge and validation of the structured result.

The model is faked: these tests exercise the deterministic Python around it.
All names and numbers are invented.
"""

from __future__ import annotations

import pytest

from app.config import get_settings
from app.schemas.structured import (
    SECTION_BY_KEY,
    SECTIONS,
    DocumentStructuredStatus,
    PageStructuredInfo,
    ValueStatus,
    blank_structured,
)
from app.services.structured_mapper import (
    StructuredMapper,
    build_json_schema,
    build_system_prompt,
    ground_value,
    merge_observations,
    observe_page,
)
from app.services.structured_validation import validate_structured
from app.services.vlm_client import VlmBadResponse, VlmTimeout, VlmUnavailable

PAGE1 = """CHARGE SHEET
District | Coastal District
Police Station | Harbour PS
FIR No. | 901/2021
Date of FIR | 18/08/2021
Crime No | N/A
Court | Special Court"""

PAGE2 = """Particulars of the Accused
1 | Manoj Pillai s/o Raman | age 35 | Harbour Road
2 | Priya Menon w/o Suresh | age 29 | Temple Street"""

PAGE3 = """List of Witnesses
1 | Arjun Nair | age 41
Accused
1 | Manoj Pillai | absconding"""


def obs(raw, text, page=1, engine="pdf_text", low=False):
    return observe_page(raw, text, page=page, text_engine=engine, low_quality=low)


def info(page, **kw):
    return PageStructuredInfo(page=page, **kw)


# ---------------------------------------------------------------- grounding / status


def test_value_found_on_page_is_extracted_with_real_source_text():
    o = obs({"case": {"police_station": "Harbour PS", "fir_number": "901/2021"}}, PAGE1)
    got = {(s, k): v for s, k, v in o.single}
    ps = got[("case", "police_station")]
    assert ps.status == ValueStatus.EXTRACTED and not ps.needs_review
    assert ps.source_text == "Police Station | Harbour PS"  # a real line of the page, not model text
    assert got[("case", "fir_number")].source_text == "FIR No. | 901/2021"


def test_value_not_on_page_is_unverified_and_kept():
    o = obs({"case": {"district": "Imaginary District"}}, PAGE1)
    v = o.single[0][2]
    assert v.status == ValueStatus.UNVERIFIED and v.needs_review and v.source_text is None
    assert v.value == "Imaginary District"  # not discarded


def test_explicit_na_is_distinct_from_absent():
    o = obs({"case": {"crime_number": "N/A"}}, PAGE1)
    assert o.single[0][2].status == ValueStatus.EXPLICIT_NA and not o.single[0][2].needs_review
    data = merge_observations([o], page_infos=[info(1)])
    assert data.single["case"]["crime_number"].status == ValueStatus.EXPLICIT_NA
    assert data.single["case"]["fir_date"].status == ValueStatus.NOT_FOUND  # never stated


def test_na_is_explicit_only_next_to_its_own_field_label():
    # PAGE1 says "Crime No | N/A": that answers crime_number, not jurisdiction
    o = obs({"case": {"jurisdiction": "N/A"}}, PAGE1)
    assert o.single[0][2].status == ValueStatus.AMBIGUOUS and o.single[0][2].needs_review
    # and an N/A the page does not contain at all is only the model saying "nothing here": ignored
    o2 = obs({"case": {"jurisdiction": "N/A"}}, "Police Station | Harbour PS")
    assert o2.single == [] and o2.dropped == 1


def test_fir_number_under_a_crime_label_is_ambiguous_not_silently_accepted():
    page = "Crime No | 55/2022\nPolice Station | Hill View"
    o = obs({"case": {"fir_number": "55/2022", "crime_number": "55/2022"}}, page)
    got = {k: v for _, k, v in o.single}
    assert got["fir_number"].status == ValueStatus.AMBIGUOUS and got["fir_number"].needs_review
    assert got["crime_number"].status == ValueStatus.EXTRACTED


def test_header_row_label_above_the_value_row_counts_as_label():
    page = "FIR No | Year | Date\n55/2022 | 2022 | 12/05/2022"
    o = obs({"case": {"fir_number": "55/2022", "fir_date": "12/05/2022"}}, page)
    assert {v.status for _, _, v in o.single} == {ValueStatus.EXTRACTED}


def test_illegible_marker_is_flagged():
    o = obs({"case": {"district": "Coas??? District"}}, "District | Coas??? District")
    assert o.single[0][2].status == ValueStatus.ILLEGIBLE and o.single[0][2].needs_review


def test_low_quality_page_forces_review_but_keeps_status_extracted():
    o = obs({"case": {"police_station": "Harbour PS"}}, PAGE1, low=True)
    v = o.single[0][2]
    assert v.status == ValueStatus.EXTRACTED and v.needs_review and "reduced quality" in v.note


def test_short_values_need_word_boundaries():
    assert ground_value("35", ["born 2035 in town"]) is None
    assert ground_value("35", ["age 35 years"]) is not None


def test_value_wrapped_over_lines_is_grounded():
    hit = ground_value("12 Temple Street Harbour Town", ["Address: 12 Temple Street", "Harbour Town"])
    assert hit is not None and "Temple" in hit[1] and "Harbour Town" in hit[1]


def test_unexpected_keys_are_kept_as_other_details_and_reported():
    raw = {"case": {"district": "Coastal District", "mystery": "x"}, "bogus_section": {"a": "Harbour PS"},
           "other_details": [{"label": "Beat", "value": "Harbour PS"}]}
    o = obs(raw, PAGE1)
    assert set(o.unexpected_keys) == {"case.mystery", "bogus_section"}
    labels = {d.label for d in o.others}
    assert "Beat" in labels and any(l.startswith("(unmapped)") for l in labels)


# ---------------------------------------------------------------- merge across pages


def test_same_value_on_two_pages_is_one_value_with_both_pages():
    o1 = obs({"case": {"fir_number": "901/2021"}}, PAGE1, page=1)
    o2 = obs({"case": {"fir_number": "901/2021"}}, "FIR No: 901/2021\nOther text", page=2)
    d = merge_observations([o1, o2], page_infos=[info(1), info(2)])
    v = d.single["case"]["fir_number"]
    assert v.status == ValueStatus.EXTRACTED and v.value == "901/2021" and v.pages == [1, 2]


def test_different_values_conflict_and_every_candidate_is_kept():
    o1 = obs({"case": {"fir_number": "901/2021"}}, PAGE1, page=1)
    o2 = obs({"case": {"fir_number": "910/2021"}}, "FIR No: 910/2021", page=2)
    d = merge_observations([o1, o2], page_infos=[info(1), info(2)])
    v = d.single["case"]["fir_number"]
    assert v.status == ValueStatus.CONFLICTING and v.value is None and v.needs_review
    assert [(c.value, c.source_page) for c in v.candidates] == [("901/2021", 1), ("910/2021", 2)]
    issues = validate_structured(d).issues
    assert any(i.code == "conflict" and i.path == "case.fir_number" for i in issues)


def test_accused_are_separate_entities_with_their_own_details():
    raw = {"accused": [
        {"serial_no": "1", "name": "Manoj Pillai", "parent_or_spouse": "Raman", "age": "35"},
        {"serial_no": "2", "name": "Priya Menon", "parent_or_spouse": "Suresh", "age": "29"},
    ]}
    d = merge_observations([obs(raw, PAGE2, page=2)], page_infos=[info(2)])
    a = d.lists["accused"]
    assert len(a) == 2
    assert (a[0].fields["name"].value, a[0].fields["age"].value) == ("Manoj Pillai", "35")
    assert (a[1].fields["name"].value, a[1].fields["age"].value) == ("Priya Menon", "29")
    assert a[0].fields["age"].source_text.startswith("1 | Manoj")  # evidence is the person's own line


def test_same_person_on_two_pages_merges_and_keeps_new_details():
    p2 = obs({"accused": [{"name": "Manoj Pillai", "age": "35"}]}, PAGE2, page=2)
    p3 = obs({"accused": [{"name": "Manoj Pillai", "arrest_status": "absconding"}]}, PAGE3, page=3)
    d = merge_observations([p2, p3], page_infos=[info(2), info(3)])
    rec = d.lists["accused"][0]
    assert len(d.lists["accused"]) == 1 and rec.pages == [2, 3]
    assert rec.fields["age"].value == "35" and rec.fields["arrest_status"].value == "absconding"
    assert rec.fields["address"].status == ValueStatus.NOT_FOUND


def test_merge_does_not_confuse_sections_that_share_an_index():
    # page 1 has witness 0; page 2 starts accused 0 and 1 and repeats that witness: it must merge
    # with page 1's witness, not be blocked because index 0 was just created in another section
    p1 = obs({"witnesses": [{"name": "Arjun Nair", "age": "41"}]}, "Arjun Nair age 41", page=1)
    p2 = obs({"accused": [{"name": "Manoj Pillai"}, {"name": "Priya Menon"}],
              "witnesses": [{"name": "Arjun Nair", "role": "complainant"}]},
             PAGE2 + "\nArjun Nair complainant", page=2)
    d = merge_observations([p1, p2], page_infos=[info(1), info(2)])
    assert len(d.lists["witnesses"]) == 1 and d.lists["witnesses"][0].pages == [1, 2]
    assert d.lists["witnesses"][0].fields["age"].value == "41"
    assert d.lists["witnesses"][0].fields["role"].value == "complainant"
    assert len(d.lists["accused"]) == 2


def test_conflicting_details_for_the_same_person_are_not_overwritten():
    p2 = obs({"accused": [{"name": "Manoj Pillai", "age": "35"}]}, PAGE2, page=2)
    p3 = obs({"accused": [{"name": "Manoj Pillai", "age": "53"}]}, "Accused\nManoj Pillai age 53", page=3)
    d = merge_observations([p2, p3], page_infos=[info(2), info(3)])
    age = d.lists["accused"][0].fields["age"]
    assert age.status == ValueStatus.CONFLICTING and {c.value for c in age.candidates} == {"35", "53"}


def test_same_name_twice_on_one_page_is_not_merged_but_flagged():
    page = "1 | Ravi Kumar | age 30\n2 | Ravi Kumar | age 52"
    raw = {"witnesses": [{"serial_no": "1", "name": "Ravi Kumar", "age": "30"},
                         {"serial_no": "2", "name": "Ravi Kumar", "age": "52"}]}
    d = merge_observations([obs(raw, page)], page_infos=[info(1)])
    assert len(d.lists["witnesses"]) == 2
    assert any(i.code == "duplicate_record" for i in validate_structured(d).issues)


def test_nameless_records_are_never_merged():
    raw = {"witnesses": [{"role": "Doctor"}, {"role": "Doctor"}]}
    d = merge_observations([obs(raw, "Doctor\nDoctor")], page_infos=[info(1)])
    assert len(d.lists["witnesses"]) == 2


def test_nothing_is_invented_when_the_page_says_nothing():
    d = merge_observations([obs({}, "Some unrelated page text here")], page_infos=[info(1)])
    assert all(v.status == ValueStatus.NOT_FOUND and v.value is None for _, v in d.iter_values())
    assert all(not recs for recs in d.lists.values())


def test_every_document_has_the_same_sections_and_fields():
    a = merge_observations([], page_infos=[])
    b = merge_observations([obs({"case": {"district": "Coastal District"}}, PAGE1)], page_infos=[info(1)])
    assert {k: sorted(v) for k, v in a.single.items()} == {k: sorted(v) for k, v in b.single.items()}
    assert set(a.lists) == set(b.lists) == {s.key for s in SECTIONS if s.repeated}


def test_document_status_reflects_page_failures():
    ok = merge_observations([obs({}, "x" * 50)], page_infos=[info(1), info(2, mapping_status="failed", note="down")])
    assert ok.document_status == DocumentStructuredStatus.PARTIAL
    allfail = merge_observations([], page_infos=[info(1, mapping_status="failed")])
    assert allfail.document_status == DocumentStructuredStatus.FAILED


# ---------------------------------------------------------------- validation


def _data_with_case(**values):
    page = "\n".join(["Police Station | Harbour PS", "FIR No | 901/2021", "Date of FIR | 18/08/2021", "Year | 2021"])
    return merge_observations([obs({"case": values}, page)], page_infos=[info(1)])


def test_valid_result_has_no_errors_and_required_gaps_are_warnings():
    d = _data_with_case(police_station="Harbour PS", fir_number="901/2021")
    v = validate_structured(d)
    assert v.is_valid
    assert {i.path for i in v.issues if i.code == "required_missing"} == {"accused", "acts_and_sections"}


def test_unparseable_date_is_flagged_not_converted():
    d = _data_with_case(fir_date="18/08/2021")
    d.single["case"]["fir_date"].value = "18/13/2021"
    d.single["case"]["fir_date"].source_text = "Date of FIR | 18/13/2021"
    v = validate_structured(d)
    assert any(i.code == "date_shape" and i.path == "case.fir_date" for i in v.issues)
    assert d.single["case"]["fir_date"].value == "18/13/2021" and d.single["case"]["fir_date"].needs_review


def test_bad_year_and_act_with_digits_are_flagged():
    page = "Year | 21\nAct | u/s 307 IPC"
    raw = {"case": {"year": "21"}, "acts_and_sections": [{"act": "u/s 307 IPC", "section": "307"}]}
    d = merge_observations([obs(raw, page)], page_infos=[info(1)])
    codes = {i.code for i in validate_structured(d).issues}
    assert {"year_format", "act_contains_digits"} <= codes


def test_evidence_that_does_not_contain_the_value_is_an_error():
    d = _data_with_case(police_station="Harbour PS")
    d.single["case"]["police_station"].source_text = "something else"
    v = validate_structured(d)
    assert not v.is_valid and any(i.code == "evidence_mismatch" for i in v.issues)


def test_source_text_must_be_on_the_cited_page():
    d = _data_with_case(police_station="Harbour PS")
    v = validate_structured(d, page_texts={1: "an entirely different page"})
    assert any(i.code == "evidence_not_on_page" for i in v.issues)
    assert validate_structured(d, page_texts={1: PAGE1}).is_valid


def test_source_page_outside_the_document_is_an_error():
    d = _data_with_case(police_station="Harbour PS")
    d.single["case"]["police_station"].source_page = 9
    assert any(i.code == "bad_source_page" for i in validate_structured(d).issues)


def test_missing_page_numbers_are_flagged_not_assumed():
    d = _data_with_case(police_station="Harbour PS")
    d.single["case"]["police_station"].source_page = None
    assert any(i.code == "no_source_page" for i in validate_structured(d).issues)


# ---------------------------------------------------------------- mapper with a fake model


class FakeClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def complete_json(self, system, user, schema, max_tokens=None):
        self.calls.append(user)
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def mapper(replies):
    return StructuredMapper(client=FakeClient(replies), settings=get_settings())


def test_mapper_returns_grounded_observations():
    m = mapper([{"case": {"police_station": "Harbour PS"}}])
    o = m.map_page(1, PAGE1, text_engine="pdf_text")
    assert o.info.mapping_status == "ok" and o.single[0][2].status == ValueStatus.EXTRACTED


def test_blank_page_is_skipped_without_calling_the_model():
    m = mapper([])
    o = m.map_page(1, "  ", text_engine="pdf_text")
    assert o.info.mapping_status == "skipped" and m.client.calls == []


def test_unavailable_model_marks_page_failed_without_inventing():
    m = mapper([VlmUnavailable("server not reachable")])
    o = m.map_page(1, PAGE1, text_engine="pdf_text")
    assert o.info.mapping_status == "failed" and "vlm_unavailable" in o.info.note
    assert not o.single and not o.records


def test_timeout_and_malformed_reply_mark_page_failed():
    for exc in (VlmTimeout("timed out"), VlmBadResponse("model reply was not valid JSON")):
        o = mapper([exc]).map_page(1, PAGE1, text_engine="pdf_text")
        assert o.info.mapping_status == "failed"


def test_truncated_reply_is_retried_on_halves_of_the_page():
    m = mapper([
        VlmBadResponse("model reply was cut off at the token limit"),
        {"case": {"district": "Coastal District"}},
        {"case": {"police_station": "Harbour PS"}},
    ])
    o = m.map_page(1, PAGE1, text_engine="pdf_text")
    assert len(m.client.calls) == 3 and o.info.mapping_status == "ok"
    assert {k for _, k, _ in o.single} == {"district", "police_station"}


def test_long_page_is_split_and_one_failed_part_makes_the_page_partial(monkeypatch):
    monkeypatch.setenv("VLM_MAP_CHUNK_CHARS", "60")
    get_settings.cache_clear()
    text = "\n".join(f"Line number {i} of the page with filler text" for i in range(6))
    m = mapper([{"case": {}}, VlmTimeout("timed out"), {"case": {}}, {"case": {}}, {"case": {}}, {"case": {}}])
    o = m.map_page(1, text, text_engine="pdf_text")
    assert o.info.mapping_status == "partial" and len(m.client.calls) > 2


class Recorder:
    """A fake model that records each request's system text, user text and the sections in its schema."""

    def __init__(self, behave):
        self.behave, self.calls = behave, []

    def complete_json(self, system, user, schema, max_tokens=None):
        sections = [k for k in schema["properties"] if k != "other"]
        self.calls.append((system, user, sections))
        return self.behave(sections, len(self.calls))


DENSE = "\n".join(
    ["Accused", "1 | Manoj Pillai age 35", "Witnesses", "1 | Arjun Nair age 41", "FIR No | 901/2021",
     "Police Station | Harbour PS", "Section 379 IPC", "Investigating Officer | SI P. Kumar"]
    + [f"filler line {i} of a long narrative paragraph that makes the page dense" for i in range(40)])


def test_a_dense_page_is_asked_one_section_per_request_with_a_shared_prefix():
    rec = Recorder(lambda sections, n: {})
    m = StructuredMapper(client=rec, settings=get_settings())
    assert len(DENSE) > get_settings().vlm_map_split_chars
    m.map_page(1, DENSE, text_engine="pdf_text")
    asked = [s for _, _, s in rec.calls]
    assert len(asked) > 1 and all(len(s) == 1 for s in asked)
    assert len({system for system, _, _ in rec.calls}) == 1  # constant rules: identical across requests
    # single-valued sections get the whole page text first, identical, and only the field list after it
    # differs (list sections are asked in smaller pieces, so their text differs by design)
    whole = {user.split("\n\n--\n")[0] for _, user, s in rec.calls if not SECTION_BY_KEY[s[0]].repeated}
    assert len(whole) == 1
    assert "Field names:" not in rec.calls[0][0] and "Field names:" in rec.calls[0][1]


def test_list_sections_of_a_dense_page_are_asked_in_small_pieces_so_answers_fit_the_token_limit():
    people = "\n".join(f"{i} | Witness Person{i} age {30 + i} r/o Some Street {i}" for i in range(1, 41))
    page = "Witnesses\n" + people + "\nFIR No | 901/2021\nPolice Station | Harbour PS"
    rec = Recorder(lambda sections, n: {})
    StructuredMapper(client=rec, settings=get_settings()).map_page(1, page, text_engine="pdf_text")
    witness_calls = [user for _, user, s in rec.calls if s == ["witnesses"]]
    limit = get_settings().vlm_map_list_chunk_chars
    assert len(witness_calls) >= 2  # 40 people are not asked in one answer
    assert all(len(u.split("\n\n--\n")[0]) <= limit + 60 for u in witness_calls)  # + the "PAGE n TEXT:" header
    covered = "\n".join(u.split("\n\n--\n")[0] for u in witness_calls)
    assert all(f"Person{i} " in covered for i in range(1, 41))  # no person is dropped between pieces
    case_calls = [s for _, _, s in rec.calls if s == ["case"]]
    assert len(case_calls) == 1  # single-valued sections stay one request


def test_a_short_page_is_asked_in_one_request():
    rec = Recorder(lambda sections, n: {})
    StructuredMapper(client=rec, settings=get_settings()).map_page(3, PAGE3, text_engine="pdf_text")
    assert len(rec.calls) == 1 and len(rec.calls[0][2]) > 1


def test_a_cut_off_combined_answer_falls_back_to_one_request_per_section():
    def behave(sections, n):
        if len(sections) > 1:
            raise VlmBadResponse("model reply was cut off at the token limit")
        return {"case": [{"k": "police_station", "v": "Harbour PS"}]} if sections == ["case"] else {}

    rec = Recorder(behave)
    o = StructuredMapper(client=rec, settings=get_settings()).map_page(1, PAGE1 + "\n" + PAGE2, text_engine="pdf_text")
    assert o.info.mapping_status == "ok"
    assert any(k == "police_station" for _, k, _ in o.single)
    assert len(rec.calls) > 2 and all(len(s) == 1 for _, _, s in rec.calls[1:])


def test_one_section_that_cannot_be_mapped_makes_the_page_partial_not_failed():
    def behave(sections, n):
        if sections == ["witnesses"]:
            raise VlmBadResponse("model reply was cut off at the token limit")
        return {"case": [{"k": "police_station", "v": "Harbour PS"}]} if sections == ["case"] else {}

    o = StructuredMapper(client=Recorder(behave), settings=get_settings()).map_page(1, DENSE, text_engine="pdf_text")
    assert o.info.mapping_status == "partial" and "[witnesses]" in o.info.note
    assert any(k == "police_station" for _, k, _ in o.single)  # the other sections' answers are kept


def test_every_section_failing_is_a_failed_page():
    o = StructuredMapper(client=Recorder(lambda s, n: (_ for _ in ()).throw(VlmTimeout("timed out"))),
                         settings=get_settings()).map_page(1, DENSE, text_engine="pdf_text")
    assert o.info.mapping_status == "failed"


def test_schema_and_prompt_cover_every_field_and_are_json_serialisable():
    import json

    schema = build_json_schema()
    json.dumps(schema)
    assert set(schema["properties"]) == {s.key for s in SECTIONS} | {"other"}
    assert "required" not in schema  # every section is optional: absent = the page does not say
    case_pair = schema["properties"]["case"]["items"]
    assert case_pair["properties"]["k"]["enum"] == [f.key for f in SECTIONS[0].fields]  # keys are enforced
    assert case_pair["properties"]["v"]["minLength"] == 1  # no empty-string placeholders
    prompt = build_system_prompt()
    for s in SECTIONS:
        assert f"{s.key}: " in prompt and all(f.key in prompt for f in s.fields)
    # field descriptions are NOT in the prompt: a small model copied them back as values
    assert not any(f.hint and f.hint in prompt for s in SECTIONS for f in s.fields)


# ---------------------------------------------------------------- a small model's habits


def to_pairs(raw: dict) -> dict:
    """The dict answer shape as the {k, v} pair shape the real schema asks for."""
    out = {}
    for key, body in raw.items():
        if key == "case" or key in ("complainant", "informant"):
            out[key] = [{"k": k, "v": v} for k, v in body.items()]
        elif key == "other":
            out[key] = body
        else:
            out[key] = [{"f": [{"k": k, "v": v} for k, v in item.items()]} for item in body]
    return out


def test_pair_shaped_and_dict_shaped_answers_give_the_same_observations():
    raw = {"case": {"police_station": "Harbour PS", "fir_number": "901/2021"},
           "accused": [{"serial_no": "1", "name": "Manoj Pillai", "age": "35"}]}
    a, b = obs(raw, PAGE1 + "\n" + PAGE2), obs(to_pairs(raw), PAGE1 + "\n" + PAGE2)
    assert [(s, k, o.value, o.status) for s, k, o in a.single] == [(s, k, o.value, o.status) for s, k, o in b.single]
    assert [{k: v.value for k, v in r.fields.items()} for r in a.records] == [{k: v.value for k, v in r.fields.items()} for r in b.records]


@pytest.mark.parametrize("junk", ["Not Mentioned", "not specified", "Not Available", "unknown", "null"])
def test_model_placeholders_are_not_values(junk):
    o = obs({"case": {"district": junk, "fir_date": junk}, "witnesses": [{"name": junk}]}, PAGE1)
    assert o.single == [] and o.records == [] and o.others == []


def test_a_placeholder_the_page_itself_prints_next_to_the_label_is_the_documents_answer():
    page = "Jurisdiction | Not Available\nPolice Station | Harbour PS"
    o = obs({"case": {"jurisdiction": "Not Available"}}, page)
    assert o.single[0][2].status == ValueStatus.EXPLICIT_NA


def test_instruction_echo_is_ignored_unless_the_page_has_that_text():
    echo = {"case": {"district": "district name", "fir_number": "FIR number"},
            "accused": [{"name": "full name only, without s/o, age or address"}]}
    o = obs(echo, PAGE1)
    assert o.single == [] and o.records == [] and o.dropped == 3


def test_a_record_with_nothing_on_the_page_is_not_shown_as_a_person_but_is_kept():
    raw = {"accused": [{"name": "Invented Person", "age": "40"}, {"name": "Manoj Pillai", "age": "35"}]}
    o = obs(raw, PAGE2, page=2)
    assert [r.fields["name"].value for r in o.records] == ["Manoj Pillai"]
    assert {(d.label, d.value) for d in o.others} == {("(not on page) accused.name", "Invented Person"),
                                                      ("(not on page) accused.age", "40")}
    assert all(d.needs_review and d.status == ValueStatus.UNVERIFIED for d in o.others)


def test_value_on_the_page_under_another_label_is_ambiguous_not_extracted():
    page = "Court | JMFC North\nFIR No | 55/2022"
    o = obs({"case": {"district": "JMFC North", "court_name": "JMFC North"}}, page)
    got = {k: v.status for _, k, v in o.single}
    assert got == {"district": ValueStatus.AMBIGUOUS, "court_name": ValueStatus.EXTRACTED}


def test_one_value_with_evidence_beats_unsupported_values_without_hiding_them():
    p1 = obs({"case": {"year": "2022", "fir_number": "55/2022"}}, "Year | 2022\nFIR No | 55/2022", page=1)
    p3 = obs({"case": {"year": "2023", "district": "JMFC North"}}, "Court | JMFC North", page=3)  # 2023 is not on the page
    p2 = obs({"case": {"district": "North Zone"}}, "District | North Zone", page=2)
    d = merge_observations([p1, p2, p3], page_infos=[info(1), info(2), info(3)])
    year = d.single["case"]["year"]
    assert year.status == ValueStatus.EXTRACTED and year.value == "2022" and year.needs_review  # not silently settled
    assert {c.value for c in year.candidates} == {"2022", "2023"} and "2023" in year.note
    district = d.single["case"]["district"]
    assert district.value == "North Zone" and district.status == ValueStatus.EXTRACTED
    assert {c.value for c in district.candidates} == {"North Zone", "JMFC North"}
    codes = {(i.code, i.path) for i in validate_structured(d).issues}
    assert ("unused_candidates", "case.year") in codes and not any(c == "conflict" for c, _ in codes)


def test_values_without_any_evidence_that_disagree_are_still_a_conflict():
    p1 = obs({"case": {"jurisdiction": "Circle A"}}, "nothing relevant here", page=1)
    p2 = obs({"case": {"jurisdiction": "Circle B"}}, "nothing relevant here either", page=2)
    d = merge_observations([p1, p2], page_infos=[info(1), info(2)])
    assert d.single["case"]["jurisdiction"].status == ValueStatus.CONFLICTING


def test_a_person_needs_their_section_heading_above_them():
    # the model put an accused's name under "informant": the name is on the page, but not in an informant section
    o = obs({"informant": [{"name": "Priya Menon"}]}, PAGE2, page=2)
    assert o.single == [] and o.records == []  # a record with nothing evidenced is not shown as an informant
    o = obs({"accused": [{"name": "Priya Menon"}]}, PAGE2, page=2)
    assert o.records[0].fields["name"].status == ValueStatus.EXTRACTED
    o = obs({"accused": [{"name": "Priya Menon"}]}, "Priya Menon w/o Suresh", page=2)  # no heading at all
    assert o.records[0].fields["name"].status == ValueStatus.AMBIGUOUS
    assert "heading" in o.records[0].fields["name"].note
    o = obs({"complainant": [{"name": "Ramesh Kumar"}]}, "Ramesh Kumar", page=1)
    assert o.single == [] or o.single[0][2].status == ValueStatus.AMBIGUOUS


def test_format_examples_appear_only_for_the_sections_asked_and_never_count_as_values():
    from app.services.structured_mapper import EXAMPLE_ACT, EXAMPLE_PERSON, select_sections

    prompt3 = build_system_prompt(select_sections(PAGE3))  # witnesses + accused + case
    assert EXAMPLE_PERSON in prompt3 and EXAMPLE_ACT not in prompt3
    only_case = build_system_prompt([SECTIONS[0]])
    assert EXAMPLE_PERSON not in only_case and EXAMPLE_ACT not in only_case
    # a model copying the example back gets nothing for it unless the page really says it
    o = obs({"accused": [{"f": [{"k": "name", "v": "Tara Bose"}, {"k": "age", "v": "20"}]}]}, PAGE2, page=2)
    assert o.records == [] and o.dropped >= 1


def test_name_with_relationship_marker_and_long_serial_are_flagged_not_cleaned():
    page = "Accused\nSuresh Singh s/o Mohan Singh age 32 r/o Ward 4\n1. Suresh Singh s/o Mohan Singh age 32"
    raw = {"accused": [{"f": [{"k": "serial_no", "v": "1. Suresh Singh s/o Mohan Singh age 32"},
                              {"k": "name", "v": "Suresh Singh s/o Mohan Singh"}]}]}
    d = merge_observations([obs(raw, page)], page_infos=[info(1)])
    rec = d.lists["accused"][0]
    assert rec.fields["name"].value == "Suresh Singh s/o Mohan Singh"  # never rewritten
    codes = {(i.code, i.path) for i in validate_structured(d).issues}
    assert ("name_contains_relation", "accused.0.name") in codes and ("serial_format", "accused.0.serial_no") in codes
    assert rec.fields["name"].needs_review and rec.fields["serial_no"].needs_review


def test_the_model_is_only_asked_about_sections_the_page_shows_signs_of():
    from app.services.structured_mapper import select_sections

    keys = lambda text: [s.key for s in select_sections(text)]  # noqa: E731
    assert keys(PAGE2) == ["accused"] or set(keys(PAGE2)) <= {"accused", "case"}
    assert "witnesses" in keys(PAGE3) and "accused" in keys(PAGE3) and "property" not in keys(PAGE3)
    assert "informant" not in keys(PAGE1) and "complainant" not in keys(PAGE1)
    assert keys("Brief narrative with nothing of the kind.") == ["case"]  # nothing recognised: case details only
    # a page in another script cannot be judged by the English vocabulary: everything is asked
    assert len(keys("अभियुक्त राम कुमार " * 6)) == len(SECTIONS)
    schema = build_json_schema(select_sections(PAGE3))
    assert set(schema["properties"]) <= {s.key for s in SECTIONS} | {"other"} and "informant" not in schema["properties"]
    assert "informant:" not in build_system_prompt(select_sections(PAGE3))


def test_mapper_sends_a_narrowed_schema_and_records_which_sections_it_asked():
    seen = {}

    class Spy:
        def complete_json(self, system, user, schema, max_tokens=None):
            seen["schema"], seen["system"] = schema, system
            return {}

    m = StructuredMapper(client=Spy(), settings=get_settings())
    o = m.map_page(2, PAGE3, text_engine="pdf_text")
    assert "informant" not in seen["schema"]["properties"] and "witnesses" in seen["schema"]["properties"]
    assert o.info.sections_asked and "witnesses" in o.info.sections_asked


def test_recorded_real_model_answers_do_not_flood_the_result_with_junk():
    """The first prompt made the real 3B model echo the instructions, fill empty slots and invent an act.
    These are its recorded answers for the three pages of the synthetic sample_03 fixture."""
    import json
    from pathlib import Path

    import fitz

    root = Path(__file__).parent / "fixtures"
    answers = json.loads((root / "vlm_answers" / "sample_03_live_run1.json").read_text(encoding="utf-8"))["answers"]
    doc = fitz.open(root / "sample_03_multipage.pdf")
    observations = [obs(answers[str(i)], doc[i - 1].get_text("text"), page=i) for i in (1, 2, 3)]
    d = merge_observations(observations, page_infos=[info(i) for i in (1, 2, 3)])
    case = d.single["case"]
    assert (case["fir_number"].value, case["fir_number"].status) == ("55/2022", ValueStatus.EXTRACTED)
    assert case["police_station"].value == "Hill View" and case["district"].value == "North Zone"
    assert case["court_name"].value == "JMFC North" and case["court_name"].status == ValueStatus.EXTRACTED
    assert case["charge_sheet_number"].value == "9/2023"
    assert not [p for p, v in d.iter_values() if v.status == ValueStatus.CONFLICTING], "no conflicts from junk"
    # the page-2 echo of the instructions and the page-3 "Not Mentioned" fill-ins are gone
    texts = {v.value for _, v in d.iter_values() if v.value} | {c.value for _, v in d.iter_values() for c in v.candidates}
    assert not any("only if labelled" in t or t == "Not Mentioned" or t == "full name only, without s/o, age or address" for t in texts)
    # the real witness survives; the invented NDPS act is not shown as an act
    assert [r.fields["name"].value for r in d.lists["witnesses"]] == ["Local shopkeeper"]
    assert all(r.fields.get("act") is None or r.fields["act"].value != "NDPS Act" for r in d.lists["acts_and_sections"])
    assert any(o.label.startswith("(not on page) acts_and_sections") for o in d.other_details)


def test_recorded_real_model_answers_in_the_pairs_format_keep_only_what_the_page_supports():
    """Second recorded run (pairs-format prompt, sections narrowed per page): the model still filled
    absent fields with 'Not Mentioned' / 'N/A', invented values and put a witness under officers."""
    import json
    from pathlib import Path

    import fitz

    root = Path(__file__).parent / "fixtures"
    answers = json.loads((root / "vlm_answers" / "sample_03_live_run2.json").read_text(encoding="utf-8"))["answers"]
    doc = fitz.open(root / "sample_03_multipage.pdf")
    d = merge_observations([obs(answers[str(i)], doc[i - 1].get_text("text"), page=i) for i in (1, 2, 3)],
                           page_infos=[info(i) for i in (1, 2, 3)])
    case = d.single["case"]
    assert (case["fir_number"].value, case["fir_number"].status) == ("55/2022", ValueStatus.EXTRACTED)
    assert case["charge_sheet_number"].value == "9/2023" and case["police_station"].value == "Hill View"
    assert [r.fields["name"].value for r in d.lists["accused"]] == ["Karan Mehta", "Neha Shah", "Unknown person (absconding)"]
    assert [r.pages for r in d.lists["accused"]] == [[2], [2], [2]]
    assert [r.fields["name"].value for r in d.lists["witnesses"]] == ["Local shopkeeper"]
    # nothing the document does not say is presented as extracted
    for key in ("fir_date", "submission_date"):
        assert case[key].status != ValueStatus.EXTRACTED
    assert case["jurisdiction"].status != ValueStatus.EXTRACTED  # "North" is on the page, but under no jurisdiction label
    for rec in d.lists["investigating_officers"]:  # a witness the model filed under officers
        assert rec.fields["name"].status != ValueStatus.EXTRACTED
    shown = [v.value for _, v in d.iter_values() if v.status == ValueStatus.EXTRACTED]
    assert "N/A" not in shown and "Not Mentioned" not in shown
    assert not any(v.status == ValueStatus.NOT_FOUND and v.value for _, v in d.iter_values())


def test_blank_structured_round_trips_through_json():
    from app.schemas.structured import StructuredData

    d = blank_structured()
    assert StructuredData.model_validate_json(d.model_dump_json()) == d

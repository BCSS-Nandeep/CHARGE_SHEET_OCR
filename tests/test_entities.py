"""Person entity parsing: attribute boundaries, word boundaries, splitting, roles."""

from types import SimpleNamespace

from app.services.entity_extractor import (
    extract_accused,
    extract_witnesses,
    find_person_mentions,
    parse_officers,
    parse_person,
    split_accused,
)


def item(text, serial=None, role=None, page=1):
    return SimpleNamespace(text=text, serial=serial, role=role, page=page, kind="x", source_text=text)


def test_attributes_stop_at_next_marker_phone_and_id():
    p = parse_person(
        "Sri. John Doe S/o Richard Roe., Age: 33yrs., Caste: X, Occ: Business., R/o. Northtown Village,\n"
        "Adhar No.234567890123., Contact:9876543210\n(Issued: 41-A Cr.P.C notice served on 02.03.2017)"
    )
    assert p.name == "John Doe"
    assert p.parent_or_spouse == "S/o Richard Roe"
    assert p.age == "33"
    assert p.occupation == "Business"
    assert p.address == "Northtown Village"
    assert p.identification == "234567890123"
    assert p.contact == "9876543210"
    assert "notice" in p.arrest_status


def test_occupation_and_address_do_not_absorb_role_or_narrative():
    p = parse_person(
        "Sri.A. Witness S/o B., age: 45yrs,\nOcc:PC-1363 of Riverside PS\nR/o Hilltop village of East Dist",
        role="Eye witness and assisted the I.O",
    )
    assert p.occupation == "PC-1363 of Riverside PS"
    assert p.address == "Hilltop village of East Dist"
    assert p.role.startswith("Eye witness")


def test_age_and_role_need_word_boundaries():
    # 'PAGE 2' must not give age 2; 'confession' must not give role 'io'
    p = parse_person("Sri. A. Officer, SI of Police Riverside : IO --- PAGE 2 ---")
    assert p.age is None
    q = parse_person("Sri. B. Panch S/o C, age: 30 conducted the confessioncum recovery")
    assert q.age == "30"
    assert q.role is None or "io" != q.role.lower()


def test_name_and_designation_are_separated():
    assert parse_person("Sri K John Doe SIP Riverside P.S.").name == "K John Doe"
    p = parse_officers("1. Ms. J. Doe, SI of Police Riverside PS\n2. Sri. R. Roe, SI of Police Riverside PS", page=1)
    assert [o.name for o in p] == ["J. Doe", "R. Roe"]
    assert parse_officers("SI A. Officer", page=1)[0].name == "A. Officer"
    t = parse_person("Smt, V. Example Tahsildar (Executive Magistrate) of Riverside Mandal.")
    assert t.name == "V. Example"
    assert "Tahsildar (Executive Magistrate)" in t.occupation


def test_glued_accused_block_split_with_cross_referenced_remarks():
    block = (
        "A1.JohnDoeS/oRichard.,age:24yrs,Caste:X.,Occ: painter.,R/o.\nNorthtown(V)\n"
        "A2.MarkRoeS/osam.,age:22yrs.,caste:Y.,R/ochityville,East\nRural\n"
        "(A1&A2werearrestedandsendtoRemandon02.03.2017)\n"
        "A3.PaulPoeS/opeter,Age:19yrs.Caste:Z.,Occ:student.,\nR/o H.NO.5-35,Temple street.\n"
        "4. (A3wasarrestedonsendtoRemandon03.03.2017throughPTWarrant)\n"
        "A4TomGoeS/.Harry.Age19yearscaste:wocc:labour.R/oHNo2-1-32,Market.\n"
    )
    chunks = split_accused(block)
    assert [s for s, _ in chunks] == ["1", "2", "3", "4"]
    people = extract_accused([item(block)])
    assert [p.name for p in people] == ["John Doe", "Mark Roe", "Paul Poe", "Tom Goe"]
    assert people[3].parent_or_spouse == "S/o Harry"
    assert people[3].age == "19" and people[3].occupation == "labour"
    assert "02.03.2017" in people[0].arrest_status and "02.03.2017" in people[1].arrest_status
    assert "03.03.2017" in people[2].arrest_status
    assert people[2].address.endswith("Temple street.") or people[2].address.endswith("Temple street")


def test_text_layout_accused_keep_their_list_number():
    people = extract_accused([item("Suresh Example s/o Mohan age 32 r/o Ward 4", serial="1")])
    assert people[0].serial_no == "1"
    assert people[0].address == "Ward 4"


def test_witness_ditto_inline_role_and_duplicates():
    ws = extract_witnesses(
        [
            item("Sri. A. One S/o B, age: 30", serial="1", role="Panch for scene"),
            item("Sri. C. Two S/o D, age: 40", serial="2", role="-do-"),
            item("LW-3 E. Three W/o F, Age: 41, R/o Lake Colony - Complainant", serial=None),
            item("LW-4 E. Three W/o F, Age: 41, R/o Lake Colony - Complainant", serial=None),
            item("PW-5 Dr. G. Five (medical officer)", serial=None),
        ]
    )
    assert [w.serial_no for w in ws] == ["1", "2", "3", "5"]  # duplicate entry merged
    assert ws[1].role == "Panch for scene" and "ditto" in ws[1].role_note
    assert ws[2].role == "Complainant" and ws[2].address == "Lake Colony"
    assert ws[3].name == "G. Five" and ws[3].occupation == "medical officer"


def test_alias_split():
    p = parse_person("A-2) Rob Example kumar @ Robby S/o Mark, Age; 24 years")
    assert p.name == "Rob Example kumar" and p.alias == "Robby"
    assert p.age == "24"


def test_narrative_mentions_extract_clean_names():
    text = (
        "on that information he went there and found one person namely Jon Exampel Doe S/o  Ricard, "
        "age:32yrs., Caste: X. In the presence of punches i.e.(1) Sri.A. Kemp, S/o Laxman.,age:39yrs. "
        "The accused persons i.e. A1-John Doe S/o Richard, Age: 20 Yrs."
    )
    mentions = find_person_mentions([(2, text)])
    names = [m.name for m in mentions]
    assert names == ["Jon Exampel Doe", "A. Kemp", "John Doe"]
    assert mentions[0].age == "32" and mentions[0].parent == "Ricard"
    assert all(m.page == 2 for m in mentions)

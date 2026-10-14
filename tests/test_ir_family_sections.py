"""Family-history rows, lettered annexure headings, bold selections, and blank template rows.

Synthetic data shaped like a real report that printed "Father's Sister", "Friends/Associates" and
"Drug Supplier" rows in the family table, put sections 25-32 under lettered headings (A., B., ...),
marked its choices by bold with empty boxes, and left many tables blank.
"""

from __future__ import annotations

from app.ir_standard.evidence import EvidenceIndex
from app.ir_standard.matching import snap_relation
from app.ir_standard.paths import flatten, set_value
from app.ir_standard.schema import FAMILY_RELATIONS, SECTION_BY_NUMBER, SECTIONS
from app.ir_standard.sectioner import split_sections
from app.ir_standard.validator import blank_standard, build_standard

S4, S5 = SECTION_BY_NUMBER[4], SECTION_BY_NUMBER[5]

FAMILY_MD = """5.FAMILY HISTORY

| Sl<br>No. | Relation | Name | Address | Phone number | Criminal<br>Back ground<br>Any | Alive /dead |
| --- | --- | --- | --- | --- | --- | --- |
|  | **Father** | Rao Kumar, Age 54, occ: Acting | Main Road, Hyderabad | 9948812484 | ☐ Yes<br>☐ **No** | ☐ **Alive**<br>☐ Dead |
|  | **Brothers** | Ravi Kumar, Age 26 | -do- | 8179923060 | ☐ Yes<br>**No** | ☐ **Alive**<br>Dead |
|  | **Father’s Sister** | Priya w/o Ranga | Nijampet | 9000000001 | ☐ Yes<br>☐ **No** | ☐ **Alive**<br>☐ Dead |
|  | **Father’s Sister** |  |  |  |  |  |
| 6. | **Friends/**<br>Associates | Mallela Akash, Age 31 | Anand Nagar | 9010906600 | ☐ Yes<br>☐ **No** | ☐ **Alive**<br>☐ Dead |
| 7. | **Drug Supplier** |  |  |  |  |  |
"""


def member_rows(raw_rows):
    index = EvidenceIndex(S5, FAMILY_MD, "docx_native")
    std = build_standard({S5.key: {"members": raw_rows}}, indexes={S5.key: index})
    return std["sections"][S5.key]["members"], std


# ---- family table -------------------------------------------------------------------------------------

def test_relations_match_exactly_so_a_fathers_sister_is_never_the_father():
    assert snap_relation("Father’s Sister", FAMILY_RELATIONS) == "Father's Sister"
    assert snap_relation("Father", FAMILY_RELATIONS) == "Father"
    assert snap_relation("Brothers", FAMILY_RELATIONS) == "Brothers" == snap_relation("Brother", FAMILY_RELATIONS)
    assert snap_relation("Sisters", FAMILY_RELATIONS) == "Sisters"
    assert snap_relation("Mother’s Sisters", FAMILY_RELATIONS) == "Mother's Sisters"
    assert snap_relation("Wife's Father", FAMILY_RELATIONS) == "Wife's Father"
    for unknown in ("Friends/ Associates", "Drug Supplier", "Cousin", ""):
        assert snap_relation(unknown, FAMILY_RELATIONS) is None


def test_every_printed_row_lands_on_its_own_relation_and_nothing_is_dropped():
    rows, std = member_rows([
        {"relation": "Father", "name": "Rao Kumar, Age 54, occ: Acting", "address": "Main Road, Hyderabad",
         "phone": "9948812484", "criminal_background": {"selected": "No"}, "alive_dead": {"selected": "Alive"}},
        {"relation": "Brothers", "name": "Ravi Kumar, Age 26", "address": "-do-", "phone": "8179923060"},
        {"relation": "Father’s Sister", "name": "Priya w/o Ranga", "address": "Nijampet", "phone": "9000000001"},
        {"relation": "Father’s Sister"},  # the second, blank printed row
        {"relation": "Friends/ Associates", "name": "Mallela Akash, Age 31", "address": "Anand Nagar",
         "phone": "9010906600"},
        {"relation": "Drug Supplier"},
    ])
    by = {r["relation"]: r for r in rows[: len(FAMILY_RELATIONS)]}
    assert [r["relation"] for r in rows[: len(FAMILY_RELATIONS)]] == list(FAMILY_RELATIONS)
    assert by["Father"]["name"].startswith("Rao Kumar")
    assert by["Brothers"]["name"] == "Ravi Kumar, Age 26"
    assert by["Father's Sister"]["name"] == "Priya w/o Ranga"  # not merged into "Father" or "Sisters"
    assert by["Sisters"]["name"] is None and by["Father's brothers"]["name"] is None
    extras = rows[len(FAMILY_RELATIONS):]
    assert [r["relation"] for r in extras] == ["Friends/ Associates"]  # a row the template has no line for: kept as printed
    assert extras[0]["name"] == "Mallela Akash, Age 31" and extras[0]["phone"] == "9010906600"
    # the blank second "Father's Sister" row and the empty "Drug Supplier" row add nothing
    assert not [f for f in std["review_flags"] if f["code"] == "ungrounded_value_removed" and "members" in f["path"]]


def test_a_relation_the_model_invents_is_not_added():
    rows, _ = member_rows([{"relation": "Uncle on the moon", "name": "Nobody"}])
    assert len(rows) == len(FAMILY_RELATIONS)


def test_family_rows_show_up_in_the_field_list_under_their_own_labels():
    rows, std = member_rows([{"relation": "Father’s Sister", "name": "Priya w/o Ranga"},
                             {"relation": "Friends/ Associates", "name": "Mallela Akash, Age 31"}])
    flat = {f.id: f for f in flatten(std)}
    labels = [f.label for f in flat.values() if f.id.startswith("s05_family_history.members.") and f.id.endswith(".name")]
    assert any("Father's Sister" in x for x in labels) and any("Friends/ Associates" in x for x in labels)
    assert all(f.group == "Family members" for f in flat.values() if f.id.startswith("s05_family_history.members."))


# ---- lettered headings ----------------------------------------------------------------------------------

LETTERED_MD = "\n".join([
    "Facts of the case.",
    "24. PRESENT WHEREABOUTS", "| a | ☐ In jail | x |",
    "Annexure-A", "**Additional Information for NDPS Act-1985 Cases (Mandatory)**",
    "**A. Types of Drugs.**", "| Sl. No. | Type of Drug | Quantity |", "| 1. | **Ganja** | 12 grms |",
    "**B. Consumer Details.**", "| Sl. No. | Name | Aadhaar Card and Phone No. |", "| 1. | Ravi | 9083 |",
    "**C. Time since the accused is running drug peddling and his/her Modus operandi (MO):**", "| Notes: |",
    "**D. Financial History and Bank Account Transactions (including UPI IDs)**", "| Sl. No. | UPI IDs |",
    "**E. SIM Details**", "| Sl. No. | Phone No. | SDR | IMEI |", "| 1. | 9515 | Name | 8662 |",
    "**F. DOPAMS Links of the accused arrested/apprehended under the NDPS Act, 1985.**",
    "**G. Papillon Device**", "**H. CCTNS**",
])


def test_sections_25_to_32_are_found_under_lettered_annexure_headings():
    chunks, missing = split_sections(LETTERED_MD)
    for key in ("s25_types_of_drugs_sold", "s26_consumers", "s27_peddling_period", "s29_financial_history",
                "s30_sim_details", "s32_dopams_links"):
        assert key in chunks, key
    assert "Ganja" in chunks["s25_types_of_drugs_sold"] and "Ganja" not in chunks["s26_consumers"]
    assert "IMEI" in chunks["s30_sim_details"] and "Papillon" in chunks["s32_dopams_links"]
    assert {"s28_source_of_procurement", "s31_legal_support"} <= set(missing)  # really absent from this text


# ---- the template keeps every section, field and column --------------------------------------------------

def test_json_and_field_list_keep_every_section_and_column_even_when_the_report_has_no_data():
    std = build_standard({})  # nothing at all was read
    assert list(std["sections"]) == [s.key for s in SECTIONS]
    flat = flatten(std)
    ids = {f.id for f in flat}
    for s in SECTIONS:
        if not s.number:
            continue
        assert any(f.section == s.number for f in flat), f"section {s.number} has no fields in the list"
        for t in s.tables:
            for c in t.columns[1 if t.fixed_rows else 0:]:
                assert f"{s.key}.{t.key}.0.{c.key}" in ids, f"{s.key}.{t.key}.{c.key}"  # blank row, every column
            assert len(std["sections"][s.key][t.key]) >= 1


def test_a_reviewer_can_fill_a_blank_table_row_and_add_rows():
    std = blank_standard()
    assert set_value(std, "s06_associates.associates.0.member_name", "Ravi Kumar")
    assert std["sections"]["s06_associates"]["associates"][0]["member_name"] == "Ravi Kumar"
    assert set_value(std, "s06_associates.associates.2.mobile_no", "9876543210")  # a new row further down
    rows = std["sections"]["s06_associates"]["associates"]
    assert len(rows) == 3 and rows[1]["member_name"] is None and rows[2]["mobile_no"] == "9876543210"
    assert not set_value(std, "s06_associates.associates.999.mobile_no", "x")


def test_new_columns_for_the_drug_consumer_and_sim_tables_exist():
    cols = {t.key: {c.key for c in t.columns} for s in SECTIONS for t in s.tables}
    assert "mode_of_transport" in cols["drugs"] and "imei" in cols["sims"]
    assert {"address_details", "place_of_consumption"} <= cols["consumers"]


# ---- written-value rows (a report that prints only the chosen value) -------------------------------------

def test_a_values_only_row_is_read_as_written_and_unlisted_values_are_not_forced_onto_options():
    md = ("3.PHYSICAL FEATURES\n\n| **Sex**<br>**(1)** | **Built**<br>**(2)** | **Height**<br>**(3)** | **Hair**<br>**(5)** | **Color**<br>**(7)** |\n"
          "| --- | --- | --- | --- | --- |\n| **Male** | Normal | 6.1 ft | Curly Hair | Normal |\n")
    spec = SECTION_BY_NUMBER[3]
    index = EvidenceIndex(spec, md, "docx_native")
    std = build_standard({spec.key: {"sex": "Male", "built": "Normal", "height": "6.1-6.5 ft", "hair": "Curly",
                                     "color": "Fair"}}, indexes={spec.key: index})
    sec = std["sections"][spec.key]
    assert sec["sex"]["selected"] == "Male" and sec["built"]["selected"] == "Normal" and sec["hair"]["selected"] == "Curly"
    # "6.1 ft" is not one of the height bands: kept exactly as written, the model's 6.1-6.5 band is not assumed
    assert sec["height"]["selected"] is None and sec["height"]["other"] == "6.1 ft"
    assert sec["color"]["selected"] is None and sec["color"]["other"] == "Normal"
    codes = {(f["code"], f["path"].split(".")[-1]) for f in std["review_flags"] if f["section"] == spec.key}
    assert ("choice_not_in_source", "height") in codes and ("written_value_not_an_option", "color") in codes


# ---- the model filling blank printed rows --------------------------------------------------------------

def test_blank_printed_family_rows_stay_blank_even_if_the_model_fills_them():
    filled_blank = {"name": "-do-", "address": "-do-", "phone": "-do-",
                    "criminal_background": {"selected": "No", "evidence": "bold"},
                    "alive_dead": {"selected": "Alive", "evidence": "bold"}}
    rows, std = member_rows([
        {"relation": "Father’s Sister", "name": "Priya w/o Ranga", "address": "Nijampet", "phone": "-do-",
         "criminal_background": {"selected": "No"}, "alive_dead": {"selected": "Alive"}},
        {"relation": "Father’s Sister", **filled_blank},  # the second printed row is blank
        {"relation": "Drug Supplier", **filled_blank},  # printed, blank
        {"relation": "Children", **filled_blank},  # no such row is printed at all
    ])
    by = {r["relation"]: r for r in rows[: len(FAMILY_RELATIONS)]}
    sister = by["Father's Sister"]
    assert sister["name"] == "Priya w/o Ranga" and sister["address"] == "Nijampet"
    assert sister["phone"] is None  # "-do-" is not in Priya's printed row
    assert sister["criminal_background"]["selected"] == "No"  # bold "No" in her own row
    assert sister["alive_dead"]["selected"] == "Alive"
    assert not [r for r in rows[len(FAMILY_RELATIONS):]]  # the blank second row and "Drug Supplier" add nothing
    children = by["Children"]
    assert children["name"] is None and children["criminal_background"]["selected"] is None
    assert children["alive_dead"]["selected"] is None
    assert any(f["code"] == "ungrounded_rows_removed" and "Children" in f["detail"] for f in std["review_flags"])


def test_choices_in_a_family_row_are_read_from_that_rows_own_marks():
    rows, std = member_rows([{"relation": "Father", "name": "Rao Kumar, Age 54, occ: Acting",
                              "criminal_background": {"selected": "Yes"}, "alive_dead": {"selected": "Dead"}}])
    father = rows[0]
    assert father["criminal_background"]["selected"] is None and father["alive_dead"]["selected"] is None
    codes = {f["code"] for f in std["review_flags"] if f["path"].startswith("s05_family_history.members.0.")}
    assert "unsupported_selection" in codes  # the report's bold marks are No / Alive, so Yes / Dead are not assumed


# ---- header detection, headings, dashes, categories ----------------------------------------------------

def test_the_word_yes_is_not_a_column_header_for_eyes():
    from app.ir_standard.evidence import match_field

    s3 = SECTION_BY_NUMBER[3]
    assert match_field("yes", s3.fields) is None and match_field("**yes**", s3.fields) is None
    assert match_field("Eyes", s3.fields).key == "eyes" and match_field("Eyes (4)", s3.fields).key == "eyes"
    md = ("3.PHYSICAL FEATURES\n\n| **Nose**<br>**(9)** | **Beard**<br>**(10)** | **Moustache**<br>**(11)** | **Ear**<br>**(12)** |  |\n"
          "| --- | --- | --- | --- | --- |\n|  | **Beard** | **yes** | **Normal** |  |\n"
          "| **Normal** | Full beard | ☐ Turnee up |  |  |\n| Hooked | ☐ Short | ☐ Chipped |  |  |\n")
    index = EvidenceIndex(s3, md, "docx_native")
    nose = {m.label: m for m in index.marks_for(next(f for f in s3.fields if f.key == "nose"))}
    assert nose["Normal"].bold and "Hooked" in nose  # the nose options stay in the Nose column
    eyes = next(f for f in s3.fields if f.key == "eyes")
    assert not index.marks_for(eyes)  # nothing from the moustache column leaks into Eyes
    assert index.text_for(next(f for f in s3.fields if f.key == "ear")) == "Normal"  # the Ear column's own value


def test_the_sections_own_heading_line_is_not_read_as_a_field_label():
    s7 = SECTION_BY_NUMBER[7]
    md = "**7.PERIOD OF OFFENCE:**\n\n| ☐ Dark Fort night | ☐ **Moon light** | ☐ Strangers’ night | ☐ Others (specify) |\n| --- | --- | --- | --- |"
    index = EvidenceIndex(s7, md, "docx_native")
    assert index.written_answer(s7.fields[0]) is None  # it is an option list
    std = build_standard({s7.key: {"period": {"selected": "Moon light", "evidence": "bold"}}}, indexes={s7.key: index})
    period = std["sections"][s7.key]["period"]
    assert period["selected"] == ["Moon light"] and period["verification"]["chosen"][0]["via"] == "bold"
    assert not [f for f in std["review_flags"] if f["section"] == s7.key]


def test_a_dash_returned_for_a_field_that_has_text_is_flagged():
    s3 = SECTION_BY_NUMBER[3]
    md = ("3.PHYSICAL FEATURES\n\n| **Burn Marks**<br>**(16)** | **Leucodema**<br>**(17)** | **Mole**<br>**(18)** | **Scar**<br>**(19)** | **Tattoo**<br>**(20)** |\n"
          "| --- | --- | --- | --- | --- |\n| -- | - | - |  | Left arm (owl mark) |\n")
    index = EvidenceIndex(s3, md, "docx_native")
    std = build_standard({s3.key: {"tattoo": "-", "burn_marks": "--"}}, indexes={s3.key: index})
    flagged = {f["path"].split(".")[-1]: f["code"] for f in std["review_flags"] if f["section"] == s3.key}
    assert std["sections"][s3.key]["tattoo"] == "Left arm (owl mark)"  # the report's own cell replaces the dash
    assert flagged.get("tattoo") == "filled_from_report_text"
    assert "burn_marks" not in flagged and std["sections"][s3.key]["burn_marks"] == "--"  # a dash for a dash is fine


def test_oc_is_a_category_option():
    from app.ir_standard.matching import snap_option

    s1 = SECTION_BY_NUMBER[1]
    cat = next(f for f in s1.fields if f.key == "category")
    assert snap_option("OC", cat.options) == "OC"


def test_label_value_rows_inside_a_table_with_a_header_row_are_read():
    s1 = SECTION_BY_NUMBER[1]
    md = ("1.INDIVIDUAL PARTICULARS\n\n| Sl<br>No. | Particulars | First name | Middle name | Last name |\n| --- | --- | --- | --- | --- |\n"
          "|  | Name | Asha |  | Rao |\n|  | Father’s name | Ravi Rao |  |  |\n|  | SC/ST/BC/Others | OC |  |  |\n")
    index = EvidenceIndex(s1, md, "docx_native")
    std = build_standard({s1.key: {"name_first": "Asha", "name_last": "Rao", "father_name": "Ravi Rao",
                                   "category": {"selected": "OC", "evidence": "written"}}}, indexes={s1.key: index})
    sec = std["sections"][s1.key]
    assert sec["category"]["selected"] == "OC" and sec["category"]["verification"]["status"] == "written"
    assert sec["father_name"] == "Ravi Rao" and sec["name_first"] == "Asha" and sec["name_last"] == "Rao"
    assert not [f for f in std["review_flags"] if f["section"] == s1.key and f["code"] != "missing_required"]

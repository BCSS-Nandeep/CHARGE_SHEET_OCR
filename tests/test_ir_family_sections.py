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

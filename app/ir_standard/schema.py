"""The standard Interrogation Report (Part-II, 32 sections) as data.

This is the single source of truth for the fixed output template: the VLM
prompts, the validator, the blank template and the standard JSON are all
generated from it. Uploaded reports may look different; whatever they contain
is mapped onto these sections, fields and canonical options.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

SCHEMA_VERSION = "ir-standard-1.0"

TEXT, SINGLE, MULTI = "text", "single", "multi"


@dataclass(frozen=True)
class FieldSpec:
    key: str
    label: str
    kind: str = TEXT  # text | single | multi
    options: Tuple[str, ...] = ()
    column_no: Optional[int] = None  # printed column number, e.g. "(3)" for Height
    required: bool = False
    # single/multi: keep text that matches no option instead of discarding it
    allow_other: bool = False


@dataclass(frozen=True)
class TableSpec:
    key: str
    title: str
    columns: Tuple[FieldSpec, ...]
    fixed_rows: Tuple[str, ...] = ()  # first-column labels of a fixed-row table (e.g. Father, Mother)
    blank_rows: int = 2  # empty rows printed in the blank template
    max_rows: int = 40


@dataclass(frozen=True)
class SectionSpec:
    number: int
    key: str
    title: str
    fields: Tuple[FieldSpec, ...] = ()
    tables: Tuple[TableSpec, ...] = ()
    layout: str = "kv"  # kv | physical | choices | tables
    hints: Tuple[str, ...] = field(default_factory=tuple)  # extra prompt guidance

    @property
    def heading(self) -> str:
        return f"{self.number}. {self.title}" if self.number else self.title


def _t(key: str, label: str, **kw) -> FieldSpec:
    return FieldSpec(key, label, TEXT, **kw)


def _s(key: str, label: str, options, **kw) -> FieldSpec:
    return FieldSpec(key, label, SINGLE, tuple(options), **kw)


def _m(key: str, label: str, options, **kw) -> FieldSpec:
    return FieldSpec(key, label, MULTI, tuple(options), **kw)


YES_NO = ("Yes", "No")

# The standard IR's family table, row by row. A report that prints other rows (e.g. "Friends/Associates")
# keeps them as extra rows; nothing is dropped and no printed row is forced onto a different relation.
FAMILY_RELATIONS = (
    "Father", "Mother", "Brothers", "Sisters", "Wife", "Children", "Father's brothers", "Father's Sister",
    "Mother's Brother", "Mother's Sisters", "Wife's Father", "Wife's Mother", "Wife's brother", "Wife's sisters",
    "Any others (Brother in laws)",
)

# Case-file columns shared by sections 15 and 17
_CASE_COLUMNS = (
    _t("dist_unit_division", "Dist/Unit/Division"),
    _t("ps", "P.S"),
    _t("cr_no", "Cr.No"),
    _t("section_of_law", "Sec. of law"),
    _t("gang_members", "Gang members each case"),
    _t("property_stolen", "Property stolen"),
    _t("property_recovered", "Property recovered"),
    _t("police_officers_arrested", "Police officers who arrested"),
    _t("remarks", "Remarks"),
)

_WARRANT_COLUMNS = (
    _t("dist_division", "Dist/Division"),
    _t("ps", "P.S"),
    _t("cr_no", "Cr.No"),
    _t("section_of_law", "Sec. of law"),
    _t("sc_cc_no", "SC/CC No"),
    _t("court", "Court"),
)


SECTIONS: Tuple[SectionSpec, ...] = (
    SectionSpec(
        0, "s00_narrative", "Narrative (Part-I and Part-III)",
        fields=(
            _t("report_date", "Date"),
            _t("officer", "Reporting officer"),
            _t("officer_station", "Officer's station"),
            _t("part1_facts", "Facts of the case (Part-I)"),
            _t("part1_accused", "Accused (Part-I)"),
            _t("part3_facts", "Facts of the case (Part-III)"),
            _t("part3_registration", "Case registration (Part-III)"),
        ),
        hints=("Copy the narrative paragraphs verbatim; do not summarise.",),
    ),
    SectionSpec(
        1, "s01_individual_particulars", "INDIVIDUAL PARTICULARS",
        fields=(
            _t("name_first", "First name", required=True),
            _t("name_middle", "Middle name"),
            _t("name_last", "Last name"),
            _t("alias", "Alias"),
            _t("father_name", "Father", required=True),
            _t("age", "Age", required=True),
            _t("nationality", "Nationality"),
            _t("religion", "Religion"),
            _t("caste", "Caste"),
            _s("category", "SC/ST/BC/Others", ("SC", "ST", "BC", "OC", "Others"), allow_other=True),
            _t("present_address", "Present address"),
            _t("permanent_address", "Permanent address"),
        ),
        hints=("The accused is marked 'A-1', 'A-2' ...; drop that prefix from the name.",),
    ),
    SectionSpec(
        2, "s02_unique_identifiers", "UNIQUE IDENTIFIERS",
        fields=(
            _t("identification_type", "Identification type"),
            _t("mobile_number", "Mobile number"),
            _t("email_ids", "Email IDs"),
            _t("social_media_account", "Social Media Account"),
            _t("aadhaar_number", "Aadhar Number"),
            _t("ration_card", "Ration card"),
            _t("passport", "Passport"),
            _t("pan_card", "PAN card"),
            _t("electricity_connection", "Electricity Connection"),
            _t("gas_connection", "Gas Connection"),
            _t("vehicles", "Vehicles"),
            _t("driving_license", "Driving License"),
            _t("other_id_proofs", "Other ID Proofs"),
        ),
    ),
    SectionSpec(
        3, "s03_physical_features", "PHYSICAL FEATURES", layout="physical",
        fields=(
            _s("sex", "Sex", ("Male", "Female"), column_no=1),
            _s("built", "Built", ("Lean", "Stout", "Stiff", "Erect", "Stooping", "Hunch Back", "Normal", "Medium"), column_no=2),
            _s("height", "Height", ("3.5-4.0 ft", "4.1-4.5 ft", "4.6-5.0 ft", "5.1-5.5 ft", "5.6-6.0 ft",
                                    "6.1-6.5 ft", "6.6-7.0 ft", "7.1-7.5 ft"), column_no=3, allow_other=True),
            _s("eyes", "Eyes", ("Black", "Cat", "Squint", "One Eye", "Wearing Spectacles", "Others"), column_no=4),
            _s("hair", "Hair", ("Black", "Brown", "White", "Bald", "Long", "Curly", "Back Combed",
                                "Partly Combed", "Mixed Hairs"), column_no=5),
            _s("face", "Face", ("Round", "Oval", "Wrinkled", "Square", "Long", "Others"), column_no=6),
            _s("color", "Color", ("White", "Fair", "Black", "Brown", "Sallow", "Medium"), column_no=7),
            _s("teeth", "Teeth", ("Regular", "Irregular", "Discolored", "Broken", "Double", "Normal", "Others"),
               column_no=8),
            _s("nose", "Nose", ("Large", "Small", "Hooked", "Snub", "Projected", "Sharp", "Normal"), column_no=9),
            _s("beard", "Beard", ("Trim", "Long", "Short", "French Cut", "Clean Shave"), column_no=10),
            _s("mustaches", "Mustaches", ("Small", "Long", "Turned Up", "Chipped", "Dropping", "No Mustaches"),
               column_no=11),
            _t("ear", "Ear", column_no=12),
            _t("identification_marks", "Identification marks", column_no=13),
            _t("deformities", "Deformities/Peculiarities", column_no=14),
            _t("language_dialect", "Language/Dialect", column_no=15),
            _t("burn_marks", "Burn Marks", column_no=16),
            _t("leucoderma", "Leucodema", column_no=17),
            _t("mole", "Mole", column_no=18),
            _t("scar", "Scar", column_no=19),
            _t("tattoo", "Tattoo", column_no=20),
        ),
        hints=(
            "Each column (1)-(11) is a single choice. The chosen option is the one with a tick, "
            "or in bold while the others are plain, or with 'Yes' written beside it. "
            "An empty box with plain text is NOT chosen. Ear (12) and columns (13)-(20) are free text.",
        ),
    ),
    SectionSpec(
        4, "s04_socio_economic_profile", "SOCIO / ECONOMIC PROFILE", layout="choices",
        fields=(
            _s("living_status", "Living Status", ("Alone", "In Hotel", "Hut", "With Family", "In Hostel", "Slum",
                                                   "With Associates", "Kacha House", "With Brother")),
            _s("marital_status", "Marital Status", ("Married", "Unmarried", "Having two wives")),
            _s("education", "Education Details", ("Up to 5th class", "7th to 10th", "Intermediate", "Graduate",
                                                   "Post graduate", "Professional Course", "Illiterate")),
            _s("occupation", "Occupation", ("Govt Job", "Contract Labour", "Student", "Pvt Job", "Business",
                                             "Farmer", "Daily Labour", "Unemployed", "Finance")),
            _s("income_group", "Income Group", ("Lower Income Rs.2500 pm", "Lower middle income Rs.2500 to Rs.5000",
                                                 "Middle income Rs.5000 to Rs.10000",
                                                 "Upper middle income Rs.10000 to Rs.25000",
                                                 "Upper income Rs.25000 above")),
        ),
    ),
    SectionSpec(
        5, "s05_family_history", "FAMILY HISTORY",
        fields=(
            _s("family_stay_together", "Does the family members stay together", YES_NO),
            _t("family_peculiarity", "Peculiarity of family members, any"),
        ),
        tables=(
            TableSpec(
                "members", "Family members",
                (
                    _t("relation", "Relation"),
                    _t("name", "Name"),
                    _t("address", "Address"),
                    _t("phone", "Phone number"),
                    _s("criminal_background", "Criminal Background / Any", YES_NO),
                    _s("alive_dead", "Alive / Dead", ("Alive", "Dead")),
                ),
                fixed_rows=FAMILY_RELATIONS,
            ),
        ),
    ),
    SectionSpec(
        6, "s06_associates", "RELATION SHIP WITH OTHER ASSOCIATES / GANG MEMBERS AND FRIENDS", layout="tables",
        tables=(
            TableSpec("associates", "Associates", (
                _t("gang", "Gang"), _t("member_name", "Member name"), _t("address", "Address"),
                _t("mobile_no", "Mobile no."), _t("relationship", "Relationship"),
            ), blank_rows=3),
        ),
    ),
    SectionSpec(
        7, "s07_period_of_offence", "PERIOD OF OFFENCE", layout="choices",
        fields=(
            _m("period", "Period of offence", ("Dark Fort night", "Moon light", "Strangers' night", "Others")),
            _t("period_other", "Others (specify)"),
        ),
    ),
    SectionSpec(
        8, "s08_local_contacts", "LOCAL CONTACTS/FACILITATORS", layout="tables",
        tables=(
            TableSpec("contacts", "Local contacts", (
                _t("town_city_village", "Town/City/village"), _t("ps_limits", "PS / Limits"),
                _t("contact_name_address", "Name and address of Contact person"),
            )),
        ),
    ),
    SectionSpec(
        9, "s09_regular_habits", "REGULAR HABITS", layout="choices",
        fields=(
            _m("habits", "Regular habits", ("Liquor", "Toddy", "Smoking", "Gambling", "Chewing Pan/Gutka/Ganja",
                                             "Prostitution", "Horse Race", "Maintenance of keep", "Others")),
            _t("keep_name", "Maintenance of keep (name, if any)"),
            _t("habits_other", "Others (specify)"),
        ),
        hints=("Several habits may be marked.",),
    ),
    SectionSpec(
        10, "s10_indulgence_before_offence", "INDULGENCE BEFORE OFFENCE", layout="choices",
        fields=(
            _m("indulgence", "Indulgence before offence",
               ("Witnessing movie", "Consuming liquor/Toddy", "Consuming Ganja", "On Road with Bike", "Others")),
            _t("indulgence_other", "Others (specify)"),
        ),
    ),
    SectionSpec(
        11, "s11_modus_operandi", "MODUS OPERANDI", layout="tables",
        tables=(
            TableSpec("mo", "Modus operandi", (
                _t("crime_head", "Crime head"), _t("crime_sub_head", "Crime Sub-Head"),
                _t("mo_description", "MO (description)"),
            ), blank_rows=1),
        ),
    ),
    SectionSpec(
        12, "s12_shelter", "SHELTER", layout="tables",
        tables=(
            TableSpec("shelter", "Shelter", (
                _t("regular_residence", "Regular residence"), _t("preparation_of_offence", "Preparation of offence"),
                _t("after_offence", "After offence"), _t("remarks", "Remarks"),
            ), blank_rows=1),
        ),
    ),
    SectionSpec(
        13, "s13_disposal_of_property", "DISPOSAL OF PROPERTY", layout="tables",
        tables=(
            TableSpec("disposal", "Disposal of property", (
                _t("how_disposed", "How disposed"), _t("receiver_name", "Receiver details / Name of receiver"),
                _t("address", "Address"), _t("family_members_others", "Family members/ Others (specify)"),
            ), blank_rows=1),
        ),
    ),
    SectionSpec(
        14, "s14_share_of_amount_spent", "HOW SHARE OF AMOUNT SPENT", layout="choices",
        fields=(
            _m("spent_on", "How share of amount spent",
               ("Lavish spending", "Keep maintenance", "Prostitution", "Clearing Debts", "Saving")),
        ),
    ),
    SectionSpec(
        15, "s15_cases_confessed", "CASES CONFESSED", layout="tables",
        tables=(TableSpec("cases", "Cases confessed", _CASE_COLUMNS, blank_rows=3),),
    ),
    SectionSpec(
        16, "s16_arrest_particulars", "ARREST PARTICULARS",
        fields=(
            _t("date_of_arrest", "Date of arrest", required=True),
            _t("place_of_arrest", "Place of arrest"),
            _t("crime_no_section", "Crime No. & Sec of law", required=True),
            _t("police_station", "Police Station", required=True),
            _t("sub_division", "Sub Division"),
            _t("district_unit", "Dist/Unit"),
            _t("arrested_by", "Arrested by"),
            _t("interrogated_by", "Interrogated by"),
            _t("others_who_can_identify", "Others who can identify"),
        ),
    ),
    SectionSpec(
        17, "s17_previous_cases", "PREVIOUS CASES INVOLVED", layout="tables",
        tables=(TableSpec("cases", "Previous cases", _CASE_COLUMNS, blank_rows=5),),
    ),
    SectionSpec(
        18, "s18_pt_warrants_nbws", "REGULARISATION OF PT WARRANTS / NBWS", layout="tables",
        tables=(
            TableSpec("pt_warrants", "a) Regularization of PT Warrant",
                      _WARRANT_COLUMNS + (_t("date_of_regularization", "Date of regularization of PT Warrant"),
                                          _t("remarks", "Remarks"))),
            TableSpec("nbw_execution", "b) Execution of NBW",
                      _WARRANT_COLUMNS + (_t("date_of_execution", "Date of Execution of NBW"),
                                          _t("remarks", "Remarks"))),
        ),
    ),
    SectionSpec(
        19, "s19_nbw_pending", "NBW PENDING", layout="tables",
        tables=(
            TableSpec("nbw_pending", "NBW pending",
                      _WARRANT_COLUMNS + (_t("pending_since", "Pending Since"), _t("sureties", "Sureties"))),
        ),
    ),
    SectionSpec(
        20, "s20_sureties", "SURETIES", layout="tables",
        tables=(
            TableSpec("sureties", "Sureties", (
                _t("dist_division", "Dist/Division"), _t("ps", "P.S"), _t("cr_no_section", "Cr.No & Sec. of law"),
                _t("surety_name", "Surety name"), _t("surety_address", "Surety address"),
                _s("local_non_local", "Local / Non local", ("Local", "Non local")),
                _s("nature_of_surety", "Nature of Surety", ("Cash", "Personal", "Professional")),
            )),
        ),
    ),
    SectionSpec(
        21, "s21_jail_stay", "JAIL STAY & ACQUINTANCES", layout="tables",
        tables=(
            TableSpec("jail", "Jail stay", (
                _t("jail_from", "Jail period: From"), _t("jail_to", "Jail period: To"), _t("jail_name", "Jail name"),
                _t("acquaintance_name_address", "New acquaintance: Name & address of jail member"),
                _t("nature_of_crime", "Nature of Crime involved"),
                _t("ps_remanded", "Police station where from remanded"),
                _t("remarks_mo", "Remarks / MO"),
                _t("cases_details", "GO / Button required to show cases"),
            )),
        ),
    ),
    SectionSpec(
        22, "s22_new_gang_formation", "NEW GANG FORMATION", layout="tables",
        tables=(
            TableSpec("gang", "New gang formation", (
                _t("addition_split", "Addition /split"), _t("name_address", "Name & Address"),
                _t("previous_history", "Previous history (PS, Unit & State involved in crime)"),
                _t("jails_lodged", "Jails were lodged"),
            )),
        ),
    ),
    SectionSpec(
        23, "s23_conviction_acquittals", "CONVICTION / ACQUITTALS", layout="tables",
        tables=(
            TableSpec("convictions", "Conviction / acquittals", (
                _t("dist_unit", "Dist/Unit"), _t("division", "Division"), _t("ps", "PS"), _t("cr_no", "Cr.No."),
                _t("section_of_law", "Sec. of law"), _t("sc_cc_no", "SC/CC NO."),
                _t("date_of_judgement", "Date of Judgement"),
                _t("details", "Details of Conviction/Acquittal"),
                _t("reasons_for_acquittal", "Reasons for acquittal if any"),
            )),
        ),
    ),
    SectionSpec(
        24, "s24_present_whereabouts", "PRESENT WHEREABOUTS",
        fields=(
            _s("status", "Present whereabouts",
               ("In jail", "On bail", "Absconding", "Normal life", "Rehabilitated", "Dead", "Facing Trial")),
            _t("jail_from_where_sent", "In jail: from where sent"),
            _t("jail_crime_no", "In jail: Crime No. / UT No."),
            _t("bail_crime_no_date", "On bail: Crime No. and date of bail"),
            _t("absconding_crime_numbers", "Absconding: Crime Numbers"),
            _t("trial_police_station", "Facing trial: Police Station"),
            _t("trial_crime_numbers", "Facing trial: Crime numbers"),
        ),
        hints=("Lettered rows a)-g); the chosen row is the one marked or written 'yes'.",),
    ),
    SectionSpec(
        25, "s25_types_of_drugs_sold", "Types of Drugs Sold", layout="tables",
        tables=(
            TableSpec("drugs", "Types of drugs sold", (
                _t("type_of_drug", "Type of Drug"), _t("quantity", "Quantity"), _t("earnings", "Earnings"),
                _t("supplier", "Supplier"), _t("supplies_to", "Supplies to"),
                _t("mode_of_transport", "Mode of Transport"),
            )),
        ),
        hints=("Some reports print other column names: 'Purchase amount & mode of payment' = earnings, 'Supplier Details' = supplier, 'Receivers Details' = supplies_to.",),
    ),
    SectionSpec(
        26, "s26_consumers", "Consumers", layout="tables",
        tables=(
            TableSpec("consumers", "Consumers", (
                _t("consumer_name", "Consumer Name"), _t("aadhaar_phone", "Aadhar Card and Phone No."),
                _t("address_details", "Address Details"), _t("place_of_consumption", "Place of consumption"),
                _t("other_consumers_phone", "Other consumers and Phone no."),
                _t("other_sources_phone", "Other sources and phone no."),
            )),
        ),
        hints=("'Place of consumption' is its own column (place_of_consumption); do not put it under address_details. 'Name' = consumer_name.",),
    ),
    SectionSpec(
        27, "s27_peddling_period", "Time since the accused is running peddling",
        fields=(_t("notes", "Notes"),),
    ),
    SectionSpec(
        28, "s28_source_of_procurement", "Source of Procurement of Drugs", layout="tables",
        tables=(
            TableSpec("suppliers", "Source of procurement", (
                _t("supplier", "Supplier"), _t("address", "Address"), _t("phone_no", "Phone no."),
                _t("mode_of_delivery", "Mode of delivery"), _t("payment_mode", "Payment mode"),
            )),
        ),
    ),
    SectionSpec(
        29, "s29_financial_history", "Financial History and Transactions", layout="tables",
        tables=(
            TableSpec("financial", "Financial history", (
                _t("bank_details", "Bank Details of accused and family"), _t("upi_ids", "UPI IDs"),
                _t("pan_no", "PAN No."),
                _t("immovable_property", "Immovable Property acquired with proceeds of Crime"),
                _t("movable_property", "Movable property acquired with proceeds of Crime"),
            )),
        ),
    ),
    SectionSpec(
        30, "s30_sim_details", "SIM Details", layout="tables",
        tables=(
            TableSpec("sims", "SIM details", (
                _t("phone_no", "Phone No."), _t("sdr", "SDR"), _t("imei", "IMEI"), _t("accused_details", "Accused details"),
                _t("true_caller_name", "True Caller Name"),
            )),
        ),
    ),
    SectionSpec(
        31, "s31_legal_support", "Legal Support", layout="tables",
        tables=(
            TableSpec("advocates", "Legal support", (_t("advocate_name", "Name of advocate"),
                                                      _t("phone_number", "Phone number")), blank_rows=1),
        ),
    ),
    SectionSpec(
        32, "s32_dopams_links", "DOPAMS Links",
        fields=(_t("links", "DOPAMS links"),),
    ),
)

SECTION_BY_KEY: Dict[str, SectionSpec] = {s.key: s for s in SECTIONS}
SECTION_BY_NUMBER: Dict[int, SectionSpec] = {s.number: s for s in SECTIONS}


def section_spec(key: str) -> SectionSpec:
    return SECTION_BY_KEY[key]


def field_specs(section: SectionSpec) -> List[FieldSpec]:
    return list(section.fields)

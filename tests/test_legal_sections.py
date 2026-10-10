"""Legal provision grammar, roles and conflicts — no per-document patterns."""

import pytest

from app.services.legal_section_parser import consolidate, normalize_act, parse_provisions


def pairs(text, **kw):
    return [(p.act, p.section, p.role) for p in parse_provisions(text, zone=kw.pop("zone", "narrative"), page=1, **kw)]


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Sections: 188 IPC,34(a) A.P Excise Act & U/s 8(C) r/w 22 of NDPS Act-1985",
         [("IPC", "188"), ("A.P. Excise Act", "34(a)"), ("NDPS Act", "8(C) r/w 22")]),
        ("U/Sec.8(c),20(b)(ii)(A)ofNDPSAct", [("NDPS Act", "8(c)"), ("NDPS Act", "20(b)(ii)(A)")]),
        ("u/s 302, 34 IPC", [("IPC", "302"), ("IPC", "34")]),
        ("Sections 420, 406 IPC & 66 IT Act", [("IPC", "420"), ("IPC", "406"), ("IT Act", "66")]),
        ("20 (b) NDPS Act-1985", [("NDPS Act", "20(b)")]),
        ("303(2), 317(2) BNS", [("BNS", "303(2)"), ("BNS", "317(2)")]),
        ("u/s 3(1)(r) SC/ST (POA) Act", [("SC/ST (POA) Act", "3(1)(r)")]),
        ("IPC Sec 379", [("IPC", "379")]),
        ("punishable u188 IPC", [("IPC", "188")]),
    ],
)
def test_grammar(text, expected):
    got = [(a, s) for a, s, _ in pairs(text, zone="form", default_role="charged")]
    assert got == expected


@pytest.mark.parametrize(
    "text",
    [
        "The complainant can be reached on 9123456789.",
        "A man aged about 45 years removed the vehicle at serial no. 302 of the property register.",
        "registered vide Cr.No.83/2017 and FIR No. 12/2016",
        "seized 30 lts of liquid in 2 drums on 05.05.2017 at 1130 hrs",
        "Act-1985 came into force in 1985",
    ],
)
def test_numbers_that_are_not_sections(text):
    assert pairs(text) == []


def test_roles_from_sentence_context():
    text = (
        "A case was registered in Cr. No. 4/2017 u/s 188 IPC, 34(A) TS Excise Act and took up the investigation. "
        "Later the IO served notice u/s 41(a)(1) Cr.P.C on the accused. "
        "Basing on the FSL report the IO altered the Section of law 188 IPC to 188 IPC & U/s 8(C) r/w 22 of NDPS Act. "
        "Final report is false, action u/s 182/211 IPC. "
        "Hence the accused committed offence punishable u/s 188 IPC."
    )
    got = pairs(text)
    assert ("IPC", "188", "registered") in got
    assert ("T.S. Excise Act", "34(A)", "registered") in got
    assert ("CrPC", "41(a)(1)", "procedural") in got
    assert ("IPC", "188", "altered_from") in got
    assert ("NDPS Act", "8(C) r/w 22", "charged") in got
    assert ("IPC", "182", "reference") in got and ("IPC", "211", "reference") in got
    charged, others, conflicts = consolidate(parse_provisions(text, zone="narrative", page=1))
    assert {(c.act, c.section) for c in charged} == {("NDPS Act", "8(C) r/w 22"), ("IPC", "188")}
    assert ("CrPC", "41(a)(1)") in {(o.act, o.section) for o in others}


def test_same_section_under_different_state_acts_is_a_conflict():
    form = parse_provisions("34(a) A.P Excise Act", zone="form", page=1, default_role="charged")
    narrative = parse_provisions("a case was registered u/s 34(A) TS Excise Act", zone="narrative", page=2)
    charged, others, conflicts = consolidate(form + narrative)
    assert [c.act for c in charged] == ["A.P. Excise Act"]
    assert len(conflicts) == 1 and set(conflicts[0].values) == {"A.P. Excise Act", "T.S. Excise Act"}


def test_registration_sections_stand_in_when_no_charges():
    provs = parse_provisions("A case was registered u/s 379 IPC.", zone="narrative", page=1)
    charged, _, _ = consolidate(provs)
    assert [(c.act, c.section, c.role) for c in charged] == [("IPC", "379", "registered")]


def test_act_normalisation():
    assert normalize_act("I.P.C.,") == "IPC"
    assert normalize_act("NDPS Act-1985") == "NDPS Act"
    assert normalize_act("Cr.P.C") == "CrPC"
    assert normalize_act("TS Excise Act") == "T.S. Excise Act"

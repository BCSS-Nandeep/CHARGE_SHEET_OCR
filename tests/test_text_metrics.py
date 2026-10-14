"""CER / WER / field accuracy — verified comparison only."""

from evaluation.text_metrics import (
    build_validation_report,
    character_error_rate,
    field_accuracy,
    word_error_rate,
)


def test_cer_identical():
    r = character_error_rate("hello world", "hello world")
    assert r.errors == 0 and r.reference_units == len("hello world")
    assert r.value == 0.0


def test_wer_counts_substitutions():
    r = word_error_rate("Kalyan sold ganja", "Kalyan took ganja")
    assert r.reference_units == 3
    assert r.errors >= 1


def test_field_accuracy_missing():
    fa = field_accuracy(
        [("Name", "Dosapati Kalyan"), ("Age", "16")],
        [("Name", "Dosapati Kalyan")],
    )
    assert fa.matched == 1 and fa.expected == 2
    assert fa.missing and "Age" in fa.missing[0]


def test_report_warns_not_a_bare_claim():
    rep = build_validation_report(
        document_id="sample",
        reference_text="Dosapati Kalyan",
        hypothesis_text="DosapatiKalyan",
        expected_fields=[("Name", "Dosapati Kalyan")],
        extracted_fields=[("Name", "Dosapati Kalyan")],
    )
    d = rep.to_dict()
    assert d["cer"]["rate"] is not None
    assert any("not a general accuracy claim" in w for w in d["warnings"])

"""Conflict targeting and deterministic validation."""

from datetime import date, timedelta

from app.schemas.charge_sheet import (
    AccusedPerson,
    ChargeSheetData,
    ExtractedField,
    ExtractionStatus,
    ValidationStatus,
)
from app.services.conflict_service import apply_conflicts_to_fields, detect_person_conflicts
from app.services.entity_extractor import PersonEntity, PersonMention
from app.services.validation_service import ValidationService


def F(name, value, **kw):
    return ExtractedField(canonical_name=name, original_value=value, normalized_value=value,
                          extraction_confidence=kw.pop("ext", 0.85), mapping_confidence=kw.pop("map", 0.9), **kw)


def test_conflict_marks_only_the_matching_person():
    people = [
        PersonEntity(name="John Doe", parent_or_spouse="S/o Richard", age="33", source_page=1),
        PersonEntity(name="Mark Roe", parent_or_spouse="S/o Sam", age="40", source_page=1),
    ]
    mentions = [
        PersonMention(name="Jon Doe", parent="Richard", age="32", page=2, text="..."),
        PersonMention(name="Mark Roe", parent="Sam", age="40", page=2, text="..."),  # consistent
        PersonMention(name="Doe", parent="Richard", age=None, page=3, text="..."),  # shortened, not a variant
    ]
    conflicts = detect_person_conflicts(people, mentions, "accused")
    by_field = {c.field_name: set(c.values) for c in conflicts}
    assert by_field == {"accused[0].name": {"John Doe", "Jon Doe"}, "accused[0].age": {"33", "32"}}

    data = ChargeSheetData(
        accused_persons=[
            AccusedPerson(name=F("accused[0].name", "John Doe"), age=F("accused[0].age", "33")),
            AccusedPerson(name=F("accused[1].name", "Mark Roe"), age=F("accused[1].age", "40")),
        ]
    )
    data = apply_conflicts_to_fields(data, conflicts)
    assert data.accused_persons[0].age.validation_status == ValidationStatus.CONFLICTING
    assert data.accused_persons[0].age.alternate_values == ["32"]
    assert data.accused_persons[1].age.validation_status != ValidationStatus.CONFLICTING
    assert all(c.resolution == "unresolved" for c in data.source_conflicts)


def _validated(**case):
    data = ChargeSheetData()
    for k, v in case.items():
        setattr(data.case, k, F(k, v))
    return ValidationService().validate(data)


def test_dates_are_calendar_checked_and_normalised():
    d = _validated(fir_date="31.02.2020", submission_date="05-06-2021")
    assert d.case.fir_date.validation_status == ValidationStatus.INVALID
    assert d.case.submission_date.normalized_value == "2021-06-05"
    assert d.case.submission_date.original_value == "05-06-2021"  # original preserved
    future = (date.today() + timedelta(days=40)).strftime("%d.%m.%Y")
    assert _validated(submission_date=future).case.submission_date.validation_status == ValidationStatus.UNCERTAIN


def test_chronology_and_fir_year_cross_checks():
    d = _validated(fir_number="185/2021", year="2020", fir_date="01.10.2021", submission_date="29-04-2021")
    assert d.case.submission_date.validation_status == ValidationStatus.UNCERTAIN
    assert d.case.year.validation_status == ValidationStatus.UNCERTAIN
    assert any("earlier than the FIR date" in w for w in d.extraction_warnings)
    assert any("FIR number year" in w for w in d.extraction_warnings)


def test_blank_serial_and_bad_phone_are_invalid():
    d = _validated(charge_sheet_number="/2022", fir_number="Northtown")
    assert d.case.charge_sheet_number.validation_status == ValidationStatus.INVALID
    assert d.case.fir_number.validation_status == ValidationStatus.INVALID
    data = ChargeSheetData(accused_persons=[AccusedPerson(contact=F("accused[0].contact", "97044706663"),
                                                          age=F("accused[0].age", "230"))])
    data = ValidationService().validate(data)
    assert data.accused_persons[0].contact.validation_status == ValidationStatus.INVALID
    assert data.accused_persons[0].age.validation_status == ValidationStatus.INVALID


def test_confidence_zero_is_not_treated_as_half():
    data = ChargeSheetData()
    data.case.district = F("district", "Northtown", ext=0.0, map=0.0)
    data = ValidationService().validate(data)
    assert data.overall_confidence == 0.0
    failed = ChargeSheetData(extraction_status=ExtractionStatus.FAILED)
    assert ValidationService().validate(failed).overall_confidence == 0.0

from app.schemas.charge_sheet import ChargeSheetData, ExtractedField, SCHEMA_VERSION


def test_schema_optional_fields():
    data = ChargeSheetData()
    assert data.schema_version == SCHEMA_VERSION
    assert data.accused_persons == []
    assert data.case.fir_number is None


def test_extracted_field_traceability():
    f = ExtractedField(
        canonical_name="district",
        original_label="Distt.",
        original_value="Sample",
        normalized_value="Sample",
        source_page=1,
        source_text="Distt. : Sample",
        extraction_confidence=0.8,
        mapping_confidence=0.9,
    )
    data = ChargeSheetData()
    data.case.district = f
    assert data.field_map()["district"].original_label == "Distt."
    # corrected value is separate
    f.corrected_value = "Fixed"
    assert f.original_value == "Sample"

from app.schemas.charge_sheet import LEGACY_RESULT_WARNING, SCHEMA_VERSION, ChargeSheetData, PdfField


def test_schema_has_no_fixed_fields():
    data = ChargeSheetData()
    assert data.schema_version == SCHEMA_VERSION == "2.0.0"
    assert data.fields == []
    assert not data.is_legacy


def test_field_keeps_label_and_value_as_printed_and_correction_separately():
    f = PdfField(id="p1-02", page=1, pages=[1], serial="02", label="Charge Sheet No.", value="/2021.",
                 source="form_row")
    data = ChargeSheetData(fields=[f])
    assert data.field_map()["p1-02"].label == "Charge Sheet No."
    data.field_map()["p1-02"].corrected_value = "12/2021"
    assert data.fields[0].value == "/2021."  # the extracted value is never overwritten


def test_1x_payload_loads_as_reprocess_required():
    legacy = {
        "schema_version": "1.3.0",
        "extraction_status": "complete",
        "case": {"district": {"canonical_name": "district", "original_value": "X"}},
        "accused_persons": [],
        "narrative": None,
        "document_info": {"document_type": {"canonical_name": "document_type", "original_value": "charge_sheet"}},
        "page_summaries": [{"page_number": 1, "text_source": "native"}],
        "extraction_warnings": ["older warning"],
    }
    data = ChargeSheetData.model_validate(legacy)
    assert data.is_legacy and data.fields == []
    assert data.extraction_warnings[0] == LEGACY_RESULT_WARNING
    assert data.document_info.document_type == "charge_sheet"
    assert data.page_summaries[0].page_number == 1

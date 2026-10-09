# Accuracy Report — FIR 83/2020 Charge Sheet

Manually verified ground truth: `tests/fixtures/ground_truth_fir83_2020.json`

**Confidence scores from the extractor are NOT accuracy.** Metrics below are measured against ground truth.

## Summary metrics

| Metric | Value | Denominator | Correct | Incorrect | Missing |
|---|---:|---:|---:|---:|---:|
| Scalar field exact/contains match | 1.000 | 8 | 8 | 0 | 0 |
| Legal-section entity accuracy | 1.000 | 3 | 3 | 0 | 0 |
| Accused entity accuracy | 1.000 | 1 | 1 | 0 | 0 |
| Witness entity accuracy | 1.000 | 6 | 6 | 0 | — |
| Conflict detection rate | 1.000 | 2 | 2 | — | — |
| Completeness (all reference items) | 1.000 | 18 | 18 | — | — |

Extractor overall_confidence (NOT accuracy): `0.746`
Unresolved source conflicts: `2`
Unmapped retained fields: `1`

## Scalar field comparison

| Field | Expected | Got | Result |
|---|---|---|---|
| `district` | Medak | Medak. | correct |
| `police_station` | Haveli Ghanapur | Haveli Ghanapur. | correct |
| `year` | 2020 | 2020 | correct |
| `fir_number` | 83/2020 | 83/2020 | correct |
| `fir_date` | 05.05.2020 | 05.05.2020 | correct |
| `submission_date` | 19.04.2021 | 19.04.2021 | correct |
| `final_report_type` | Charge sheet | Charge sheet | correct |
| `supplementary_or_original` | Original | Original | correct |

## Known source inconsistencies (must not be auto-resolved)

- **accused.age:Aerrolla Rajender Goud**: values=['33', '32', '2'] — Multiple ages found for the same accused across the document (resolution=unresolved)
- **accused.name:Aerrolla Rajender Goud**: values=['Aerrolla Rajender Goud', 'that the accused Aerrolla Rajender Goud'] — Name spelling variations found in source text; not auto-resolved (resolution=unresolved)

## Remaining limitations

- Charge-sheet form layouts still vary widely; new templates may need synonym/context extensions.
- Narrative OCR/text noise can still create residual unmapped KV pairs.
- Seized-property and lab findings rely on narrative heuristics when form columns are blank.
- Authorization is configurable (`ENABLE_AUTH`); enable for shared deployments.

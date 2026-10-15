# Accuracy Report

Generated 2026-10-10 16:40 by `scripts/evaluate.py` at commit `71742df + uncommitted changes` — OCR enabled (local PaddleOCR).

**These are measured counts on a small dataset, not a general accuracy claim.** Extractor confidence scores are not accuracy and are not used here. Every value is shown as `ratio (numerator/denominator)`; see `evaluation/metrics.py` for exact definitions.

Schema 2.0.0 returns each PDF's own labels and values (no fixed field list). The ground truth records expected *values*, not labels, so these metrics measure whether each expected value was found and paired into a form field; label text and expected-blank fields are not scored, and precision (extra / wrong fields) is not measured.

Dataset: 9 document(s) evaluated — 3 real (ground truth transcribed from the source and awaiting human confirmation), 6 synthetic.

**Development-set caveat:** the real documents were used while building and fixing the parser, and the newer synthetic samples were written by the same author after the parser. High scores here show the pipeline handles these documents; they do not estimate accuracy on unseen templates. Measure on a held-out set of manually verified charge sheets before operational use.

## Aggregate

| Metric | All | Real documents | Synthetic |
|---|---|---|---|
| Document classification accuracy | 1.000 (9/9) | 1.000 (3/3) | 1.000 (6/6) |
| Expected case values found in a field | 1.000 (72/72) | 1.000 (27/27) | 1.000 (45/45) |
| Expected case values found in a form field (not only the narrative) | 0.944 (68/72) | 0.926 (25/27) | 0.956 (43/45) |
| Expected person names found in a form field | 1.000 (63/63) | 1.000 (36/36) | 1.000 (27/27) |
| Expected act + section found together in one field | 1.000 (16/16) | 1.000 (6/6) | 1.000 (10/10) |
| Completeness (expected items found in any field) | 1.000 (151/151) | 1.000 (69/69) | 1.000 (82/82) |
| Fields with a label (descriptive, not accuracy) | 0.959 (142/148) | 0.969 (62/64) | 0.952 (80/84) |
| OCR reference lines recognised (similarity >= 0.9) | 1.000 (30/30) | 1.000 (22/22) | 1.000 (8/8) |

## Per document

| Document | Layout | Extraction | case_values | case_values_form | names | legal_sections | completeness |
|---|---|---|---|---|---|---|---|
| `synthetic_01_digital_standard` | native-text | complete | 1.000 (11/11) | 0.909 (10/11) | 1.000 (5/5) | 1.000 (2/2) | 1.000 (18/18) |
| `synthetic_02_alternate_labels` | native-text | complete | 1.000 (10/10) | 1.000 (10/10) | 1.000 (4/4) | 1.000 (3/3) | 1.000 (17/17) |
| `synthetic_03_multipage` | native-text | complete | 1.000 (5/5) | 0.800 (4/5) | 1.000 (4/4) | 1.000 (1/1) | 1.000 (10/10) |
| `synthetic_04_scanned_image` | scanned-ocr | complete | 1.000 (7/7) | 1.000 (7/7) | 1.000 (4/4) | 1.000 (1/1) | 1.000 (12/12) |
| `synthetic_05_header_table_narrative_refs` | native-text | complete | 1.000 (9/9) | 1.000 (9/9) | 1.000 (6/6) | 1.000 (2/2) | 1.000 (17/17) |
| `synthetic_06_missing_fields_and_traps` | native-text | complete | 1.000 (3/3) | 1.000 (3/3) | 1.000 (4/4) | 1.000 (1/1) | 1.000 (8/8) |
| `real_cr185_2021` | native-glued-table | complete | 1.000 (9/9) | 1.000 (9/9) | 1.000 (18/18) | 1.000 (2/2) | 1.000 (29/29) |
| `real_fir242_2021_scanned` | scanned-ocr | complete | 1.000 (9/9) | 0.889 (8/9) | 1.000 (10/10) | 1.000 (1/1) | 1.000 (20/20) |
| `real_fir83_2020` | native-table | complete | 1.000 (9/9) | 0.889 (8/9) | 1.000 (8/8) | 1.000 (3/3) | 1.000 (20/20) |

## Mismatches (synthetic documents)

- `synthetic_01_digital_standard`: none
  - note: case.jurisdiction: only inside the narrative ('Sample Circle')
- `synthetic_02_alternate_labels`: none
- `synthetic_03_multipage`: none
  - note: case.court_name: only inside the narrative ('JMFC North')
- `synthetic_04_scanned_image`: none
- `synthetic_05_header_table_narrative_refs`: none
- `synthetic_06_missing_fields_and_traps`: none

Mismatch details for real documents are written to `tests/fixtures/private/ACCURACY_DETAILS.md` (gitignored) because they contain personal data. Shared reports only carry field names:

- `real_cr185_2021`: 0 mismatch(es)
- `real_fir242_2021_scanned`: 0 mismatch(es)
  - note: case.court_name: only inside the narrative
- `real_fir83_2020`: 0 mismatch(es)
  - note: case.court_name: only inside the narrative

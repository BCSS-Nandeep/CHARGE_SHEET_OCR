# Accuracy Report

Generated 2026-10-09 20:38 by `scripts/evaluate.py` at commit `9198568 + uncommitted changes` — OCR enabled (local PaddleOCR).

**These are measured counts on a small dataset, not a general accuracy claim.** Extractor confidence scores are not accuracy and are not used here. Every value is shown as `ratio (numerator/denominator)`; see `evaluation/metrics.py` for exact definitions.

Dataset: 9 document(s) evaluated — 3 real (ground truth transcribed from the source and awaiting human confirmation), 6 synthetic.

**Development-set caveat:** the real documents were used while building and fixing the parser, and the newer synthetic samples were written by the same author after the parser. High scores here show the pipeline handles these documents; they do not estimate accuracy on unseen templates. Measure on a held-out set of manually verified charge sheets before operational use.

## Aggregate

| Metric | All | Real documents | Synthetic |
|---|---|---|---|
| Document classification accuracy | 1.000 (9/9) | 1.000 (3/3) | 1.000 (6/6) |
| Case-field accuracy (incl. correctly blank fields) | 0.987 (78/79) | 0.967 (29/30) | 1.000 (49/49) |
| Case-field mapping precision | 1.000 (71/71) | 1.000 (26/26) | 1.000 (45/45) |
| Case-field recall | 0.986 (71/72) | 0.963 (26/27) | 1.000 (45/45) |
| Accused record precision | 1.000 (17/17) | 1.000 (7/7) | 1.000 (10/10) |
| Accused record recall | 1.000 (17/17) | 1.000 (7/7) | 1.000 (10/10) |
| Witness record precision | 1.000 (37/37) | 1.000 (25/25) | 1.000 (12/12) |
| Witness record recall | 1.000 (37/37) | 1.000 (25/25) | 1.000 (12/12) |
| Investigating-officer precision | 1.000 (9/9) | 1.000 (4/4) | 1.000 (5/5) |
| Investigating-officer recall | 1.000 (9/9) | 1.000 (4/4) | 1.000 (5/5) |
| Person names spelled exactly (matched records) | 1.000 (63/63) | 1.000 (36/36) | 1.000 (27/27) |
| Person attributes correct (matched records) | 1.000 (181/181) | 1.000 (128/128) | 1.000 (53/53) |
| Legal section precision | 1.000 (16/16) | 1.000 (6/6) | 1.000 (10/10) |
| Legal section recall | 1.000 (16/16) | 1.000 (6/6) | 1.000 (10/10) |
| Other-case FIR reference precision | 1.000 (4/4) | 1.000 (2/2) | 1.000 (2/2) |
| Other-case FIR reference recall | 1.000 (4/4) | 1.000 (2/2) | 1.000 (2/2) |
| Conflict detection precision | 1.000 (10/10) | 1.000 (9/9) | 1.000 (1/1) |
| Conflict detection recall | 0.833 (10/12) | 0.818 (9/11) | 1.000 (1/1) |
| Completeness (ground-truth items extracted correctly) | 0.993 (150/151) | 0.986 (68/69) | 1.000 (82/82) |
| OCR reference lines recognised (similarity >= 0.9) | 0.933 (28/30) | 0.909 (20/22) | 1.000 (8/8) |

## Per document

| Document | Layout | Extraction | case_fields | accused_recall | witnesses_recall | legal_recall | conflict_recall | completeness |
|---|---|---|---|---|---|---|---|---|
| `synthetic_01_digital_standard` | native-text | complete | 1.000 (11/11) | 1.000 (2/2) | 1.000 (2/2) | 1.000 (2/2) | — | 1.000 (18/18) |
| `synthetic_02_alternate_labels` | native-text | complete | 1.000 (10/10) | 1.000 (1/1) | 1.000 (2/2) | 1.000 (3/3) | — | 1.000 (17/17) |
| `synthetic_03_multipage` | native-text | complete | 1.000 (6/6) | 1.000 (3/3) | 1.000 (1/1) | 1.000 (1/1) | — | 1.000 (10/10) |
| `synthetic_04_scanned_image` | scanned-ocr | complete | 1.000 (7/7) | 1.000 (1/1) | 1.000 (2/2) | 1.000 (1/1) | — | 1.000 (12/12) |
| `synthetic_05_header_table_narrative_refs` | native-text | complete | 1.000 (9/9) | 1.000 (2/2) | 1.000 (3/3) | 1.000 (2/2) | 1.000 (1/1) | 1.000 (17/17) |
| `synthetic_06_missing_fields_and_traps` | native-text | complete | 1.000 (6/6) | 1.000 (1/1) | 1.000 (2/2) | 1.000 (1/1) | — | 1.000 (8/8) |
| `real_cr185_2021` | native-glued-table | complete | 1.000 (10/10) | 1.000 (4/4) | 1.000 (12/12) | 1.000 (2/2) | 0.500 (2/4) | 1.000 (29/29) |
| `real_fir242_2021_scanned` | scanned-ocr | complete | 0.900 (9/10) | 1.000 (2/2) | 1.000 (7/7) | 1.000 (1/1) | 1.000 (2/2) | 0.950 (19/20) |
| `real_fir83_2020` | native-table | complete | 1.000 (10/10) | 1.000 (1/1) | 1.000 (6/6) | 1.000 (3/3) | 1.000 (5/5) | 1.000 (20/20) |

## Mismatches (synthetic documents)

- `synthetic_01_digital_standard`: none
- `synthetic_02_alternate_labels`: none
- `synthetic_03_multipage`: none
- `synthetic_04_scanned_image`: none
- `synthetic_05_header_table_narrative_refs`: none
- `synthetic_06_missing_fields_and_traps`: none

Mismatch details for real documents are written to `tests/fixtures/private/ACCURACY_DETAILS.md` (gitignored) because they contain personal data. Shared reports only carry field names:

- `real_cr185_2021`: 2 mismatch(es)
  - conflict missed: accused[0].name
  - conflict missed: accused[3].arrest_status
- `real_fir242_2021_scanned`: 3 mismatch(es)
  - case.court_name: missing
  - ocr: page 2 reference line #17 not recognised
  - ocr: page 2 reference line #18 not recognised
- `real_fir83_2020`: 0 mismatch(es)

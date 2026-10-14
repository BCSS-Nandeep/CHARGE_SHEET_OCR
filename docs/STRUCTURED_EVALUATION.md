# Structured extraction evaluation

Generated 2026-10-14 13:49. Model `nanonets-ocr2-3b` (transcription: `nanonets`, fallback: `none`). Counts are on a small development set: **not a general accuracy claim**.

Exact match is strict (see `evaluation/structured_metrics.py`). Ground truth for real documents was transcribed by the assistant and still needs human confirmation.

## How to read this (added by hand)

* Six synthetic documents (9 pages), one run each, real model, final code. Nothing here shows that accuracy
  *improved* over the previous pipeline: the structured stage is a different, harder task (right value in the
  right named field) and the previous as-printed pipeline, scored by the old "found somewhere" metric on the same
  runs, finds every expected value (table at the end). The structured result adds field placement, evidence and
  review flags; it does not replace the as-printed fields.
* The 3B model is good at case-level identifiers (41/45 exact; the misses are flagged or are real in-document
  ambiguities such as two different jurisdictions printed on two pages) and at finding people (accused 10/10,
  witnesses 10/12, officers 5/5 when extra text in the name field is tolerated). It is weak at field *boundaries*:
  it keeps "SI" / "Dr." / "s/o ..." in `name` (strict names 7/10, 5/12, 0/5) and splits "u/s 302, 34 IPC" between
  `act` and `section` wrongly (strict legal 0/10, loose 5/10). Those values are kept as printed and the validator
  flags them (`name_contains_relation`, `act_contains_digits`, `serial_format`); they are not cleaned up.
* `case_fabricated` counts blank-in-ground-truth fields given a value *shown as extracted* (1/4); the other 3/4 got a
  value that is flagged unverified / ambiguous. `values_with_evidence` is the share of non-empty values that are
  grounded in the page text (69.8 %); it is a measure of evidence, not of correctness.
* An earlier run of the same documents with the first prompt (before sections were narrowed per page and the format
  example was added) scored accused names 2/10 and case values 40/45 and took up to 7 minutes per page; the numbers
  below are the final code only.
* Timings depend on whether another application was using the shared server (the same page took 94-444 s across runs).
* Real-document ground truth was transcribed by the assistant and is unconfirmed; real-document counts are reported
  separately (see the end of this file, if present) and never include values.

## Real documents (3 private documents, 9 pages) - BEFORE the dense-page fix

One run each on 2026-10-14, counts only (ground truth unconfirmed). Results were poor: case values exact 5/27,
critical identifiers/dates 3/12, accused names 1/7, witnesses 0/25, officers 1/4, legal sections 2/6; all three
documents ended `partial` (one with structured mapping `failed`) and took 9-31 minutes each. Cause found in the
server log: 24 mapping answers ended exactly at the 1500-token cap - on dense real pages the model fills every field
it is shown, so the answer was cut off. Fix: dense pages are now asked one section per request, a cut-off falls back
to per-section requests, and a partly mapped page is `partial`, not `failed` (see README). **The real documents were
not re-evaluated after the fix** (the shared CPU server is too slow to spare), so the fix is covered by automated
tests with a fake model only. (A pre-fix re-run of the smallest document came back `complete`, unlike its first run,
which shows results vary from run to run on dense pages.)

## Per document (synthetic only)

| document | pages | seconds | extraction | structured | case exact | critical exact | names (acc/wit/IO) | legal |
|---|---|---|---|---|---|---|---|---|
| synthetic_01_digital_standard | 2 | 151 | complete | extracted | 0.909 (10/11) | 1.000 (5/5) | 1.000 (2/2) / 0.500 (1/2) / 0.000 (0/1) | 0.000 (0/2) |
| synthetic_02_alternate_labels | 1 | 94 | complete | extracted | 1.000 (10/10) | 1.000 (6/6) | 1.000 (1/1) / 0.500 (1/2) / 0.000 (0/1) | 0.000 (0/3) |
| synthetic_03_multipage | 3 | 110 | complete | extracted | 1.000 (5/5) | 1.000 (2/2) | 0.667 (2/3) / 1.000 (1/1) / n/a (0/0) | 0.000 (0/1) |
| synthetic_04_scanned_image | 1 | 101 | complete | extracted | 0.857 (6/7) | 0.750 (3/4) | 1.000 (1/1) / 0.500 (1/2) / 0.000 (0/1) | 0.000 (0/1) |
| synthetic_05_header_table_narrative_refs | 1 | 152 | complete | extracted | 0.889 (8/9) | 0.800 (4/5) | 0.000 (0/2) / 0.000 (0/3) / 0.000 (0/1) | 0.000 (0/2) |
| synthetic_06_missing_fields_and_traps | 1 | 102 | complete | extracted | 0.667 (2/3) | 1.000 (2/2) | 1.000 (1/1) / 0.500 (1/2) / 0.000 (0/1) | 0.000 (0/1) |

## Totals

| metric | result |
|---|---|
| case_exact | 0.911 (41/45) |
| critical_exact | 0.917 (22/24) |
| case_missing | 0.022 (1/45) |
| case_wrong | 0.067 (3/45) |
| case_fabricated | 0.250 (1/4) |
| case_fabricated_flagged | 0.750 (3/4) |
| accused_names | 0.700 (7/10) |
| accused_names_loose | 1.000 (10/10) |
| accused_attrs | 1.000 (18/18) |
| accused_spurious | 0.300 (3/10) |
| witnesses_names | 0.417 (5/12) |
| witnesses_names_loose | 0.833 (10/12) |
| witnesses_attrs | 1.000 (5/5) |
| witnesses_spurious | 0.583 (7/12) |
| investigating_officers_names | 0.000 (0/5) |
| investigating_officers_names_loose | 1.000 (5/5) |
| investigating_officers_spurious | 1.000 (6/6) |
| legal_sections | 0.000 (0/10) |
| legal_sections_loose | 0.500 (5/10) |
| values_with_evidence | 0.698 (157/225) |

## Previous as-printed pipeline on the same runs (different measure)

The as-printed fields have no fixed names, so the old metric only asks whether an expected value appears *somewhere* in a field. It is not comparable to exact-match-in-the-right-field above; it is shown only for context.

| metric | result |
|---|---|
| case_values | 1.000 (45/45) |
| names | 1.000 (27/27) |
| legal_sections | 1.000 (10/10) |
| completeness | 1.000 (82/82) |

## Mismatches

- **synthetic_01_digital_standard**: 
  - case.jurisdiction: wrong (expected 'Sample Circle', got 'Sample City')
  - witnesses[1]: name not found exactly ('Mehta')
  - investigating_officers[0]: name not found exactly ('Anita Verma')
  - legal: IPC 302 not found as one act/section record
  - legal: IPC 34 not found as one act/section record
- **synthetic_02_alternate_labels**: 
  - witnesses[1]: name not found exactly ('Bank Manager')
  - investigating_officers[0]: name not found exactly ('K. Rao')
  - legal: IPC 420 not found as one act/section record
  - legal: IPC 406 not found as one act/section record
  - legal: IT Act 66 not found as one act/section record
- **synthetic_03_multipage**: 
  - case.fir_date: expected blank but a value was given, flagged unverified ('2023-01-01')
  - accused[2]: name not found exactly ('Unknown person')
  - legal: IPC 379 not found as one act/section record
- **synthetic_04_scanned_image**: 
  - case.year: missing (expected '2021', got None)
  - witnesses[1]: name not found exactly ('Ravi')
  - investigating_officers[0]: name not found exactly ('L. Thomas')
  - legal: IPC 307 not found as one act/section record
- **synthetic_05_header_table_narrative_refs**: 
  - case.fir_date: wrong (expected '14.07.2024', got '30.09.2024')
  - accused[0]: name not found exactly ('Ravi Teja')
  - accused[1]: name not found exactly ('Salim Khan')
  - witnesses[0]: name not found exactly ('Meera Iyer')
  - witnesses[1]: name not found exactly ('Joseph Dsouza')
  - witnesses[2]: name not found exactly ('R. Varma')
  - investigating_officers[0]: name not found exactly ('R. Varma')
  - legal: BNS 303(2) not found as one act/section record
  - legal: BNS 317(2) not found as one act/section record
- **synthetic_06_missing_fields_and_traps**: 
  - case.district: expected blank but a value was given, flagged ambiguous ('Riverbend')
  - case.police_station: wrong (expected 'Riverbend', got 'Riverbend PS')
  - case.charge_sheet_number: expected blank but a value was given, flagged ambiguous ('1')
  - case.submission_date: expected blank but a value was given, presented as extracted ('03-01-2025')
  - witnesses[1]: name not found exactly ('Lata Devi')
  - investigating_officers[0]: name not found exactly ('P. Kumar')
  - legal: IPC 379 not found as one act/section record

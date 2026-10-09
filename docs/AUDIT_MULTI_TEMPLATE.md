# Audit & Fix Report — Multi-Template Charge Sheet Extraction

## 1. Confirmed sample-specific hardcoding (removed)

| Location | Issue | Classification | Action |
|---|---|---|---|
| `app/services/entity_extractor.py` — `find_name_age_mentions` | Regex hardcoded `Aerrolla\|Errolla Rajender Goud` | **Genuine sample-specific hardcoding** | Removed; replaced with general surname + edit-distance variant detection |
| `app/services/extraction_service.py` — lab findings | Regex required `Alprazolam` | **Sample-specific** | Generalized to FSL/chemical-examiner narrative patterns |
| `app/services/conflict_service.py` | Comment referencing Aerrolla/Errolla | Comment only | Cleaned |
| `app/services/form_parser.py` docstring | Example text `FIR. No. 83/2020` | Documentation example only | Neutralized |
| `tests/fixtures/ground_truth_fir83_2020.json`, `scripts/evaluate_fir83.py`, `tests/test_fir83_extraction.py` | FIR 83 expected values | **Legitimate evaluation fixtures** (not runtime) | Kept |
| `scripts/debug_fir83.py` | Hardcoded document ID for debugging | Debug utility only | Not used at runtime |

**Legitimate general rules retained:** synonym dictionaries, CrPC/NDPS/IPC section regexes, form-column heuristics, glued-text softening for common charge-sheet phrases (not tied to one FIR).

Regression guard: `tests/test_no_sample_hardcoding.py` fails if forbidden sample literals reappear under `app/services/`.

## 2. Screenshot failure root causes

### A. Text PDF (`2022-05-02…` / Cr.No.185/2021) — wrong field mapping
1. **Substring synonym matching** in `best_canonical_match`: short tokens (`year`, `occ`, `sho`, `ps`) matched inside longer strings (`years`, `occurrence`, `Ashok`), mapping witness/person lines into `police_station` / `year` / `fir_date`.
2. **Glued digital text** (`Dist.Medak PS:HAVELIGHANAPUR`, `Particularsofthewitnesses…`) broke label detection; combined Dist+PS lines assigned the PS value to district.
3. **Address extractor** consumed narrative after `R/o` until end-of-string.
4. **Witness splitter** treated ages (`34 yrs`) as new serial entries.
5. **Whitespace softener** collapsed 2+ spaces, breaking space-aligned KV forms on other templates.

### B. Scanned PDF (`2021-11-26…`) — empty extraction / `unknown`
1. Native text length = 0 on all pages → OCR required.
2. **PaddleOCR not installed** locally → pages skipped.
3. Classification correctly falls to `unknown` with low confidence when OCR is missing (after fix: explicit `OCR_REQUIRED` warning; no invented fields).

### C. Confidence vs quality
Confidence reflects extraction heuristics, **not** measured accuracy. UI label updated to “Confidence (not accuracy)”. Validation no longer marks person-bleed values as `VALID` for core case fields.

## 3. Fixes applied (general, not per-PDF)

- Word-boundary synonym matching + person-label rejection (`field_mapping_service.py`, `text_normalize.py`)
- Glued-text softener that preserves column alignment (`text_normalize.py`)
- Combined Dist+PS line parser; stricter scalar value quality (`form_parser.py`)
- Structured A1/A2/A3 accused + bounded witness extraction; address narrative cut-off (`entity_extractor.py`)
- Broader NDPS/Excise section patterns tolerant of spacing (`legal_section_parser.py`)
- OCR availability messaging; optional local Tesseract fallback beside PaddleOCR (`ocr_service.py`)
- Hardcoding regression test

## 4. Test results

| Suite | Result |
|---|---|
| `pytest` | **25 passed** |
| FIR 83/2020 ground-truth evaluation | Scalar/sections/accused/witnesses/conflicts **1.000** on that dataset (see `docs/ACCURACY_REPORT_FIR83.md`) |
| Offline network guard | PASS |
| Hardcoding AST guard | PASS |

### Smoke reprocess (uploads/)

| PDF | Type | District | FIR | Accused | Witnesses | Sections | Notes |
|---|---|---|---|---|---|---|---|
| FIR 83/2020 text | charge_sheet | Medak | 83/2020 | 1 | 6 | 3 | Age 33 vs 32 + name spelling conflicts unresolved |
| Cr.185/2021 text (glued) | charge_sheet | Medak | 185/2021 | 3+ | 12 | 2 (NDPS) | Improved vs screenshot; witness dedupe can still improve |
| 2021-11-26 scanned | unknown | — | — | 0 | 0 | 0 | **OCR not installed** — expected empty until local OCR |

**Do not claim 100% accuracy across all PDFs** without per-document ground truth for each file. FIR 83 metrics apply only to that verified dataset.

## 5. Remaining limitations

- Local OCR must be installed for scanned PDFs (`pip install paddlepaddle paddleocr` or Tesseract + `pytesseract`).
- Some glued exports still over-segment witnesses; further layout-agnostic tuning needed.
- Fourth accused (A4) on Cr.185/2021 may still be missed when text is severely concatenated.
- Authorization remains off by default for local use (`ENABLE_AUTH=false`); enable for shared deployments.

## 6. Security

Processing remains local/offline-first. No document content is sent to external AI APIs. Logs avoid full narratives/PII (`LOG_PII=false`).

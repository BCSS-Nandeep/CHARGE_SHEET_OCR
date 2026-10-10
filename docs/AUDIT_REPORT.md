# Audit & Remediation Report — Charge Sheet Extraction Service

Date: 2026-10-09 · Baseline: commit `9198568` · Measured results: [ACCURACY_REPORT.md](ACCURACY_REPORT.md)

## 1. Architecture and data flow (after remediation)

```text
POST /upload ──► validate (chunked size limit, %PDF header) ──► UPLOAD_DIR/<uuid>_<name>.pdf
                 └► background worker (processing_service)
PdfService      PyMuPDF text + positioned lines + ruled tables (find_tables, row positions)
OCR (per page)  pages without a text layer → PNG in memory → PaddleOCR (boxes) / Tesseract
LayoutService   OCR boxes → header tables (label row over value row), row anchors (serials),
                column cells → table rows shaped like find_tables rows + reading-order text
FormParser      document = positioned items (table rows, header pairs, free lines) in reading order
                ├ zoning: form zone | narrative zone (brief-facts heading / salutation / prose)
                ├ form rows: serial + label + value(s); list items (accused/witness) by serial sequence
                ├ label→field by meaning (synonyms, compact match for glued text, coverage scoring)
                ├ generic labels ("Date", "Dt.", "On") resolved from the neighbouring field
                └ narrative: FIR/crime references, standalone "Label: value" lines (non-identifiers only)
select_fields   one value per field; disagreeing form values → SourceConflict + alternates
EntityExtractor person entries split by A1/A-1)/numbered markers; attribute values bounded by the
                next marker; designations, roles, remarks, ditto; narrative person mentions
LegalParser     section grammar; roles charged/registered/altered_from/procedural/reference;
                same section under different act variants → SourceConflict
ConflictService narrative mentions matched to one person (parent + name) → name/age/parent conflicts
Validation      calendar dates (ISO normalised), FIR format, chronology, FIR-year consistency,
                phone/ID/age shape; confidence weighted by validation status
ExtractionStatus complete | partial (page/stage failed) | failed (no usable text)
processing      failed extraction → document FAILED (diagnostics kept); corrections re-applied
                only when the source value is unchanged; reprocess resets verification
API / UI        same persisted payload; 422 + diagnostics for failed extraction
```

## 2. Confirmed defects (all fixed unless marked)

Severity: **C** critical (wrong data shown as correct / status lies), **H** high, **M** medium.

| # | Sev | Where (baseline) | Defect | Root cause | Fix |
|---|---|---|---|---|---|
| 1 | C | `processing_service.process_document`, `validation_service.validate` | Scanned PDF marked **completed** with confidence 0.5 although OCR produced nothing | No extraction status; `(f.extraction_confidence or 0.5)` turned 0.0 into 0.5 | `extraction_status` + per-stage results; failed extraction → document `failed`; confidence uses real values |
| 2 | C | `ocr_service.recognize_png` | PaddleOCR is installed with cached models but every page failed: `ConvertPirAttribute2RuntimeAttribute not support` | Paddle 3.x oneDNN kernels; constructor did not set `enable_mkldnn` | `OCR_ENABLE_MKLDNN=false` default; verified on all 4 scanned pages |
| 3 | C | `ocr_service.recognize_png` / `extraction_service` | OCR engine errors reported as "OCR produced no text" | exceptions swallowed, `("", 0.0)` returned | `OcrResult.error`, surfaced in warnings, page summaries and `error_message` |
| 4 | H | `ocr_service.tesseract_available` | Tesseract reported available when only the `pytesseract` wrapper was installed (binary missing) | import check instead of binary check | requires the binary (`PATH`, standard path or `TESSERACT_CMD`) |
| 5 | C | `form_parser.parse_inline_kv_lines` / `dedupe_raw` | `crime_number` = 293/2021 and `submission_date` = 02.10.2021 taken from narrative sentences about other cases | whole document text parsed as key/value lines; best-of selection | form/narrative zoning; narrative FIR numbers → `related_case_references` |
| 6 | C | `form_parser.dedupe_raw` | Different values for one field silently resolved | "best" candidate kept, others discarded | `select_fields` keeps alternates and raises `SourceConflict` |
| 7 | H | `text_normalize.soften_pdf_text` | "I.O." became "I. O.", so the IO label never matched → IO missing on both real text PDFs | `([A-Za-z])\.([A-Z])` split abbreviations | split only `x.Word` and `Label.Number` |
| 8 | H | `text_normalize.soften_pdf_text` | Words broken by marker splitting ("Occurred"→"Occ urred", "occupied", "Manager", "Villagers") | case-insensitive `Age|Caste|Occ` split before any letter | word markers split only before capital/digit/colon |
| 9 | C | `entity_extractor.parse_person_blob` | Age 2 from "--- PAGE 2 ---"; ages from "village …"; role "io" from "conf**io**n" | regexes without word boundaries | word-bounded markers |
| 10 | H | `entity_extractor.parse_person_blob` | Occupation/address absorbed roles, phones, narrative ("…Haveli Ghanapur 9603118476 Panch for Scene…") | values ran to the next comma or a narrative keyword list | values end at the next attribute marker / phone / ID; roles from the role column |
| 11 | H | `extraction_service.process` | Every accused/witness tagged source page 1 | hardcoded `page=1` | page from the row / narrative offset |
| 12 | H | `entity_extractor.extract_accused_from_block` | A4 ("A4TakMahendar…") missed; "S/." parentage missed; "(A1&A2 were arrested…)" remarks merged into addresses | split regex required punctuation after A-number; remarks not modelled | A-marker without punctuation, masked parentheses, remarks → `arrest_status` attached to referenced accused |
| 13 | H | `conflict_service.apply_conflicts_to_fields` | A conflict about one accused marked **every** accused's age/name as conflicting | loop over all accused | conflicts addressed by canonical name (`accused[0].age`) |
| 14 | H | `entity_extractor.find_name_age_mentions` | Real variant (Errolla vs Aerrolla) missed; bogus variant "that the accused …" and bogus age "2" reported — and the accuracy report scored this as 1.000 | name window included prose; no parent matching | narrative mentions parsed like list entries and matched by parent + name |
| 15 | H | `legal_section_parser.parse_legal_sections` | "8(c), 20(b)(ii)(A)" returned as one entry plus a duplicate; section changes in the narrative ignored; procedural CrPC sections treated as charges | per-document regexes; fixed region | grammar + split lists + role tagging; procedural/boilerplate separated |
| 16 | M | `routes_results.patch_fields`, UI | Corrections to unknown fields silently ignored; UI "Save corrections" marked **every** field corrected; legal sections shared one canonical name (`legal.section`) so corrections hit the last one | no validation; UI sent all inputs; non-unique names | 400 on unknown names; UI sends changed fields only; unique indexed names (test enforced) |
| 17 | M | `routes_documents.reprocess_document` | Reprocessed document stayed "verified"; stored corrections no longer reflected in results | verification flag not reset; corrections not re-applied | reset on reprocess; corrections re-applied when the source value is unchanged, else reported |
| 18 | M | `processing_service` | Documents stuck in `processing` forever after a restart; `PROCESSING_TIMEOUT_SECONDS` never used | no recovery; no budget | startup recovery marks them failed; OCR time budget → `partial` |
| 19 | M | `pdf_service` | `find_tables()` output computed and discarded | — | ruled tables are now the primary structure for native PDFs |
| 20 | M | `extraction_service` | 1200-char page-1 scan for sections; "Charge sheet" title taken as a field label | fixed positions; weak label threshold | acts field/narrative grammar; label-only lines need ≥0.95 match |
| 21 | M | `tests/conftest.py` | Every test run rewrote the 4 tracked fixture PDFs (non-deterministic bytes) | generator ran unconditionally | generate only when missing |
| 22 | M | `tests/test_field_mapping.py` | Tests exercised functions not used by the pipeline (`extract_kv_candidates`, `extract_acts_sections`, `extract_person_blocks`) | dead code | dead code removed; tests target the runtime path |

## 3. Sample-specific hardcoding and overfitting

Confirmed in runtime code (baseline) and removed:

| Location | Finding | Classification |
|---|---|---|
| `legal_section_parser.parse_legal_sections` — `explicit` list | Regexes for exactly `188 IPC`, `34(a) A.P./T.S. Excise Act`, `8(c) r/w 22 NDPS`, `8(c),20(b)(ii)(A) NDPS` — the sections of the two real sample documents | **Sample-specific** |
| `form_parser._value_quality` | Penalty words `toddy`, `land owner`, `haveli` (a police-station name from a sample) | **Sample-specific** |
| `entity_extractor.extract_witnesses_from_block` | Serial "303"→"03" special case (an artefact of one PDF) | **Sample-specific** |
| `form_parser.extract_section_blocks`, `legal_section_parser`, `entity_extractor` | Fixed form column numbers (`04 act`, next column `05`, `15 final`) | Fixed-numbering assumption |
| `extraction_service.process` | Legal sections from the first 1200 characters of page 1 | Fixed-position assumption |
| `extraction_service.process` | Seizure regex limited to liquid/weight units; FSL regex | Narrow but general (replaced by labelled fields + sentence-level lab finding) |

Legitimate general rules kept: synonym dictionaries, glued-phrase normalisation for common charge-sheet
vocabulary, act-name patterns (IPC/BNS/BNSS/CrPC/NDPS/state acts + generic "… Act").
Template reuse: `TemplateService.match` only records an exact structural fingerprint ("previously seen");
no stored mapping is ever applied, so no incompatible template can be reused. Fingerprints are now computed
from form labels only.

Guard: `tests/test_no_sample_hardcoding.py` derives forbidden tokens from **every** ground-truth file
(names, places, FIR numbers, dates) and scans all of `app/` including comments; it also checks that results
do not depend on the file name. While writing this remediation, example values from the real documents had
crept into code comments — the test caught them and they were replaced.

## 4. OCR and scanned documents

- Installed: paddlepaddle 3.3.1, paddleocr 3.7.0, PP-OCRv6 models cached locally; `pytesseract` installed but
  **no Tesseract binary**.
- Baseline: every OCR call failed (oneDNN kernel error), the failure was logged as "no text", and the
  document was marked completed with confidence 0.5 and an empty result.
- Now: OCR runs locally (≈60 s/page on this CPU at 200 DPI) with the network guard active; text boxes are
  turned into table rows and reading-order text; per-page source, OCR engine and confidence are recorded.
  The real 4-page scan (header table + ruled form, photographed with a curled page edge) extracts all form
  fields except the court name (OCR missed the curved heading line) — see the accuracy report.
- Limits: no deskew/dewarp (curled page edges degrade the rows next to the fold), English model only
  (`OCR_LANG`), no handwriting support.

## 5. Accuracy methodology review

Baseline `docs/ACCURACY_REPORT_FIR83.md` (removed) reported 1.000 on every metric for one document. Problems:
- "contains" matching in both directions (any substring of the expected value counted as correct);
- witness "accuracy" was recall only — extra or garbage witness records were not penalised;
- the conflict metric was satisfied by the primary name itself, so a bogus variant scored 1.000;
- the investigating-officer field was in the ground truth but never evaluated (it was not being extracted);
- extractor confidence was presented next to accuracy;
- a single document, also used during development.

New harness (`evaluation/`, `scripts/evaluate.py`): ground truth per document (synthetic committed, real
documents gitignored), strict normalised comparison (dates as calendar dates, names by compact equality),
precision **and** recall for every record type, expected-blank fields penalised when a valid value is produced,
conflicts matched by field and exact value set, OCR line recognition for scanned documents, and every metric
reported as numerator/denominator. Real-document mismatches go to a private file only.

**Caveat:** the three real documents were used to develop the fixes, and the two new synthetic samples were
written after the parser by the same author. The measured figures show the pipeline is correct on these
documents; they are not an estimate of accuracy on unseen templates. A held-out set of manually verified
charge sheets from other districts/form versions is needed for that.

## 6. Security review (details in [SECURITY.md](SECURITY.md))

Fixed: auth ignored `API_KEY_HEADER`; example key accepted outside production; no user identity (verification
not attributable); auth-off mode served any network client; no API-key field in the UI (unusable with auth
on); network guard treated all `172.*` as private and missed `connect_ex`; PaddleX model-hoster probes; upload
read fully into memory before the size check; path check by string prefix; retention left extracted personal
data in the database; no security headers; debug scripts dumped full document text into the project folder;
real personal data in committed test fixtures and docs (removed from the working tree — **still in git history**).
Not addressed: encryption at rest, TLS, multi-tenant isolation (single-tenant by design; documented).

## 7. Files changed

New: `app/services/layout_service.py`, `app/services/value_parsers.py`, `evaluation/`, `scripts/evaluate.py`,
`app/templates/base.html`, ground truth under `tests/fixtures/ground_truth/` (+ gitignored `private/`), two
synthetic fixtures, tests `test_layout`, `test_form_parser`, `test_entities`, `test_legal_sections`,
`test_conflicts_validation`, `test_ocr_service`, `test_ground_truth`.
Rewritten: `form_parser`, `entity_extractor`, `legal_section_parser`, `conflict_service`, `validation_service`,
`extraction_service`, `ocr_service`, `security/auth`, `network_guard`, `static/js/app.js`, templates.
Modified: schemas (v1.1.0, additive), models (+ additive migration in `db.init_db`), repositories, routes,
`pdf_service`, `processing_service`, `retention_service`, `text_normalize`, `field_mapping_service`,
`field_synonyms`, config, README, SECURITY, `.env.example`.
Removed: `scripts/debug_fir83.py`, `scripts/debug_uploads.py`, `scripts/evaluate_fir83.py`,
`scripts/measure_accuracy.py`, `tests/test_fir83_extraction.py`, old ground-truth JSON files,
`docs/ACCURACY_REPORT_FIR83.md`, `docs/AUDIT_MULTI_TEMPLATE.md` (superseded, contained incorrect claims).

## 8. Same-template robustness (known templates, different data)

`scripts/evaluate_variants.py` generates documents of the three known templates with different data,
substituted in the extracted text, table cells and OCR boxes so every layout quirk of the real template
is kept. The data that changes:

- district, police station, FIR number, year and dates, charged sections;
- every person's name, parentage, age, occupation, village, phone and Aadhaar number;
- spelling-variant pairs (re-derived from the new names);
- structure: 1–3 witnesses added or removed, accused removed.

Each variant gets its own ground truth.

Run on 2026-10-09: **450 / 450 variants fully correct** (150 per template: native ruled table, glued-text
table, scanned/OCR). The run found and fixed one real defect: narrative-mention matching linked the wrong
person when parent names shared common endings (similarity 0.75), creating false conflicts. Matching now
needs ≥ 0.80 parent and ≥ 0.80 name similarity.

Limits:
- Substitution keeps the writer's formatting conventions (e.g. `Age: 33yrs`), so a different writing
  style on the same form is not exercised.
- The scanned template reuses the original OCR boxes, so OCR recognition of new names is not exercised.
- Multiple accused in template A's single-accused cell format is not covered (format unknown).

## 9. Remaining limitations

- Conflicts that need narrative understanding are not detected: a person mentioned without parentage
  (e.g. a spelling variant of an accused's name in passing), and dates of events (arrest date in a remark vs.
  the narrative).
- Glued-text exports keep glued words in values (e.g. court name `JUDICIALFIRSTCLASSMAGISTRATE,MEDAK`);
  values are faithful to the source but not re-spaced.
- OCR: no deskew/dewarp; rows at a curled page edge can be mis-assigned; English only.
- Seized property / lab results are only taken from labelled fields and one narrative sentence.
- Single-tenant authorization; API keys rather than an identity provider.
- Accuracy has not been measured on a held-out set of unseen templates (see §5).

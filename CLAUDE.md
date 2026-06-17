# OfferVerify — Working Notes & Session Handoff

> Handoff doc for continuity across sessions. Read this first.
> Last updated: 2026-06-17.

---

## 1. What the project is

Indian **offer-letter fraud detection**. A PDF runs through a pipeline that
extracts fields → runs free checks (DNS, web presence, dates, salary math) →
makes **one** paid Claude vision+text call → scores **9 pillars out of 100** →
applies verdict + gates → returns `AnalysisResult` (and saves an HTML report for
the inbox flow).

- **Stack:** FastAPI, served on **port 8003**. LLM = **Anthropic Claude**.
- **Model:** `.env` sets `claude_model = claude-sonnet-4-6` (this OVERRIDES the
  `config.py` default of `claude-3-5-sonnet-20241022`). The live model is **Sonnet 4.6**.
- **Entry points** (all converge on `_run_pipeline` in [main.py](main.py)):
  `POST /analyze` (ad-hoc), `POST /analyze-from-inbox` (saves report), `/batch/*` + cron.
- **Extraction is regex-first** ([app/field_extractor.py](app/field_extractor.py)),
  Claude fallback only when `is_low_confidence()` is true (cost saver).
- See [PIPELINE.md](PIPELINE.md) for the full stage-by-stage map (still accurate).

## 2. HARD CONSTRAINTS (do not violate)

- **DO NOT change output-parameter WEIGHTS or verdict thresholds.** The 11/11/12…
  pillar weights and the 80 / 51 cutoffs in [app/config.py](app/config.py) are fixed.
  Our job is to make the *signals/inputs* to those parameters **stronger/cleaner**,
  not to re-weight them.
- **DO NOT break working letters.** Every change is validated with the free corpus
  audits below before being trusted.
- **CREDITS ARE LOW.** Avoid Claude/API calls. ALL extraction/parsing/scoring audits
  are **free** (pdfplumber + regex + DNS/web scrape, no API). Only `verify_sample.py`
  and a real pipeline run cost credits. **Cap any paid test at ~4 letters.**
- **DO NOT re-run the full corpus or overwrite existing `processed/` reports.**
- **DO NOT delete any offer-letter PDFs or their HTML reports.**
- Work **one item at a time** (limit-conscious).

## 3. Present condition

**Input layer is clean** (315-letter free audit: 0 crashes, 0 dirty fields). The
**AI prompt and the visual / forgery / scoring layer have now been hardened** this
session (see §4): prompt-injection fenced, Image & Signature rubrics rewritten, a
**composite-forgery detector** added (catches the Ajay blank-letterhead-overlay class),
**edit-laundering** caught (Gate 3b), and the **displayed score now follows a fraud-gated
verdict** — a confirmed fake can no longer show a higher number than a clean letter.
All verified **free** across the corpus; the detection layer was **confirmed live** on a paid
run (Suresh CLEAN→MANUAL REVIEW 76, Suresh TAMPERED→SUSPICIOUS/50 clamped — fake now scores
below clean). **Cost work added this window:** prompt caching (shipped, §4) and the **Batch API**
(built opt-in, §6 item 0 — the real −50% lever, pending one small paid validation batch).
Cost ≈ **₹7–11/letter** today (Sonnet 4.6, $3 in/$15 out per 1M); Batch API → **~₹3.5–5.5/letter**.
**Server must be RESTARTED** to load this window's code (caching, batch path, all §4 detection).

## 4. What changed — latest session (2026-06-16): vision, forgery & scoring

**Prompt-injection hardening** ([app/ai_client.py](app/ai_client.py))
- Raw PDF text fenced as UNTRUSTED in both the extraction and scoring prompts; an injection
  attempt is turned INTO a fraud signal (Text Tampering + CHECK 15).

**Composite / layered-forgery detection (NEW)** — *the Ajay "blank-letterhead overlay" case*
- [app/pdf_reader.py](app/pdf_reader.py) `_scan_composite_forgery()` (fitz-only, free): flags a
  page with NO text layer + a big background image + ≥1 small pasted image (signature/stamp) +
  heavy vector overlay + 0 fonts. Validated **1/422, 0 false positives**.
- [app/models.py](app/models.py): `RawPDFData.composite_artifacts`.
- [app/rules.py](app/rules.py) **Gate 3c** → caps verdict to SUSPICIOUS.
- [app/ai_client.py](app/ai_client.py): Image Tampering **Q5** forces a composite to ≤2/11.

**Edit-laundering (NEW)** — *iLovePDF date-edit that scored HIGHER than the original*
- [app/rules.py](app/rules.py) **Gate 3b**: an online-editor footprint AND an edit artifact
  (floating date) TOGETHER → caps to SUSPICIOUS. Both required (a plain re-save is ignored).

**Score-clamp (NEW)** ([app/rules.py](app/rules.py))
- When a FRAUD gate fires (Gate 2 / 3 / 3b / 3c) the displayed score is pulled into the capped
  verdict's band (SUSPICIOUS→≤50, MANUAL REVIEW→≤79), so a laundered fake can no longer show a
  higher NUMBER than a clean original. **Gate 1 (visual incomplete) is EXCLUDED** — "couldn't
  verify" never lowers the number (fail-loud). No weights/thresholds changed.

**Vision — signature close-up (Option A)** — *moved/pasted sig was invisible at ~108 DPI*
- [app/pdf_reader.py](app/pdf_reader.py) `_render_signature_crop()`: high-res crop of the lower
  signature zone (~4× sharper), sent as the FINAL image to Claude. `RawPDFData.signature_crop`.
  +1 image (~½¢) per letter.

**AI rubric rewrites** ([app/ai_client.py](app/ai_client.py))
- **Image Tampering**: rewritten — two disqualifiers (composite forgery / stitched non-document
  content), **GPS checks removed entirely**, composite forces ≤2/11.
- **Signature Tampering**: recalibrated — a **pasted / scanned / digital signature is LEGITIMATE**
  (normal Indian HR). Only ABSENCE, visible manipulation, or a composite forgery scores low.

**Bug fixes**
- `job_title` "graduation": "post-graduation" was parsed as a `Post:` label. Fixed the dash
  separator + added a quoted-title capture + education-word reject list
  ([app/field_extractor.py](app/field_extractor.py)); `field_audit.py` now guards it. 0/315.
- "Document Processing Issue" flag ([app/rules.py](app/rules.py)): success/enhancement warnings
  (signature close-up, composite/tamper scans, page renders) are no longer mislabeled as "partial
  data". Only genuine errors / OCR-fallback / missing-text-layer surface.

**Saved-report archive (NEW)**
- `/analyze` now writes a standalone HTML report to **`saved_reports/`** (separate from the
  disposable `reports/`), mounted at `/saved-reports`. [app/report_writer.py](app/report_writer.py),
  [main.py](main.py), [.dockerignore](.dockerignore).

**Prompt caching (NEW — cost)** ([app/ai_client.py](app/ai_client.py))
- `analyze_letter` prompt split into a **byte-stable static instruction block (~5,128 tok)** sent
  with `cache_control` + a **per-letter data block** (images, online ctx, offer JSON) sent after.
  `[CACHE]` telemetry logged. **No rules/weights/thresholds changed** — model gets the same info,
  rules-first. Paid-verified on both Suresh letters: 2nd call showed `cache_read=5128` (prefix from
  cache at ~0.1×); CLEAN stayed MANUAL REVIEW (71→76, AI variance), TAMPERED correctly SUSPICIOUS/50
  (Gate 3b + clamp). **Saving is modest (~₹1–1.5/letter at scale)** — output tokens (₹3–4) and images
  (₹3) dominate and can't be cached. **The real cost lever is the Batch API (§6).**

---

## 4b. Earlier session — input-layer hardening (still in effect)

**Housekeeping**
- [requirements.txt](requirements.txt): removed unused `pytesseract`, `Pillow`, `python-dotenv`.
- [Dockerfile](Dockerfile): dropped tesseract packages (OCR uses Claude Vision, not Tesseract).
- Added [.dockerignore](.dockerignore) (keeps `.env`, `.venv`, PDFs, reports out of the image).
- Removed dead code: `company_online_score` field; unused params on `analyze_images`/`analyze_letter`; stray imports.

**Date extraction** ([app/field_extractor.py](app/field_extractor.py)) — *bug: joining date 2017-07-01 / -3150 day gap*
- Rewrote `_DATE_RE` via shared `_DATE_TOKEN`: now accepts **2-digit years** ("16-Feb-26"),
  **ordinals** ("16th February 2026"), and `-`/`.`/space/comma separators.
- Added `_plausible_joining()`; fixed the "Date of Joining" matcher to allow table-cell
  separators (no colon); reject stale/before-offer dates → route to Claude.

**Salary parsing** ([app/field_extractor.py](app/field_extractor.py)) — *bug: CTC 4,116 + 100× inflation*
- `_parse_amount`: stop stripping the decimal point ("3.43" was becoming 343).
- `_salary_near`: handle **Lakh/Lac/LPA/Crore/Cr** units (word-bounded) → "3.43 Lakh" = 343000.
- This also corrected a latent **100× inflation** (".00" stripped) on ~40 letters.

**Plausibility gate** ([app/field_extractor.py](app/field_extractor.py))
- New `_plausibility_problems()` (CTC range, basic≤CTC, components≤CTC, joining≥offer,
  date-year sanity). Wired into `is_low_confidence()` → **implausible regex output is
  routed to Claude instead of poisoning the verdict.** Silent-bad letters: 11 → 0.

**Field cleanup** ([app/field_extractor.py](app/field_extractor.py))
- Company name: reject headings ("OFFER OF EMPLOYMENT", "LETTER OF INTENT"), contact/PIN
  lines, >6-word clauses, and bare job-titles w/o a corp suffix ("Associate Data Processing").
- Candidate name: stop `\s+` crossing newlines into "Designation"; reject label/heading/role/
  corp-suffix tokens via `_clean_person_name`.
- HR: **"Authorized Signatory" now goes to designation, NOT hr_name** (was 49 bad names);
  strip a bleed-in candidate name out of hr_name; `_clean_designation` drops clause/date spill.
- Job title: reject clauses/duplicates-of-company/candidate.
- Work location: reject clauses and placeholder blanks.

**Domain check** ([app/checker.py](app/checker.py)) — *bug*
- `domain.lstrip("www.")` ate leading letters ("wipro.com"→"ipro.com"). Fixed to a proper
  prefix strip. Restores the strongest online signal (direct website fetch).

**Online presence** ([app/checker.py](app/checker.py))
- **Removed Bing method.** Bing static scraping returns a generic JS/consent page identical
  for real and fake names, so it confirmed ANY company via the echoed query (over-credited
  fakes, slipped past Gate 2). Direct-website + Wikipedia + DuckDuckGo remain.

**Scoring rules** ([app/rules.py](app/rules.py))
- **Future-dated offer** (e.g. dated next month) now adds a penalty AND counts as a Gate-3
  fraud marker (was silently ignored).

**Deterministic fraud scans** ([app/config.py](app/config.py))
- Removed bare `"____"` and `"xxxx"` from `template_artifacts`. They matched **signature/
  acceptance lines** ("I, ____, accept") and **masked IDs** ("Aadhar XXXX8274"), wrongly
  tripping a Gate-3 fraud marker on **54 legit letters (17%) → now 0**. Real bracketed
  placeholders (`[CANDIDATE NAME]`, `[DATE]`) are still caught.

**Tamper / edit-artifact detection (NEW)** — *the clean-vs-iLovePDF identical-result case*
- [app/models.py](app/models.py): added `RawPDFData.tamper_artifacts`.
- [app/pdf_reader.py](app/pdf_reader.py): `_scan_floating_dates()` — flags a date sitting
  ALONE as its own text line in the body zone (a paste artifact left when a date is edited;
  renders inline but is a separate text object — pdfplumber hides it, PyMuPDF sees it).
- [app/rules.py](app/rules.py): yellow **"Possible Edit Artifact"** flag (escalated wording
  if the PDF also has an online-editor footprint like iLovePDF).
- [app/ai_client.py](app/ai_client.py): `tamper_artifacts` fed into the Claude **Text Tampering** context.

## 5. Verification harnesses (ALL FREE — no API)

Run with `./.venv/Scripts/python.exe <script>`:
- **`audit_extraction.py`** — plausibility audit; reports silent-bad (implausible values
  reaching the verdict). Current: 0 silent-bad / 315 text letters.
- **`field_audit.py`** — per-field quality (company/candidate/job/hr/location/cin…).
  Current: 0 dirty fields.
- **`final_check.py`** — consolidated: crashes + implausible + dirty + a SPOTLIGHT dump of
  the previously-broken letters' full field sets.
- **`verify_sample.py`** — ⚠️ PAID. Full pipeline on ~20 letters, diffs new verdict vs the
  verdict stored in each saved report. Last run: targets moved the safe direction
  (LEGITIMATE→MANUAL REVIEW), controls stable except one borderline supporting-doc.

Test fixtures (root): `Job Offer - S Suresh_Shared by aspirant.pdf` (CLEAN) and
`343077 Suresh Offer Letter (3)_Uploaded on Portal.pdf` (TAMPERED via iLovePDF, Feb→April).
The tampered one now yields `tamper_artifacts=["page 1: floating date 'April  2026.'"]`;
the clean one yields `[]`.

## 6. What's LEFT (prioritized)

0. **Batch API — BUILT (opt-in), pending one paid validation run.** The real cost lever:
   −50% on EVERYTHING → **~₹3.5–5.5/letter** vs ₹7–11, same model/prompts/checks (no accuracy or
   security change), only a ~1 h async wait. Wired as **opt-in** so the working sequential path is
   untouched: `POST /batch/start {"use_batch_api": true}` → [app/batch_processor.py](app/batch_processor.py)
   `_process_batch_via_api()` (Phase 1 local prep + build requests → submit ONE batch → poll → per-letter
   score+save). Shared builder/parser in [app/ai_client.py](app/ai_client.py): `build_scoring_content`,
   `build_batch_request`, `parse_batch_result`, `_parse_scoring_result` (sync `analyze_letter` uses the
   same helpers → byte-identical prompts; **caching stacks inside the batch**). Free-verified: SDK 0.95.0
   has `messages.batches.*`; a real Suresh letter builds a valid request (cached static block + per-letter
   data + 5 images). **NOT yet run end-to-end** — needs ONE small paid batch (`use_batch_api:true` on a
   few inbox letters) to confirm submit→poll→results→save. Caveat: only `analyze_letter` is batched; OCR
   (image PDFs) + regex→Claude extraction fallback stay synchronous full-price. Server restart mid-batch
   loses the in-memory poll (files revert to inbox via startup_cleanup, but the paid batch is wasted) —
   acceptable for v1; harden later if needed.

1. **Paid end-to-end confirmation (after a SERVER RESTART)** — ~3 letters: Ajay (composite →
   Image ≤2, SUSPICIOUS, score clamped), Suresh TAMPERED (now scores below the CLEAN copy),
   Sourav (the false "Document Processing Issue" gone). Everything in §4 is verified free but
   not yet observed live — the running server holds old code until restarted.
2. **Semantic-fraud layer (the real next frontier)** — the composite detector catches a
   fabricated *structure*; a textually-fishy yet structurally-clean scam is NOT caught. Build
   as **AI-WEIGHTED signals, NOT hard gates** — legit staffing letters (e.g. Sourav / S&IB) have
   the same shape, so a hard rule would false-positive. Candidates (all free, text-layer):
   dual-employer "job at our client", a role-aware salary-plausibility floor, mixed date-format.
3. **Minor / optional:**
   - 15-flag handler only *warns* on ≠15 (could force REVIEW).
   - `job_title` "designated as X" coverage gap (returns None — missing, not wrong).
   - `_scan_placeholders` broad `_{4,}`/`X{4,}`; `"bank account number"` red phrase (1 legit hit).
   - `job_title`/`work_location` high *missing* rates — coverage, not wrongness.
4. **Pre-existing bugs:**
   - Scheduler: `_scheduled_batch_trigger` is sync but `start_batch` calls
     `asyncio.get_event_loop()` — likely breaks under APScheduler's thread executor.
   - `@app.on_event` deprecated (migrate to lifespan).
5. **Production gate (not started):** no auth; `/reports/*` AND `/saved-reports/*` serve
   candidate PII openly. Write a deploy checklist before going live.

## 7. Architecture notes for continuity

- **9 pillars** (max): image 11, signature 11, text 11, date 11, salary 11, grammar 12,
  hr 11, company 11, online 11 = 100. Online pillar is **computed in Python** (DNS +4,
  web +7), NOT by Claude. Visual pillars are forced `null` when no images were sent.
- **Verdicts:** ≥80 LEGITIMATE · 51–79 MANUAL REVIEW · <51 SUSPICIOUS.
- **Gates only cap DOWNWARD** (never auto-approve under uncertainty):
  - Gate 1 = conservative/incomplete visual pillar → MANUAL REVIEW.
  - Gate 2 = genuinely no online presence → MANUAL REVIEW.
  - Gate 3 = deterministic fraud marker (red phrase / unfilled placeholder / impossible date / future offer).
  - **Gate 3b** = edit-laundering (online editor + edit artifact) → SUSPICIOUS.
  - **Gate 3c** = composited / layered forgery (`composite_artifacts`) → SUSPICIOUS.
- **Score-clamp:** when a FRAUD gate (2 / 3 / 3b / 3c) fires, the displayed score is pulled into
  the capped verdict's band (SUSPICIOUS→≤50, MANUAL REVIEW→≤79). **Gate 1 is EXCLUDED** (fail-loud:
  "couldn't verify" never lowers the number). This is how the number now follows the verdict.
- **Penalties are still shown as flags, NOT subtracted** — the score-clamp (above), not penalty
  subtraction, is what aligns the number with a fraud-capped verdict.
- **Signature** pillar judges *credibility*, NOT "is it pasted" — a pasted/scanned/digital sig is
  legitimate. A high-res signature close-up is sent to Claude as the final image for this pillar.
- **Fail-loud principle:** "couldn't check" ≠ "it's fake". Network/parse failures →
  conservative + flag, never a silent fraud signal.

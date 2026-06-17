# OfferVerify — End-to-End Pipeline (Single Offer Letter)

This document maps the full verification pipeline for **one** offer letter — every
function, every fallback, every connection, and where the (paid) Claude calls happen.

> All three entry points converge on the **same** `_run_pipeline()` in
> [`main.py`](main.py):
> - `POST /analyze` — ad-hoc upload (result shown, **not** saved)
> - `POST /analyze-from-inbox` — claims a file in `inbox/` → processes → moves to
>   `processed/` and saves an HTML report
> - `/batch/*` + APScheduler cron — runs the whole inbox sequentially
>
> LLM backend: **Anthropic Claude** (`claude-3-5-sonnet`), served on port **8003**.

---

## High-level flow

```
PDF bytes
   │
   ▼
STAGE 1  read_pdf()            text + images + metadata + red phrases + placeholders + page renders
   │
   ▼
STAGE 2  extract_fields()      regex first → Claude fallback only if low-confidence
   │
   ▼
STAGE 3  analyze_images()      logo/signature/stamp metadata (no API call)
   │
   ▼
STAGE 4  asyncio.gather(...)   DNS + online presence + date logic + salary math + completeness
   │
   ▼
STAGE 5  analyze_letter()      ◄── the 1 guaranteed paid Claude call (vision + text scoring)
   │
   ▼
STAGE 5a/5b  rescues           fill missing name/domain from the vision pass (no extra call)
   │
   ▼
STAGE 6  compute_final_score() 9 pillars → penalties(flags) → verdict → gates → flags
   │
   ▼
AnalysisResult  → JSON to UI   (+ save_report() HTML for the inbox flow)
```

Legend for hardening markers used below:
`★A` online-check error vs not-found · `★B` DNS lookup-error vs NXDOMAIN ·
`★C` company-name vision rescue · `★D` AI-failure surfaced · `★E` extraction-warning surfaced ·
`★G3` deterministic fraud-marker verdict cap.

---

## STAGE 1 — PDF Reading · `read_pdf()` · *no Claude (usually)*

[`app/pdf_reader.py`](app/pdf_reader.py) — pure-Python extraction. Six sub-extractors,
each wrapped so a single failure cannot abort the rest (it appends to
`extraction_warnings` and continues).

```
read_pdf()
├─ _extract_text()                          TEXT
│    ├─ 1) pdfplumber                ──found text?──► use it ✅            (0 API calls)
│    └─ 2) FALLBACK _ocr_pdf()       ──no text layer──► render pages → Claude Vision OCR
│                                     ⚠️ +1 Claude call — ONLY for scanned/image PDFs
│                                     (capped at 10 pages, 1.5× zoom, JPEG 85%)
├─ _extract_images()    logo / signature / stamp (PyMuPDF/fitz)        → warns on error
├─ _extract_metadata()  author, created-with (Canva/Photoshop?), dates → warns on error
├─ _scan_red_phrases()  regex vs config.red_phrases                    → red_phrases_found[]
├─ _scan_placeholders() [NAME], ____, RRRR unfilled template fields    → placeholder_scan[]
└─ _render_pages()      1.5× page PNGs (first / last / page 2)         → rendered_pages[] (vision)
```

**Hard guard:** if `full_text` is still empty after both attempts, the pipeline raises
**HTTP 422 "Could not extract text — may be a scanned image"** ([main.py](main.py)) — a
loud fail, never a silent empty analysis.

---

## STAGE 2 — Field Extraction · `extract_fields()` · *regex-first (cost saver)*

[`app/ai_client.py`](app/ai_client.py) `extract_fields()` → two stages:

```
extract_fields()
├─ STAGE 2a  extract_fields_from_text()        100% regex/heuristics — 0 API calls
│     → company_name, candidate, company_domain/email/website, CIN,
│       job_title, employment_type, dates, salary breakup, HR name/designation
│     (lowercase-styled brand names like "iEnergizer" handled here)
│
├─ is_low_confidence()?  ── NO ──►  return regex result ✅   (the common, free path)
│     "confident" = has contact info AND ≥2 of {valid company, candidate, a date}
│
└─ STAGE 2b  FALLBACK → Claude extract_fields()    ⚠️ +1 Claude call
      → only when regex is too sparse or OCR text is messy
      → parse failure ⇒ ExtractedFields(notes="Response could not be parsed")  (loud)
```

Key extractors in [`app/field_extractor.py`](app/field_extractor.py):
`_extract_company_name` (For-signoff → suffix line → label → near-CIN → header),
`_extract_salary` (CTC/Basic/HRA/PF/ESI with monthly↔annual normalisation),
`_extract_hr`, `_extract_cin` (CIN/GST/UDYAM), `_extract_offer_date`/`_extract_joining_date`.

---

## STAGE 3 — Image Metadata · `analyze_images()` · *no Claude*

[`app/ai_client.py`](app/ai_client.py) — returns logo / signature / stamp presence and
positions from the Stage-1 PDF metadata. **0 API calls** (this used to be a Claude call;
it was removed to cut cost).

---

## STAGE 4 — Parallel Checks · *5 concurrent via `asyncio.gather`* · *no Claude*

[main.py](main.py) — all network/CPU work, run at once:

```
├─ check_domain(domain)              DNS — [app/checker.py]
│     A-record ─┬─ resolves          → dns_valid = True
│               ├─ NXDOMAIN          → genuinely does not exist (real negative)
│               └─ timeout / error   → lookup_error = True   ★B  (system failure ≠ fake)
│     MX-record (informational only — many real cos use Gmail/Office365)
│
├─ check_company_online(name, domain)  ONLINE PRESENCE — layered, stops at first hit:
│     1) _try_direct_website(domain)   https://domain live + >1.5 KB body   → +7  (strongest)
│     2) _try_wikipedia(name)          Wikipedia REST / search title match  → +7
│     3) _try_duckduckgo(name)         ≥2 organic results + name present    → +7
│     4) _try_bing(name)               name in a real result page           → +7
│     5) _try_dns_guess(name)          a guessed domain resolves            → +4  (heuristic)
│     └─ none confirm → found = False
│           ★A  if EVERY *real* method (1–4) hit a network error → checked = False
│               ("could not verify", NOT "company is fake").
│               A dns-guess (5) miss does NOT count toward this decision.
│           Guard: needs name OR domain; Method 1 runs on domain alone.
│
├─ compute_date_logic(fields)        offer vs joining: gap_days, gap_impossible, too-long
├─ compute_salary_math(fields)       do components reconcile against CTC?
└─ compute_completeness(fields,raw)  fraction of expected fields present (0.0–1.0)
```

---

## STAGE 5 — AI Deep Analysis · `analyze_letter()` · **⚠️ the 1 guaranteed paid call (~$0.06)**

[`app/ai_client.py`](app/ai_client.py) — vision + text. **Inputs:** full-text snippet +
every Stage-4 fact (DNS/online/dates/salary/completeness/metadata) + the
`rendered_pages` images. **Returns JSON:**

- **8 pillar scores** (image, signature, text, date, salary, grammar, HR, company).
  Visual pillars (image/signature) are forced `null` when no images were sent.
- 15 mandatory flags, `summary`, `recommended_action`.
- `company_domain_found` + `company_name_found` — read from the letterhead image.
- On an unparseable/empty response → `analysis_failed = True`  ★D (surfaced loudly,
  not a silent ~50).

> The **online pillar score is NOT produced by Claude** — it is computed in Python in
> Stage 6 (DNS +4, online +7). Claude only writes a one-line qualitative note.

Reliability: `@retry(3×, exponential backoff)` on hard exceptions; after 3 failures the
exception propagates → **HTTP 500 "Pipeline error"** (loud).

---

## STAGE 5a / 5b — Rescues from the vision pass · *no extra Claude call*

[main.py](main.py) — reuse the Stage-5 result so a text-extraction miss is recovered for free:

```
5a) company_name missing  + analysis.company_name_found   → fill fields.company_name   ★C
5b) company_domain missing + analysis.company_domain_found → fill domain, then
        RE-RUN check_domain() + check_company_online()  (free network calls, no Claude)
```

---

## STAGE 6 — Final Scoring · `compute_final_score()` · *no Claude*

[`app/rules.py`](app/rules.py):

```
1. Build 9 pillars via _build_pillar():
     score present → PillarScore(score, "verified")
     score None    → conservative 50% of max + "unverified_conservative"   (never a silent 0)

2. ONLINE pillar = (DNS +4 if dns_valid) + (online +7 if found), capped at 11.
     BUT if dns.lookup_error OR not company_online.checked
        → conservative + "unverified_conservative"   ★A/★B  (network failure ≠ negative)

3. total_raw = Σ all 9 pillar scores            (denominator is ALWAYS 100)

4. Penalties (red phrases, impossible date, salary-math, placeholders, metadata,
   offer-age, low-completeness) → collected as FLAGS ONLY. They do NOT subtract
   from the score (avoids double-counting the AI + keeps the validated corpus stable).

5. Verdict by score:  ≥80 LEGITIMATE · 51–79 MANUAL_REVIEW · <51 SUSPICIOUS

6. GATES — only ever cap DOWNWARD (never auto-approve under uncertainty):
     Gate 1  any conservative pillar (visual/AI incomplete)        → block LEGITIMATE
     Gate 2  GENUINELY no online presence
             (company_online.checked AND not found AND
              not dns_valid AND not dns.lookup_error)               → block LEGITIMATE
     Gate 3  deterministic fraud marker — red phrase OR unfilled    → block LEGITIMATE   ★G3
             placeholder OR impossible date (joining < offer)
             (verdict CAP, not a point subtraction)

7. Flags assembled & sorted (red → yellow → green):
     AI flags + DNS/short-gap + penalties("Risk Signal Detected")
     + AI-failure("AI Scoring Failed")          ★D
     + extraction warnings("Document Processing Issue", success-render line skipped)  ★E

8. Return AnalysisResult(overall_score, verdict, verdict_color, score_breakdown,
                         flags, penalties, letter_summary, hard_gate_*)
```

---

## Output

- `POST /analyze` → `AnalysisResult` JSON rendered by `static/index.html`.
- `POST /analyze-from-inbox` → same JSON **plus** `save_report()` writes an HTML report
  into `reports/{LEGITIMATE,MANUAL_REVIEW,SUSPICIOUS}/`, and the PDF moves to `processed/`.

---

## Claude call count & cost

The only **guaranteed** paid call is `analyze_letter` (Stage 5). Everything else —
DNS, the 5-method online chain, Wikipedia/DuckDuckGo/Bing, image/metadata extraction,
red-phrase/placeholder scans, all scoring and gating — is **free** (network/CPU).

| PDF type                 | Vision OCR | extract_fields | analyze_letter | **Total**       |
|--------------------------|:----------:|:--------------:|:--------------:|:----------------|
| **Text-based (typical)** |     –      | regex (0)      |       1        | **1  (~$0.06)** |
| Sparse / messy text      |     –      | +1 fallback    |       1        | 2               |
| **Scanned / image PDF**  | +1 OCR     | usually +1     |       1        | **2–3**         |

---

## Silent-failure hardening (why "couldn't check" ≠ "it's bad")

The pipeline is designed to **fail loud**: any verification step that could not actually
run is surfaced as *unverified → human review*, never disguised as a fraud signal.

| Marker | Failure mode | Old behaviour (silent) | New behaviour (loud) |
|--------|--------------|------------------------|----------------------|
| ★A | Online check network error | scored as "company not found" (−7) | `checked=False` → conservative + flag, no auto-approve |
| ★B | DNS timeout/resolver error | scored as "domain fake" (−4) | `lookup_error=True` → conservative, distinct from NXDOMAIN |
| ★C | Company name not in text    | `None` flows downstream silently | rescued from vision pass (no extra call) |
| ★D | AI returns unparseable JSON | silent ~50 / MANUAL_REVIEW | red "AI Scoring Failed" flag |
| ★E | PDF sub-extraction degraded | warnings collected, never shown | yellow "Document Processing Issue" flags |
| ★G3| Deterministic fraud marker  | flag only, could still auto-approve | verdict capped out of LEGITIMATE |

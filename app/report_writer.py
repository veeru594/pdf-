"""
report_writer.py — Generate a standalone, self-contained HTML report from
an AnalysisResult.  The saved file looks identical to the live results screen
but requires no server: open it in any browser, print it, or email it.

Security: every field sourced from the PDF / Claude is HTML-escaped before
embedding — no XSS possible from crafted PDF content.
"""
import re
import html
import hashlib
from pathlib import Path
from datetime import datetime

from app.models import AnalysisResult
from app.inbox import REPORTS_DIR, VERDICT_FOLDER, ensure_dirs, PROJECT_ROOT

# Persistent archive for ad-hoc /analyze runs. Deliberately SEPARATE from the
# inbox→processed→reports flow (which is purged periodically) so a direct upload
# always leaves a reference you can reopen after the results screen is gone.
SAVED_REPORTS_DIR = PROJECT_ROOT / "saved_reports"


# ── Public API ───────────────────────────────────────────────────

def save_report(result: AnalysisResult, original_filename: str) -> Path:
    """
    Generate a standalone HTML report and write it to the correct verdict folder.
    Returns the path of the saved file.
    """
    ensure_dirs()
    folder  = VERDICT_FOLDER.get(result.verdict, "MANUAL_REVIEW")
    outdir  = REPORTS_DIR / folder
    fname   = _report_filename(original_filename, result)
    outpath = outdir / fname
    outpath.write_text(_build_html(result, original_filename), encoding="utf-8")
    return outpath


def save_adhoc_report(result: AnalysisResult, original_filename: str) -> Path:
    """
    Persist a standalone HTML report for an ad-hoc /analyze run into saved_reports/.
    Independent of the inbox/processed/reports flow — these files are NOT purged with
    processed/. The filename embeds company, candidate, date and score for easy browsing.
    Returns the path of the saved file.
    """
    SAVED_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    fname   = _report_filename(original_filename, result)
    outpath = SAVED_REPORTS_DIR / fname
    outpath.write_text(_build_html(result, original_filename), encoding="utf-8")
    return outpath


# ── Filename ─────────────────────────────────────────────────────

def _report_filename(pdf_name: str, result: AnalysisResult) -> str:
    ls        = result.letter_summary
    company   = _slug(ls.get("company_name")   or "Unknown")[:20]
    candidate = _slug(ls.get("candidate_name") or "Unknown")[:18]
    date      = datetime.now().strftime("%Y-%m-%d")
    pdf_stem  = _slug(Path(pdf_name).stem)[:30]
    score     = int(result.overall_score)          # embed score for queue display
    suffix    = hashlib.md5(pdf_name.encode()).hexdigest()[:4]
    return f"{pdf_stem}_{company}_{candidate}_{date}_s{score}_{suffix}.html"


def _slug(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "_", str(text)).strip("_")


# ── HTML builder ─────────────────────────────────────────────────

def _build_html(r: AnalysisResult, filename: str) -> str:
    ls    = r.letter_summary
    score = r.overall_score
    vc    = r.verdict_color  # green | yellow | red

    color_map = {
        "green":  {"score": "#059669", "bar": "#059669", "verdict": "#059669"},
        "yellow": {"score": "#d97706", "bar": "#d97706", "verdict": "#d97706"},
        "red":    {"score": "#dc2626", "bar": "#dc2626", "verdict": "#dc2626"},
    }
    col = color_map.get(vc, color_map["red"])

    # Chips
    chip_defs = {
        "green":  [("80+",   "green")],
        "yellow": [("51–79", "amber")],
        "red":    [("0–50",  "red")],
    }
    chips_html = "".join(
        f'<div class="chip chip-{c}"><span class="chip-dot"></span>{_e(t)}</div>'
        for t, c in chip_defs.get(vc, chip_defs["red"])
    )
    if r.hard_gate_triggered:
        chips_html += '<div class="chip chip-amber" style="font-size:11px">⚠ Visual Check Blocked</div>'
    chips_html += f'<div class="chip chip-blue">{_e(r.recommended_action)}</div>'

    hard_gate_html = ""
    if r.hard_gate_triggered and r.hard_gate_reason:
        hard_gate_html = f"""
<div class="gate-banner">
  <span style="font-size:16px;flex-shrink:0">⚠</span>
  <div>
    <strong style="display:block;margin-bottom:2px">Hard Gate Active — Score capped at 51–79 range</strong>
    {_e(r.hard_gate_reason)}
  </div>
</div>"""

    # Flags
    red_flags    = [f for f in r.flags if f.severity == "red"]
    yellow_flags = [f for f in r.flags if f.severity == "yellow"]
    green_flags  = [f for f in r.flags if f.severity == "green"]

    flags_html = ""
    if red_flags:
        flags_html += _flag_section("🚨 Critical Issues", red_flags, "red")
    if yellow_flags:
        flags_html += _flag_section("⚠ Warnings", yellow_flags, "yellow")
    if green_flags:
        flags_html += _flag_section("✓ Passed Checks", green_flags, "green")

    # Letter overview table
    rows = [
        ("Company",          ls.get("company_name")),
        ("Candidate",        ls.get("candidate_name")),
        ("Position",         ls.get("job_title")),
        ("Employment Type",  ls.get("employment_type")),
        ("Offer Date",       ls.get("offer_date")),
        ("Joining Date",     ls.get("joining_date")),
        ("Date Gap",         f'{ls["date_gap_days"]} days' if ls.get("date_gap_days") is not None else None,
                             "ok" if ls.get("date_gap_valid") else ("err" if ls.get("date_gap_valid") is False else None)),
        ("Work Location",    ls.get("work_location")),
        ("HR Name",          ls.get("hr_name")),
        ("HR Designation",   ls.get("hr_designation")),
        ("CTC Annual",       f'₹ {ls["ctc_annual"]:,.0f}' if ls.get("ctc_annual") else None),
        ("Completeness",     f'{round(ls["completeness_score"]*100)}%' if ls.get("completeness_score") is not None else None),
        ("Red Phrases",      ", ".join(ls["red_phrases_found"]) if ls.get("red_phrases_found") else "None found"),
        ("Salary Math",      "Correct" if ls.get("salary_math_ok") is True else ("Mismatch" if ls.get("salary_math_ok") is False else "N/A"),
                             "ok" if ls.get("salary_math_ok") is True else ("err" if ls.get("salary_math_ok") is False else "na")),
        ("PDF Created With", ls.get("pdf_created_with")),
        ("PDF Author",       ls.get("pdf_author")),
        ("PDF Created",      ls.get("pdf_created_date")),
        ("PDF Modified",     ls.get("pdf_modified_date")),
        ("Metadata",         ls.get("pdf_metadata_reason") if ls.get("pdf_metadata_suspicious")
                             else ("Clean" if ls.get("pdf_created_with") else None),
                             "err" if ls.get("pdf_metadata_suspicious") else "ok"),
    ]
    ov_rows_html = ""
    for row in rows:
        label  = row[0]
        value  = row[1]
        tag_cls = row[2] if len(row) > 2 else None
        if value is None or value == "":
            continue
        if tag_cls:
            cell = f'<span class="tag tag-{tag_cls}">{_e(value)}</span>'
        else:
            cell = _e(value)
        ov_rows_html += f"<tr><td>{_e(label)}</td><td>{cell}</td></tr>\n"

    # Score breakdown
    bk = r.score_breakdown
    PARAM_ORDER = [
        ("company_online",       "Company Online Presence"),
        ("company_details",      "Company Details"),
        ("hr_signature_details", "HR Signature & Details"),
        ("grammar_errors",       "Grammar & Employment Terms"),
        ("salary_details",       "Salary Details"),
        ("date_tampering",       "Date Tampering"),
        ("signature_tampering",  "Signature Tampering"),
        ("text_tampering",       "Text Tampering"),
        ("image_tampering",      "Image Tampering"),
    ]
    breakdown_html = ""
    for key, name in PARAM_ORDER:
        item = getattr(bk, key, None)
        if item:
            breakdown_html += _pillar_row(name, item)

    total_pct   = min(score, 100)
    total_cls   = "high" if score >= 80 else ("medium" if score >= 51 else "low")
    breakdown_html += f"""
<div class="br-row br-total-row">
  <div class="br-top">
    <span class="br-name br-total-label">Overall Score</span>
    <div class="br-track"><div class="br-fill {total_cls}" style="width:{total_pct}%"></div></div>
    <span class="br-score br-total-score">{score}/100</span>
  </div>
</div>"""

    # Risk signals (penalties)
    risk_html = ""
    if r.penalties:
        items = "".join(
            f'<div class="penalty-row"><span class="penalty-reason">{_e(p.get("reason",""))}</span></div>'
            for p in r.penalties
        )
        risk_html = f"""
<div class="section-card">
  <div class="section-title">⬇ Risk Signals Detected</div>
  <div class="penalty-list">{items}</div>
</div>"""

    # Footer
    proc_time  = f"{r.processing_time_ms / 1000:.1f}s" if r.processing_time_ms else "—"
    file_size  = f"{r.file_size_kb} KB" if r.file_size_kb else "—"
    report_gen = datetime.now().strftime("%d %b %Y, %H:%M")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>OfferVerify Report — {_e(ls.get("company_name") or "Unknown")} — {_e(ls.get("candidate_name") or "Unknown")}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@300;400;500;600;700;800&family=IBM+Plex+Mono:wght@400;500;700&display=swap" rel="stylesheet">
<style>
:root {{
  --ink:      #1a202c;
  --ink-2:    #2d3748;
  --ink-3:    #718096;
  --ink-4:    #a0aec0;
  --paper:    #ffffff;
  --surface:  #f8fafc;
  --border:   #e2e8f0;
  --border-2: #cbd5e0;
  --accent:   #2563eb;
  --accent-l: #eff6ff;
  --green:    #059669;
  --green-l:  #ecfdf5;
  --amber:    #d97706;
  --amber-l:  #fffbeb;
  --red:      #dc2626;
  --red-l:    #fee2e2;
  --font:     'IBM Plex Sans', sans-serif;
  --mono:     'IBM Plex Mono', monospace;
  --r:        8px;
  --sh:       0 1px 2px rgba(0,0,0,.05);
  --sh-md:    0 4px 15px rgba(0,0,0,.08);
}}
*, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{
  font-family: var(--font);
  background: var(--surface);
  color: var(--ink);
  font-size: 14px;
  line-height: 1.5;
  -webkit-font-smoothing: antialiased;
}}
.report-header {{
  background: var(--paper);
  border-bottom: 1px solid var(--border);
  padding: 16px 32px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  box-shadow: var(--sh);
}}
.report-brand {{
  display: flex;
  align-items: center;
  gap: 10px;
}}
.brand-mark {{
  width: 32px; height: 32px;
  background: linear-gradient(135deg, #2563eb 0%, #1e40af 100%);
  border-radius: 8px;
  display: grid;
  place-items: center;
}}
.brand-mark svg {{ width: 18px; height: 18px; fill: white; }}
.report-brand-name {{ font-size: 16px; font-weight: 700; color: var(--ink); }}
.report-meta {{
  display: flex;
  gap: 24px;
  font-size: 12px;
  color: var(--ink-4);
  font-family: var(--mono);
  font-weight: 500;
}}
.report-meta span {{ display: flex; align-items: center; gap: 5px; }}
.page-body {{
  max-width: 900px;
  margin: 0 auto;
  padding: 32px 24px 60px;
  display: grid;
  grid-template-columns: 1fr;
  gap: 18px;
}}
.verdict-banner {{
  background: linear-gradient(135deg, var(--paper) 0%, #f8fafc 100%);
  border: 1px solid var(--border);
  border-radius: 16px;
  padding: 32px;
  display: grid;
  grid-template-columns: auto 1fr auto;
  align-items: center;
  gap: 32px;
  box-shadow: var(--sh-md);
}}
.score-block {{ display: flex; flex-direction: column; align-items: center; gap: 4px; }}
.score-num-big {{
  font-size: 52px; font-weight: 800; line-height: 1;
  font-variant-numeric: tabular-nums; letter-spacing: -2px;
}}
.score-denom {{ font-size: 12px; color: var(--ink-4); font-family: var(--mono); font-weight: 500; }}
.verdict-label {{ font-size: 20px; font-weight: 700; letter-spacing: -0.5px; margin-bottom: 4px; }}
.verdict-sub   {{ font-size: 13px; color: var(--ink-3); font-weight: 500; }}
.verdict-chips {{ display: flex; flex-direction: column; gap: 10px; align-items: flex-end; }}
.chip {{
  display: inline-flex; align-items: center; gap: 8px;
  padding: 8px 14px; border-radius: 20px;
  font-size: 12px; font-weight: 700;
  letter-spacing: 0.5px; text-transform: uppercase; white-space: nowrap;
}}
.chip-red   {{ background: var(--red-l);   color: var(--red);   border: 1.5px solid #fca5a5; }}
.chip-amber {{ background: var(--amber-l); color: var(--amber); border: 1.5px solid #fcd34d; }}
.chip-green {{ background: var(--green-l); color: var(--green); border: 1.5px solid #6ee7b7; }}
.chip-blue  {{ background: var(--accent-l); color: var(--accent); border: 1.5px solid #93c5fd; }}
.chip-dot {{ width: 8px; height: 8px; border-radius: 50%; background: currentColor; }}
.score-bar-track {{
  height: 4px; background: var(--border); border-radius: 4px; overflow: hidden; margin: 14px 0 0;
}}
.score-bar-fill {{ height: 100%; border-radius: 4px; }}
.gate-banner {{
  background: var(--amber-l); border: 1.5px solid #fcd34d;
  border-radius: 10px; padding: 12px 16px;
  font-size: 13px; color: var(--amber);
  display: flex; gap: 10px; align-items: flex-start;
}}
.section-card {{
  background: var(--paper); border: 1px solid var(--border);
  border-radius: 12px; overflow: hidden; box-shadow: var(--sh);
}}
.section-title {{
  padding: 16px 20px; font-size: 14px; font-weight: 700;
  color: var(--ink-2); border-bottom: 1px solid var(--border);
  background: var(--surface);
}}
.section-body {{ padding: 20px; }}
.ov-table {{ width: 100%; border-collapse: collapse; }}
.ov-table tr {{ border-bottom: 1px solid var(--border); }}
.ov-table tr:last-child {{ border-bottom: none; }}
.ov-table td {{ padding: 11px 4px; vertical-align: top; font-size: 14px; }}
.ov-table td:first-child {{ color: var(--ink-3); font-weight: 600; width: 35%; padding-right: 16px; }}
.tag {{
  display: inline-flex; align-items: center; gap: 6px;
  padding: 4px 10px; border-radius: 6px;
  font-size: 12px; font-weight: 600; font-family: var(--mono);
}}
.tag-ok  {{ background: var(--green-l); color: var(--green); }}
.tag-err {{ background: var(--red-l);   color: var(--red); }}
.tag-na  {{ background: var(--surface); color: var(--ink-4); border: 1px solid var(--border); }}
.summary-text {{ font-size: 14px; line-height: 1.8; color: var(--ink-2); }}
.flag-list {{ display: flex; flex-direction: column; gap: 10px; }}
.flag-item {{
  display: flex; gap: 14px; padding: 14px 16px;
  border-radius: 10px; font-size: 14px; border: 1.5px solid;
}}
.flag-item.red    {{ background: var(--red-l);   border-color: #fca5a5; }}
.flag-item.yellow {{ background: var(--amber-l); border-color: #fcd34d; }}
.flag-item.green  {{ background: var(--green-l); border-color: #6ee7b7; }}
.flag-sym {{
  width: 24px; height: 24px; border-radius: 50%;
  display: grid; place-items: center;
  font-size: 12px; font-weight: 700; flex-shrink: 0;
}}
.flag-item.red    .flag-sym {{ background: var(--red);   color: white; }}
.flag-item.yellow .flag-sym {{ background: #d97706;      color: white; }}
.flag-item.green  .flag-sym {{ background: var(--green); color: white; }}
.flag-title  {{ font-weight: 700; color: var(--ink); margin-bottom: 2px; }}
.flag-detail {{ color: var(--ink-3); font-size: 13px; line-height: 1.6; }}
.breakdown-list {{ display: flex; flex-direction: column; gap: 16px; }}
.br-row {{}}
.br-top {{ display: flex; align-items: center; gap: 14px; margin-bottom: 6px; }}
.br-name {{ font-size: 13px; font-weight: 700; color: var(--ink-2); min-width: 180px; }}
.br-track {{ flex: 1; height: 8px; background: var(--border); border-radius: 4px; overflow: hidden; }}
.br-fill  {{ height: 100%; border-radius: 4px; }}
.br-fill.high   {{ background: #10b981; }}
.br-fill.medium {{ background: #f59e0b; }}
.br-fill.low    {{ background: #ef4444; }}
.br-score {{ font-family: var(--mono); font-size: 13px; color: var(--ink-2); font-weight: 700; min-width: 48px; text-align: right; }}
.br-reason {{ font-size: 12px; color: var(--ink-4); line-height: 1.6; padding-left: 2px; }}
.br-total-row {{ border-top: 2px solid var(--border); margin-top: 8px; padding-top: 10px; }}
.br-total-label {{ color: var(--ink); font-size: 14px; font-weight: 800; }}
.br-total-score {{ color: var(--ink); font-size: 14px; font-weight: 800; }}
.penalty-list {{ display: flex; flex-direction: column; gap: 8px; }}
.penalty-row {{
  padding: 11px 14px; background: var(--amber-l);
  border: 1.5px solid #fcd34d; border-radius: 8px; font-size: 13px;
}}
.penalty-reason {{ color: var(--ink-2); line-height: 1.6; font-weight: 500; }}
.results-footer {{
  display: flex; align-items: center; gap: 32px;
  justify-content: center; padding: 14px 20px;
  background: var(--paper); border: 1px solid var(--border);
  border-radius: 10px; font-size: 12px;
  color: var(--ink-4); font-family: var(--mono);
}}
.results-footer span {{ display: flex; align-items: center; gap: 6px; font-weight: 500; }}
@media print {{
  body {{ background: white; }}
  .report-header {{ position: static; box-shadow: none; }}
}}
</style>
</head>
<body>

<!-- ── HEADER ──────────────────────────────────────────────── -->
<div class="report-header">
  <div class="report-brand">
    <div class="brand-mark">
      <svg viewBox="0 0 20 20"><path d="M4 3h8l4 4v10H4V3z"/><path d="M12 3v4h4" fill="none" stroke="white" stroke-width="1.5"/><line x1="7" y1="10" x2="13" y2="10" stroke="white" stroke-width="1.5"/><line x1="7" y1="13" x2="11" y2="13" stroke="white" stroke-width="1.5"/></svg>
    </div>
    <span class="report-brand-name">OfferVerify — Authenticity Report</span>
  </div>
  <div class="report-meta">
    <span>📅 {_e(report_gen)}</span>
    <span>📄 {_e(filename)}</span>
    <span>⏱ {_e(proc_time)}</span>
    <span>💾 {_e(file_size)}</span>
  </div>
</div>

<!-- ── PAGE BODY ───────────────────────────────────────────── -->
<div class="page-body">

  <!-- Verdict banner -->
  <div class="verdict-banner">
    <div class="score-block">
      <div class="score-num-big" style="color:{col['score']}">{score}</div>
      <div class="score-denom">out of 100</div>
    </div>
    <div>
      <div class="verdict-label" style="color:{col['verdict']}">{_e(r.verdict)}</div>
      <div class="verdict-sub">{_e((r.summary or "").split(".")[0] + "." if r.summary else "Analysis completed")}</div>
      <div class="score-bar-track">
        <div class="score-bar-fill" style="width:{min(score,100)}%;background:{col['bar']}"></div>
      </div>
    </div>
    <div class="verdict-chips">{chips_html}</div>
  </div>

  {hard_gate_html}

  <!-- Letter overview -->
  <div class="section-card">
    <div class="section-title">📋 Letter Overview</div>
    <div class="section-body">
      <table class="ov-table">{ov_rows_html}</table>
    </div>
  </div>

  <!-- Analysis summary -->
  <div class="section-card">
    <div class="section-title">📝 Analysis Summary</div>
    <div class="section-body">
      <p class="summary-text">{_e(r.summary or "—")}</p>
    </div>
  </div>

  {flags_html}

  <!-- Score breakdown -->
  <div class="section-card">
    <div class="section-title">📊 Score Breakdown</div>
    <div class="section-body">
      <div class="breakdown-list">{breakdown_html}</div>
    </div>
  </div>

  {risk_html}

  <!-- Footer -->
  <div class="results-footer">
    <span>⏱ {_e(proc_time)}</span>
    <span>📄 {_e(filename)}</span>
    <span>💾 {_e(file_size)}</span>
  </div>

</div>
</body>
</html>"""


# ── HTML helpers ─────────────────────────────────────────────────

def _e(v) -> str:
    """HTML-escape any value for safe embedding in the report."""
    if v is None:
        return "—"
    return html.escape(str(v))


def _flag_section(title: str, flags: list, cls: str) -> str:
    sym = {"red": "✕", "yellow": "!", "green": "✓"}.get(cls, "•")
    items = "".join(
        f"""<div class="flag-item {cls}">
  <div class="flag-sym">{sym}</div>
  <div>
    <div class="flag-title">{_e(f.title)}</div>
    <div class="flag-detail">{_e(f.detail)}</div>
  </div>
</div>"""
        for f in flags
    )
    return f"""
<div class="section-card">
  <div class="section-title">{title} ({len(flags)})</div>
  <div class="section-body">
    <div class="flag-list">{items}</div>
  </div>
</div>"""


def _pillar_row(name: str, item) -> str:
    score     = item.score
    max_score = item.max
    reasoning = item.reasoning or ""
    score_disp = f"{score:.1f}" if score != int(score) else str(int(score))

    if item.score_type == "unverified_conservative":
        return f"""<div class="br-row">
  <div class="br-top">
    <span class="br-name">{_e(name)}</span>
    <div class="br-track"><div class="br-fill medium" style="width:50%"></div></div>
    <span class="br-score" style="color:#d97706">{score_disp}/{max_score}</span>
  </div>
  <div class="br-reason" style="color:#d97706">⚠ Conservative score applied — manual visual check required</div>
</div>"""

    pct      = round(score / max_score * 100) if max_score > 0 else 0
    fill_cls = "high" if pct >= 66 else ("medium" if pct >= 33 else "low")
    return f"""<div class="br-row">
  <div class="br-top">
    <span class="br-name">{_e(name)}</span>
    <div class="br-track"><div class="br-fill {fill_cls}" style="width:{pct}%"></div></div>
    <span class="br-score">{score_disp}/{max_score}</span>
  </div>
  <div class="br-reason">{_e(reasoning)}</div>
</div>"""

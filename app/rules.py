import re
from typing import Optional
from datetime import datetime

from app.config import settings
from app.models import (
    ExtractedFields, AIAnalysisResult, AIImageResult,
    CompanyOnlineResult, DnsResult, RawPDFData,
    AnalysisResult, ScoreBreakdown, PillarScore, AnalysisFlag,
)


def compute_date_logic(fields: ExtractedFields) -> dict:
    """Validate date logic from extracted fields."""
    result = {
        "offer_date": fields.offer_date,
        "joining_date": fields.joining_date,
        "gap_days": None,
        "gap_valid": None,
        "gap_impossible": False,
        "gap_note": "Could not determine dates"
    }

    if not fields.offer_date or not fields.joining_date:
        return result

    try:
        offer_dt = datetime.strptime(fields.offer_date, "%Y-%m-%d")
        join_dt  = datetime.strptime(fields.joining_date, "%Y-%m-%d")
        gap = (join_dt - offer_dt).days
        result["gap_days"] = gap

        if gap < 0:
            result["gap_valid"]      = False
            result["gap_impossible"] = True
            result["gap_note"]       = f"Joining date is {abs(gap)} days BEFORE offer date — impossible"
        elif gap > settings.max_joining_gap_days:
            result["gap_valid"] = False
            result["gap_note"]  = f"{gap} days gap — unusually long (over {settings.max_joining_gap_days} days)"
        elif gap < 3:
            result["gap_valid"] = True
            result["gap_note"]  = f"Only {gap} day(s) gap — very short, worth noting"
        else:
            result["gap_valid"] = True
            result["gap_note"]  = f"Gap of {gap} days is within normal range"
    except Exception:
        result["gap_note"] = "Date parsing failed"

    return result


def check_offer_age(offer_date: str) -> dict:
    """Detect if offer letter is suspiciously old (reused/resurrected fraud)."""
    result = {
        "offer_date": offer_date,
        "is_old": False,
        "days_old": None,
        "severity": None,
        "note": "Recent offer"
    }

    if not offer_date:
        return result

    try:
        offer_dt = datetime.strptime(offer_date, "%Y-%m-%d")
        today = datetime.now()
        days_old = (today - offer_dt).days
        result["days_old"] = days_old

        if days_old < 0:
            result["is_old"] = True
            result["severity"] = "red"
            result["note"] = f"Offer dated {abs(days_old)} days in the FUTURE — impossible"
        elif days_old > 365:
            result["is_old"] = True
            result["severity"] = "red"
            result["note"] = f"Offer is {days_old} days old (over 1 year) — likely reused/recycled fraud"
        elif days_old > 180:
            result["is_old"] = True
            result["severity"] = "yellow"
            result["note"] = f"Offer is {days_old} days old (6+ months) — unusually stale, may be resurrected"
        elif days_old > 90:
            result["severity"] = "yellow"
            result["note"] = f"Offer is {days_old} days old (3+ months) — consider verifying with company"
        else:
            result["severity"] = "green"
            result["note"] = f"Offer is {days_old} days old — recent, normal range"

    except Exception as e:
        result["note"] = f"Could not parse offer date: {str(e)}"

    return result


def compute_salary_math(fields: ExtractedFields) -> dict:
    """Check if salary components make sense relative to CTC."""
    s = fields.salary
    result = {
        "math_consistent": None,
        "discrepancy_note": "Insufficient data to verify salary math"
    }

    components = [
        s.basic or 0,
        s.hra or 0,
        s.special_allowance or 0,
        s.pf_employer or 0,
        s.gratuity or 0,
    ]
    component_sum = sum(components)

    if not s.ctc_annual or component_sum <= 100:
        return result

    if component_sum < s.ctc_annual / 6:
        component_sum *= 12

    pct = component_sum / s.ctc_annual

    if component_sum > s.ctc_annual * 1.05:
        result["math_consistent"]  = False
        result["discrepancy_note"] = (
            f"Salary components ({component_sum:,.0f}) exceed "
            f"stated CTC ({s.ctc_annual:,.0f}) — impossible"
        )
    elif pct < 0.40:
        result["math_consistent"]  = False
        result["discrepancy_note"] = (
            f"Salary components account for only {int(pct*100)}% of CTC "
            f"— too many major components missing or numbers manipulated"
        )
    else:
        result["math_consistent"]  = True
        result["discrepancy_note"] = (
            f"Salary components account for {int(pct*100)}% of CTC "
            f"— remainder is variable pay / benefits (normal)"
        )

    return result


def compute_completeness(fields: ExtractedFields, raw: RawPDFData) -> float:
    """Score how complete the extracted letter is."""
    expected = {
        "company_name":    fields.company_name,
        "company_address": fields.company_address,
        "candidate_name":  fields.candidate_name,
        "job_title":       fields.job_title,
        "hr_designation":  fields.hr_designation,
        "offer_date":      fields.offer_date,
        "joining_date":    fields.joining_date,
        "ctc_or_stipend":  fields.salary.ctc_annual or fields.salary.gross_monthly,
        "has_logo":        raw.images.has_logo or None,
        "has_signature":   raw.images.has_signature or None,
    }
    present = sum(1 for v in expected.values() if v)
    return round(present / len(expected), 2)


def scan_template_artifacts(text: str) -> list[str]:
    """Find unfilled template placeholders in the letter text."""
    found = []
    text_lower = text.lower()

    for artifact in settings.template_artifacts:
        if artifact.lower() in text_lower:
            found.append(artifact)

    bracket_matches = re.findall(r'\[[A-Z][A-Z\s]{2,}\]', text)
    for m in bracket_matches:
        if m not in found:
            found.append(m)

    return found


# ── PILLAR BUILDER ───────────────────────────────────────────────

def _build_pillar(
    score_or_none: Optional[int],
    weight: int,
    reasoning: str,
    conservative_reason: str = "Analysis incomplete — conservative score applied",
) -> PillarScore:
    """
    Build a PillarScore.
    - If score_or_none is None: apply conservative score = 50% of max.
      score_type = "unverified_conservative" → triggers hard gate.
    - Otherwise: verified score.
    conservative_reason lets callers customise the message for non-visual pillars.
    """
    if score_or_none is None:
        conservative = round(weight * 0.5, 1)
        return PillarScore(
            score=conservative,
            max=weight,
            reasoning=(
                f"{conservative_reason} "
                f"({conservative}/{weight}). Manual review required."
            ),
            score_type="unverified_conservative",
        )
    return PillarScore(
        score=float(score_or_none),
        max=weight,
        reasoning=reasoning,
        score_type="verified",
    )


def _online_reasoning(dns: DnsResult, company_online: CompanyOnlineResult,
                      score: int, max_score: int) -> str:
    """
    Build a fact-based reasoning string for the online-presence pillar.
    Reflects what our Python checks actually found — not Claude's text-only guess.
    """
    parts = []
    if dns.dns_valid:
        parts.append("domain resolves (+4)")
    elif dns.domain:
        parts.append(f"domain '{dns.domain}' does not resolve")
    else:
        parts.append("no domain to verify")

    if company_online.found:
        parts.append(f"online presence confirmed (+7) — {company_online.note}")
    else:
        parts.append(f"no online presence — {company_online.note}")

    return f"{score}/{max_score}: " + "; ".join(parts) + "."


# ── MAIN SCORING FUNCTION ────────────────────────────────────────

def compute_final_score(
    fields: ExtractedFields,
    raw: RawPDFData,
    company_online: CompanyOnlineResult,
    dns: DnsResult,
    analysis: AIAnalysisResult,
    images: AIImageResult,
    date_logic: dict,
    salary_math: dict,
    completeness: float,
    file_name: str,
    file_size_kb: int,
    processing_time_ms: int,
) -> AnalysisResult:
    """
    Score formula:  total_score = sum(all 9 pillar scores)
    Denominator is always 100.
    Unavailable visual checks → conservative score (50% of max).
    Hard gate: if any conservative score exists AND calc'd score ≥ 80 → cap at MANUAL REVIEW.
    """

    # ── COMPANY ONLINE SCORE — deterministic Python formula ─────────
    # DNS resolves (A record) = +4 pts  |  Company found online = +7 pts
    # Computed here, NOT delegated to Claude (Claude only provides reasoning).
    #
    # CRITICAL: distinguish "checked and found nothing" (a real negative, score 0)
    # from "could not check due to a network/system error" (unverified). A system
    # failure must NOT be scored as a genuine "company is fake" signal — otherwise
    # a transient DNS/network blip silently penalises a legitimate company.
    _online_unverifiable = dns.lookup_error or (not company_online.checked)
    _online_score = (4 if dns.dns_valid else 0) + (7 if company_online.found else 0)
    _online_score = min(_online_score, settings.weight_company_online)

    # ── BUILD SCORE BREAKDOWN ────────────────────────────────────────
    # All pillars go through _build_pillar so that a None score (parse failure
    # or genuinely unavailable) always gets a conservative 50% score rather
    # than silently becoming 0.
    breakdown = ScoreBreakdown(
        # Visual pillars — conservative when rendered pages not available
        image_tampering=_build_pillar(
            analysis.image_tampering_score,
            settings.weight_image_tampering,
            analysis.image_tampering_reasoning or images.logo_note or images.reasoning,
            conservative_reason="Images not extractable from PDF — conservative score applied",
        ),
        signature_tampering=_build_pillar(
            analysis.signature_tampering_score,
            settings.weight_signature_tampering,
            analysis.signature_tampering_reasoning or images.signature_note or images.reasoning,
            conservative_reason="Images not extractable from PDF — conservative score applied",
        ),
        # Text-based pillars — verified; None only on AI parse failure
        text_tampering=_build_pillar(
            analysis.text_tampering_score,
            settings.weight_text_tampering,
            analysis.text_tampering_reasoning,
            conservative_reason="AI response incomplete — conservative score applied",
        ),
        date_tampering=_build_pillar(
            analysis.date_tampering_score,
            settings.weight_date_tampering,
            analysis.date_tampering_reasoning,
            conservative_reason="AI response incomplete — conservative score applied",
        ),
        salary_details=_build_pillar(
            analysis.salary_details_score,
            settings.weight_salary_details,
            analysis.salary_details_reasoning,
            conservative_reason="AI response incomplete — conservative score applied",
        ),
        grammar_errors=_build_pillar(
            analysis.grammar_errors_score,
            settings.weight_grammar_errors,
            analysis.grammar_errors_reasoning,
            conservative_reason="AI response incomplete — conservative score applied",
        ),
        hr_signature_details=_build_pillar(
            analysis.hr_signature_details_score,
            settings.weight_hr_signature_details,
            analysis.hr_signature_details_reasoning,
            conservative_reason="AI response incomplete — conservative score applied",
        ),
        company_details=_build_pillar(
            analysis.company_details_score,
            settings.weight_company_details,
            analysis.company_details_reasoning,
            conservative_reason="AI response incomplete — conservative score applied",
        ),
        # Online presence — Python-computed, not Claude.
        # When verification could not actually run (network/system error), mark
        # the pillar conservative+unverified so the hard gate fires (no silent
        # 0, no auto-approve) instead of scoring a system failure as a negative.
        company_online=(
            _build_pillar(
                None,
                settings.weight_company_online,
                "",
                conservative_reason=(
                    "Online/DNS verification could not complete (network/system error) "
                    "— conservative score applied"
                ),
            )
            if _online_unverifiable
            else PillarScore(
                score=float(_online_score),
                max=settings.weight_company_online,
                reasoning=_online_reasoning(dns, company_online, _online_score, settings.weight_company_online),
                score_type="verified",
            )
        ),
    )

    # ── RAW TOTAL (denominator always 100) ───────────────────────
    all_pillars = [
        breakdown.image_tampering,
        breakdown.signature_tampering,
        breakdown.text_tampering,
        breakdown.date_tampering,
        breakdown.salary_details,
        breakdown.grammar_errors,
        breakdown.hr_signature_details,
        breakdown.company_details,
        breakdown.company_online,
    ]
    total_raw = sum(p.score for p in all_pillars)

    # ── HARD GATE CHECK ──────────────────────────────────────────
    has_conservative = any(
        p.score_type == "unverified_conservative" for p in all_pillars
    )

    # ── HARD PENALTIES ───────────────────────────────────────────
    penalties = []
    total_penalty = 0.0

    # Unfilled template placeholders
    template_found = scan_template_artifacts(raw.full_text)
    if template_found:
        total_penalty += settings.penalty_unfilled_template
        penalties.append({
            "reason": f"Unfilled placeholders: {', '.join(template_found[:4])}",
            "points": -settings.penalty_unfilled_template
        })

    # Red phrases
    if raw.red_phrases_found:
        penalty = min(
            len(raw.red_phrases_found) * settings.penalty_per_red_phrase,
            settings.penalty_red_phrase_cap
        )
        total_penalty += penalty
        penalties.append({
            "reason": f"Fraud language: {', '.join(raw.red_phrases_found)}",
            "points": -penalty
        })

    # Impossible date gap
    if date_logic.get("gap_impossible"):
        total_penalty += settings.penalty_date_impossible
        penalties.append({
            "reason": f"Date issue: {date_logic.get('gap_note')}",
            "points": -settings.penalty_date_impossible
        })

    # Salary math impossible (components exceed CTC)
    if salary_math.get("math_consistent") is False:
        total_penalty += settings.penalty_salary_math
        penalties.append({
            "reason": f"Salary math: {salary_math.get('discrepancy_note')}",
            "points": -settings.penalty_salary_math
        })

    # Suspicious metadata (design tools, heavy editors)
    if raw.metadata.suspicious_metadata:
        total_penalty += settings.penalty_suspicious_metadata
        penalties.append({
            "reason": f"Metadata: {raw.metadata.suspicious_reason}",
            "points": -settings.penalty_suspicious_metadata
        })

    # Online PDF editor (lighter penalty)
    if raw.metadata.online_edit_detected:
        total_penalty += settings.penalty_online_edit
        penalties.append({
            "reason": f"PDF processed with {raw.metadata.online_edit_tool} — verify original source",
            "points": -settings.penalty_online_edit
        })

    # Low completeness
    if completeness < settings.completeness_threshold:
        total_penalty += settings.penalty_low_completeness
        penalties.append({
            "reason": f"Document only {int(completeness*100)}% complete — key fields missing",
            "points": -settings.penalty_low_completeness
        })

    # Offer age
    offer_age = check_offer_age(date_logic.get("offer_date"))
    _offer_in_future = offer_age.get("days_old") is not None and offer_age["days_old"] < 0
    if offer_age.get("is_old"):
        if _offer_in_future:
            # Offer dated in the future is impossible — a real fraud signal.
            total_penalty += settings.penalty_date_impossible
            penalties.append({
                "reason": f"Offer age: {offer_age.get('note')}",
                "points": -settings.penalty_date_impossible
            })
        elif offer_age.get("days_old", 0) > 365:
            total_penalty += settings.penalty_offer_very_old
            penalties.append({
                "reason": f"Offer age: {offer_age.get('note')}",
                "points": -settings.penalty_offer_very_old
            })
        elif offer_age.get("days_old", 0) > 180:
            total_penalty += settings.penalty_offer_stale
            penalties.append({
                "reason": f"Offer age: {offer_age.get('note')}",
                "points": -settings.penalty_offer_stale
            })

    # Formula: sum of all 9 pillar scores — denominator is always 100.
    # Penalties are informational only (shown in UI); they do NOT reduce the score.
    # Parameter scores already reflect detected issues via the AI's lower scoring.
    final_score = max(0, int(round(total_raw)))

    # ── VERDICT + HARD GATE APPLICATION ─────────────────────────────
    hard_gate_triggered = False
    hard_gate_reason = ""

    # Gate 1: conservative scores (visual analysis incomplete)
    if final_score >= settings.verdict_legitimate:
        if has_conservative:
            verdict, verdict_color = "MANUAL REVIEW", "yellow"
            hard_gate_triggered = True
            hard_gate_reason = (
                "Visual analysis incomplete — auto-accept blocked. "
                "Human review of signatures and images required."
            )
        else:
            verdict, verdict_color = "LEGITIMATE", "green"
    elif final_score >= settings.verdict_manual_review:
        verdict, verdict_color = "MANUAL REVIEW", "yellow"
        if has_conservative:
            hard_gate_triggered = True
            hard_gate_reason = (
                "Visual analysis incomplete — conservative scores applied. "
                "Human review required."
            )
    else:
        verdict, verdict_color = "SUSPICIOUS", "red"

    # Gate 2: company GENUINELY has no online presence — block LEGITIMATE.
    # Must require checked=True and no DNS lookup error, so a network/system
    # failure (already handled as conservative/unverified above) is NOT
    # mislabelled here as "company doesn't exist".
    _no_online = (
        company_online.checked and not company_online.found
        and not dns.dns_valid and not dns.lookup_error
    )
    if _no_online and verdict == "LEGITIMATE":
        verdict, verdict_color = "MANUAL REVIEW", "yellow"
        hard_gate_triggered = True
        hard_gate_reason = (
            "Company has no verifiable online presence (DNS + web both failed) "
            "— cannot auto-approve."
        )

    # Gate 3: deterministic fraud markers — verdict cap (NOT a point subtraction).
    # Regex/math-certain signals that must block auto-approval regardless of the
    # AI pillar scores: fraud phrases, unfilled template placeholders, impossible
    # dates. Only letters that ACTUALLY contain a marker change verdict, so the
    # validated corpus and the 80/51 thresholds are untouched. Already-low scores
    # stay SUSPICIOUS; this only stops a marker-bearing letter from auto-approving.
    _fraud_markers = []
    if raw.red_phrases_found:
        _fraud_markers.append(f"fraud language ({', '.join(raw.red_phrases_found[:3])})")
    if template_found:
        _fraud_markers.append(f"unfilled placeholder(s) ({', '.join(template_found[:3])})")
    if date_logic.get("gap_impossible"):
        _fraud_markers.append("joining date before offer date (impossible)")
    if _offer_in_future:
        _fraud_markers.append("offer dated in the future (impossible)")

    if _fraud_markers and verdict == "LEGITIMATE":
        verdict, verdict_color = "MANUAL REVIEW", "yellow"
        hard_gate_triggered = True
        hard_gate_reason = (
            "Deterministic fraud marker(s) detected — "
            + "; ".join(_fraud_markers) + " — cannot auto-approve."
        )

    # Gate 3b: confirmed edit-laundering — an online-editor footprint AND a
    # physical edit artifact (floating/pasted date) present TOGETHER. This is the
    # signature of a document opened in an online editor, content-altered, and
    # re-saved — the re-save also strips the original metadata, so a laundered
    # fake can otherwise score HIGHER than its messy original. Requires BOTH
    # signals so a letter merely re-saved through a converter (no content edit)
    # is NOT caught. Deterministic tamper marker → cap verdict to SUSPICIOUS,
    # which lands a laundered copy below an untouched original regardless of the
    # raw pillar total. Does not subtract points or alter weights/thresholds.
    _edit_artifact = bool(raw.tamper_artifacts)
    _online_editor = bool(raw.metadata.online_edit_detected)
    if _edit_artifact and _online_editor and verdict != "SUSPICIOUS":
        _t_art = (raw.tamper_artifacts or ["edit artifact"])[0]
        verdict, verdict_color = "SUSPICIOUS", "red"
        hard_gate_triggered = True
        hard_gate_reason = (
            f"Confirmed edit-laundering: {raw.metadata.online_edit_tool or 'online editor'} "
            f"footprint together with an edit artifact ({_t_art}) — content was altered "
            "and re-saved. Treated as suspicious; manual verification against the original required."
        )

    # Gate 3c: composited / layered forgery — body text and a pasted signature/stamp
    # overlaid onto a real company's BLANK letterhead, then flattened (no text layer,
    # no fonts, big background image + small pasted images + heavy vector overlay). A
    # genuine letter is never built this way. Deterministic, structural, and validated
    # at 0 false positives over the full corpus → cap verdict to SUSPICIOUS.
    if raw.composite_artifacts and verdict != "SUSPICIOUS":
        verdict, verdict_color = "SUSPICIOUS", "red"
        hard_gate_triggered = True
        hard_gate_reason = (
            f"Composited document forgery: {raw.composite_artifacts[0]}. Content was "
            "overlaid onto a blank letterhead and flattened — fabricated, not a genuine letter."
        )

    # ── SCORE CLAMP — the number must honour a FRAUD/tamper verdict cap ──────
    # A deterministic fraud / forgery / no-online finding overrides the additive
    # pillar sum, so the displayed score never sits ABOVE the band of its (capped)
    # verdict — otherwise a laundered fake can show a higher number than a clean
    # original even while flagged SUSPICIOUS. EXCLUDES Gate 1 (visual incomplete):
    # "couldn't verify" must never lower the number (fail-loud — unverified ≠ fake).
    # Verdicts and the 80/51 thresholds are untouched; only the number is aligned.
    _fraud_cap = (
        bool(_fraud_markers) or _no_online
        or (_edit_artifact and _online_editor)
        or bool(raw.composite_artifacts)
    )
    if _fraud_cap:
        if verdict == "SUSPICIOUS":
            final_score = min(final_score, settings.verdict_manual_review - 1)  # ≤ 50
        elif verdict == "MANUAL REVIEW":
            final_score = min(final_score, settings.verdict_legitimate - 1)     # ≤ 79

    # ── SYNC recommended_action with verdict ─────────────────────────
    recommended_action = analysis.recommended_action
    # Hard gate overrides: if verdict is capped, action must not say APPROVE
    if hard_gate_triggered and recommended_action == "APPROVE":
        recommended_action = "REVIEW"
    # Extreme fraud signals
    if raw.red_phrases_found and final_score < 50:
        recommended_action = "REJECT"
    if template_found and final_score < 40:
        recommended_action = "REJECT"

    # ── FLAGS ─────────────────────────────────────────────────────
    all_flags = list(analysis.flags)

    # DNS informational flag
    if dns.domain and not dns.dns_valid:
        all_flags.append(AnalysisFlag(
            severity="yellow",
            title="Domain Does Not Resolve",
            detail=f"{dns.domain} — {dns.note or 'could not verify domain'}"
        ))

    if dns.domain and dns.dns_valid and not dns.mx_records_exist:
        all_flags.append(AnalysisFlag(
            severity="yellow",
            title="No Mail Server Found",
            detail=f"{dns.domain} has no MX records — company may use Gmail or third-party email"
        ))

    # Short gap
    if (date_logic.get("gap_days") is not None
            and 0 <= date_logic["gap_days"] < 3
            and not date_logic.get("gap_impossible")):
        all_flags.append(AnalysisFlag(
            severity="yellow",
            title="Very Short Joining Gap",
            detail=date_logic.get("gap_note", "")
        ))

    # Hard gate warning flag
    if hard_gate_triggered:
        all_flags.insert(0, AnalysisFlag(
            severity="yellow",
            title="Visual Analysis Incomplete",
            detail=hard_gate_reason,
        ))

    # Risk signal flags (penalties are informational — they do NOT reduce the score;
    # the AI already scores lower when it finds these issues)
    for p in penalties:
        all_flags.append(AnalysisFlag(
            severity="red" if abs(p["points"]) >= 10 else "yellow",
            title="Risk Signal Detected",
            detail=p["reason"],
        ))

    # ── D: AI scoring failure — surface loudly, never a silent ~50 ──
    if getattr(analysis, "analysis_failed", False):
        all_flags.insert(0, AnalysisFlag(
            severity="red",
            title="AI Scoring Failed",
            detail=("The AI returned an unreadable response, so all pillar scores "
                    "fell back to conservative values. This score is NOT reliable — "
                    "manual review required."),
        ))

    # ── E: PDF extraction degraded — surface processing failures ───
    # Skip the informational "Rendered N page(s)…" success line; surface only
    # warnings that indicate something failed (errors / missing text layer).
    # Informational SUCCESS lines (page renders, the signature close-up, and the
    # tamper/forgery scans — which raise their OWN dedicated flags/gates) are NOT
    # processing failures and must not be framed as "partial data". Surface only the
    # genuine degradations (errors, OCR fallback, missing text layer).
    _INFO_WARNINGS = (
        "Rendered ",
        "Signature close-up rendered",
        "Composite-forgery scan:",
        "Tamper scan:",
    )
    for w in (raw.extraction_warnings or []):
        if w.startswith(_INFO_WARNINGS):
            continue
        all_flags.append(AnalysisFlag(
            severity="yellow",
            title="Document Processing Issue",
            detail=f"{w} — some analysis may be based on partial data.",
        ))

    # Edit-artifact warning: a date floating as its own body line is a classic
    # copy-paste tamper trace (e.g. a changed joining date pasted back as a new
    # text object). Advisory Warning — escalated wording if the PDF also carries
    # an online-editor footprint (iLovePDF/Smallpdf), a strong combined signal.
    for art in (raw.tamper_artifacts or []):
        editor = f" PDF also processed with {raw.metadata.online_edit_tool}." if raw.metadata.online_edit_detected else ""
        all_flags.append(AnalysisFlag(
            severity="yellow",
            title="Possible Edit Artifact",
            detail=(f"{art} — a date appears outside the normal sentence/table flow, "
                    f"which can indicate the date was edited.{editor} Verify against the original."),
        ))

    # Sort: red first, then yellow, then green
    all_flags.sort(key=lambda f: {"red": 0, "yellow": 1, "green": 2}.get(f.severity, 1))

    # ── LETTER SUMMARY ────────────────────────────────────────────
    letter_summary = {
        "company_name":             fields.company_name,
        "company_domain":           fields.company_domain,
        "company_email":            fields.company_email,
        "company_cin":              fields.company_cin,
        "candidate_name":           fields.candidate_name,
        "job_title":                fields.job_title,
        "department":               fields.department,
        "hr_name":                  fields.hr_name,
        "hr_designation":           fields.hr_designation,
        "offer_date":               date_logic.get("offer_date"),
        "joining_date":             date_logic.get("joining_date"),
        "date_gap_days":            date_logic.get("gap_days"),
        "date_gap_valid":           date_logic.get("gap_valid"),
        "ctc_annual":               fields.salary.ctc_annual,
        "gross_monthly":            fields.salary.gross_monthly,
        "basic":                    fields.salary.basic,
        "hra":                      fields.salary.hra,
        "employment_type":          fields.employment_type,
        "work_location":            fields.work_location,
        "completeness_score":       completeness,
        "red_phrases_found":        raw.red_phrases_found,
        "template_artifacts_found": template_found,
        "salary_math_ok":           salary_math.get("math_consistent"),
        "extraction_notes":         fields.extraction_notes,
        "pdf_created_with":         raw.metadata.created_with,
        "pdf_author":               raw.metadata.author,
        "pdf_created_date":         raw.metadata.created_date,
        "pdf_modified_date":        raw.metadata.modified_date,
        "pdf_metadata_suspicious":  raw.metadata.suspicious_metadata,
        "pdf_metadata_reason":      raw.metadata.suspicious_reason,
    }

    return AnalysisResult(
        overall_score=final_score,
        verdict=verdict,
        verdict_color=verdict_color,
        recommended_action=recommended_action,
        summary=analysis.summary,
        score_breakdown=breakdown,
        flags=all_flags,
        penalties=penalties,
        letter_summary=letter_summary,
        image_details={
            "logo":             images.logo_assessment,
            "logo_note":        images.logo_note,
            "signature":        images.signature_assessment,
            "signature_note":   images.signature_note,
            "images_available": images.images_available,
            "has_logo":         raw.images.has_logo,
            "has_signature":    raw.images.has_signature,
            "logo_position":    raw.images.logo_position,
        },
        processing_time_ms=processing_time_ms,
        file_name=file_name,
        file_size_kb=file_size_kb,
        hard_gate_triggered=hard_gate_triggered,
        hard_gate_reason=hard_gate_reason,
    )

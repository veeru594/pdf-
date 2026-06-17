from pydantic import BaseModel
from typing import Optional


# ── RAW EXTRACTION FROM PDF ─────────────────────────────────────

class ImageData(BaseModel):
    has_logo: bool = False
    logo_position: Optional[str] = None
    logo_base64: Optional[str] = None
    has_signature: bool = False
    signature_position: Optional[str] = None
    signature_base64: Optional[str] = None
    has_stamp: bool = False
    stamp_base64: Optional[str] = None
    total_images_found: int = 0


class PDFMetadata(BaseModel):
    created_with: Optional[str] = None
    author: Optional[str] = None
    created_date: Optional[str] = None
    modified_date: Optional[str] = None
    suspicious_metadata: bool = False
    suspicious_reason: Optional[str] = None
    # Lighter flag: online editor detected but not a forgery tool
    online_edit_detected: bool = False
    online_edit_tool: Optional[str] = None
    # Post-creation modification tracking
    modified_after_creation: bool = False
    modification_gap_days: Optional[int] = None


class RawPDFData(BaseModel):
    """Everything extracted directly from the PDF — no Claude involved"""
    full_text: str = ""
    images: ImageData = ImageData()
    metadata: PDFMetadata = PDFMetadata()
    red_phrases_found: list[str] = []
    extraction_warnings: list[str] = []
    rendered_pages: list[str] = []   # base64 PNG renders of key pages (first + last + page 2 if exists)
    placeholder_scan: list[dict] = []  # per-page unfilled placeholder findings
    tamper_artifacts: list[str] = []  # edit traces, e.g. a date floating as its own body line
    signature_crop: Optional[str] = None  # high-res base64 PNG close-up of the signature zone (Option A)
    composite_artifacts: list[str] = []  # layered-forgery traces: blank letterhead + pasted sig/stamp + overlaid vector text


# ── CLAUDE EXTRACTED FIELDS ─────────────────────────────────────

class SalaryBreakup(BaseModel):
    ctc_annual: Optional[float] = None
    gross_monthly: Optional[float] = None
    net_monthly: Optional[float] = None
    basic: Optional[float] = None
    hra: Optional[float] = None
    pf_employee: Optional[float] = None
    pf_employer: Optional[float] = None
    gratuity: Optional[float] = None
    special_allowance: Optional[float] = None
    joining_bonus: Optional[float] = None
    other_components: dict = {}


class DnsResult(BaseModel):
    domain: Optional[str] = None
    dns_valid: bool = False
    mx_records_exist: bool = False
    note: Optional[str] = None
    # True when the A-record lookup hit a system/network error (timeout, resolver
    # failure) — i.e. we could NOT determine existence. Distinct from NXDOMAIN,
    # which is a genuine "domain does not exist" negative.
    lookup_error: bool = False


class ExtractedFields(BaseModel):
    """Fields extracted by AI from the full text"""
    company_name: Optional[str] = None
    company_address: Optional[str] = None
    company_email: Optional[str] = None
    company_domain: Optional[str] = None
    company_phone: Optional[str] = None
    company_website: Optional[str] = None
    company_cin: Optional[str] = None
    candidate_name: Optional[str] = None
    candidate_email: Optional[str] = None
    job_title: Optional[str] = None
    department: Optional[str] = None
    reporting_to: Optional[str] = None
    employment_type: Optional[str] = None
    work_location: Optional[str] = None
    hr_name: Optional[str] = None
    hr_designation: Optional[str] = None
    hr_email: Optional[str] = None
    offer_date: Optional[str] = None
    joining_date: Optional[str] = None
    salary: SalaryBreakup = SalaryBreakup()
    extraction_notes: Optional[str] = None


# ── AI ANALYSIS RESULTS ─────────────────────────────────────────

class PillarScore(BaseModel):
    score: float = 0            # float to support 5.5 conservative scores
    max: int = 0
    reasoning: str = ""
    score_type: str = "verified"  # "verified" | "unverified_conservative"

    @property
    def percentage(self) -> float:
        return (self.score / self.max * 100) if self.max > 0 else 0


class AnalysisFlag(BaseModel):
    severity: str  # red | yellow | green
    title: str
    detail: str

    @property
    def css_class(self) -> str:
        return f"flag-{self.severity}"

    @property
    def icon(self) -> str:
        return {"red": "✗", "yellow": "⚠", "green": "✓"}.get(self.severity, "•")


class AIAnalysisResult(BaseModel):
    """Results from AI's scoring analysis — all 9 pillars"""

    # ── Visual pillars (null when images not extractable) ────────
    image_tampering_score: Optional[int] = None
    image_tampering_reasoning: str = ""
    signature_tampering_score: Optional[int] = None
    signature_tampering_reasoning: str = ""

    # ── Text-based pillars ───────────────────────────────────────
    text_tampering_score: Optional[int] = None
    text_tampering_reasoning: str = ""
    date_tampering_score: Optional[int] = None
    date_tampering_reasoning: str = ""
    salary_details_score: Optional[int] = None
    salary_details_reasoning: str = ""
    grammar_errors_score: Optional[int] = None
    grammar_errors_reasoning: str = ""
    hr_signature_details_score: Optional[int] = None
    hr_signature_details_reasoning: str = ""
    company_details_score: Optional[int] = None
    company_details_reasoning: str = ""

    # ── Online presence pillar ───────────────────────────────────
    # (score is computed in Python — rules.py; only the qualitative note is AI-provided)
    company_online_reasoning: str = ""

    # ── Flags + structured check lists ──────────────────────────
    flags: list[AnalysisFlag] = []
    critical_issues: list[str] = []
    warnings: list[str] = []
    passed_checks: list[str] = []

    # ── Domain rescued from letterhead images ───────────────────
    # Populated when analyze_letter can read the domain from rendered page images
    # even though regex/Claude-text extraction returned None.
    company_domain_found: Optional[str] = None
    # Company name read from the rendered letterhead/sign-off — rescues a missing
    # company_name when regex/text extraction failed (e.g. lowercase-styled brands).
    company_name_found: Optional[str] = None

    # ── Failure signal ───────────────────────────────────────────
    # True when the AI scoring call returned an unparseable/empty response, so
    # all pillar scores fell back to conservative. Lets scoring surface this
    # loudly (flag + hard gate) instead of presenting a silent ~50 score.
    analysis_failed: bool = False

    # ── Summary ──────────────────────────────────────────────────
    summary: str = ""
    recommended_action: str = "REVIEW"


class AIImageResult(BaseModel):
    """Results from AI's image analysis — qualitative only. Scores live in AIAnalysisResult."""
    logo_assessment: str = "absent"
    logo_note: str = ""
    signature_assessment: str = "absent"
    signature_note: str = ""
    stamp_assessment: str = "absent"
    images_available: bool = False      # True when at least one image was found + analyzed
    reasoning: str = ""


# ── COMPANY ONLINE CHECK ────────────────────────────────────────

class CompanyOnlineResult(BaseModel):
    found: bool = False
    score: int = 0
    note: str = ""
    # False when every verification attempt hit a network/system error, so we
    # could NOT actually check. Distinct from found=False with checked=True,
    # which means we searched and genuinely found no presence.
    checked: bool = True


# ── FINAL RESULT ────────────────────────────────────────────────

class ScoreBreakdown(BaseModel):
    image_tampering: PillarScore = PillarScore(score=0, max=11)
    text_tampering: PillarScore = PillarScore(score=0, max=11)
    signature_tampering: PillarScore = PillarScore(score=0, max=11)
    date_tampering: PillarScore = PillarScore(score=0, max=11)
    salary_details: PillarScore = PillarScore(score=0, max=11)
    grammar_errors: PillarScore = PillarScore(score=0, max=12)
    hr_signature_details: PillarScore = PillarScore(score=0, max=11)
    company_details: PillarScore = PillarScore(score=0, max=11)
    company_online: PillarScore = PillarScore(score=0, max=11)


class AnalysisResult(BaseModel):
    """The complete final result returned to the portal"""
    overall_score: int = 0
    verdict: str = ""
    verdict_color: str = ""
    recommended_action: str = "REVIEW"
    summary: str = ""
    score_breakdown: ScoreBreakdown = ScoreBreakdown()
    flags: list[AnalysisFlag] = []
    penalties: list[dict] = []
    letter_summary: dict = {}
    image_details: dict = {}
    processing_time_ms: int = 0
    file_name: str = ""
    file_size_kb: int = 0
    hard_gate_triggered: bool = False
    hard_gate_reason: str = ""

    @property
    def verdict_css_class(self) -> str:
        return {
            "green":  "verdict-high",
            "yellow": "verdict-medium",
            "red":    "verdict-low",
        }.get(self.verdict_color, "verdict-medium")

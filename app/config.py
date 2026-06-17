from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from .env file"""

    # Anthropic API
    anthropic_api_key: str = ""
    claude_model: str = "claude-3-5-sonnet-20241022"

    # App metadata
    app_name: str = "OfferVerify — Offer Letter Authenticity System"
    app_version: str = "1.0.0"

    # File upload
    max_file_size_mb: int = 50

    # ── SCORING WEIGHTS ─────────────────────────────────────────
    # Total = 100 points
    weight_image_tampering: int = 11       # AI vision: logo/image authenticity
    weight_text_tampering: int = 11        # AI: font inconsistency, copy-paste, candidate name
    weight_signature_tampering: int = 11   # AI vision: signature authenticity
    weight_date_tampering: int = 11        # AI: date manipulation + completeness
    weight_salary_details: int = 11        # AI: compensation clarity, components, market rate
    weight_grammar_errors: int = 12        # AI: language quality + employment terms
    weight_hr_signature_details: int = 11  # AI: HR name, designation, sign-off
    weight_company_details: int = 11       # AI: company info, CIN/GST validity
    weight_company_online: int = 11        # External: DNS + web presence (merged)

    # ── HARD PENALTIES ──────────────────────────────────────────
    penalty_per_red_phrase: int = 10      # each fraud phrase found
    penalty_red_phrase_cap: int = 30      # max penalty from red phrases
    penalty_date_impossible: int = 15     # joining date BEFORE offer date
    penalty_salary_math: int = 12         # salary components don't add up
    penalty_suspicious_metadata: int = 8  # created in Canva / Photoshop / heavy editor
    penalty_online_edit: int = 2          # PDF run through iLovePDF / Smallpdf etc. (minor)
    penalty_low_completeness: int = 10    # too many missing fields
    completeness_threshold: float = 0.6   # below this = penalty
    penalty_unfilled_template: int = 15   # [NAME] [DATE] placeholders left in
    penalty_offer_very_old: int = 20      # offer > 1 year old (reused fraud)
    penalty_offer_stale: int = 10         # offer 6-12 months old (suspicious)

    # ── VERDICT THRESHOLDS ──────────────────────────────────────
    # Percentage-based scoring: (scored_sum / max_sum) × 100 − penalties
    verdict_legitimate: int = 80          # ≥ 80 → LEGITIMATE
    verdict_manual_review: int = 51       # 51–79 → MANUAL REVIEW
    # < 51 → SUSPICIOUS

    # ── DATE LOGIC ──────────────────────────────────────────────
    min_joining_gap_days: int = 1         # gaps above 0 are valid (short is flagged)
    max_joining_gap_days: int = 90

    # ── RED PHRASES ─────────────────────────────────────────────
    red_phrases: list[str] = [
        # ── FINANCIAL FRAUD SIGNALS ──
        "processing fee",
        "registration fee",
        "security deposit",
        "transfer the amount",
        "pay to confirm",
        "Western Union",
        "wire transfer",
        "kindly deposit",
        "refundable deposit",
        "courier charges",
        "training fee",
        "medical fee to be paid",
        "provide bank details",
        "bank account number",

        # ── CONFIDENTIALITY ABUSE ──
        "do not share this letter",
        "keep this offer confidential",
        "do not contact hr",
        "do not verify with hr",
        "do not reach out to company",
        "verify with your company",

        # ── URGENCY/PRESSURE TACTICS ──
        "confirm by clicking",
        "valid for 24 hours",
        "expires in 24 hours",
        "limited time offer",
        "respond immediately",
        "confirm immediately",
        "accept within 24 hours",

        # ── CLASSIC FRAUD MARKERS ──
        "send your details to claim",
        "failure to respond will result in legal action",
        "legally binding offer letter",
        "dummy offer",
        "will call you with the joining details",
        "collect from office after joining",
    ]

    # ── TEMPLATE ARTIFACT PATTERNS ──────────────────────────────
    # Unfilled placeholders left in the letter — hard penalty
    template_artifacts: list[str] = [
        "[candidate name]",
        "[your name]",
        "[name]",
        "[address]",
        "[date]",
        "[position name]",
        "[reference number]",
        "[salary]",
        "[department]",
        "[company name]",
        # NOTE: bare "____" and "xxxx" were removed — they matched signature/
        # acceptance lines ("I, ____, accept") and masked IDs ("XXXX8274"),
        # not unfilled template fields. Genuine unfilled placeholders are caught
        # by the bracketed entries above + the [A-Z]{2,} bracket regex in
        # rules.scan_template_artifacts.
    ]

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = False


settings = Settings()
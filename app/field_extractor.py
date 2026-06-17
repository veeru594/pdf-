"""
field_extractor.py — Extract structured fields from offer letter text
using regex and heuristic patterns. No Claude API call.

Used for all PDFs where pdfplumber extracted readable text.
Claude extract_fields() is only called when the PDF went through OCR
(scanned / image-based), where the text quality may be too poor for regex.

Saves one full Claude API call per analysis (~50% cost reduction).
"""
import re
import logging
from typing import Optional
from datetime import datetime
from dateutil import parser as dateparser

from app.models import ExtractedFields, SalaryBreakup, RawPDFData

logger = logging.getLogger(__name__)


# ── Public API ───────────────────────────────────────────────────

def extract_fields_from_text(raw: RawPDFData) -> ExtractedFields:
    """
    Pure Python field extraction — no Claude API call.
    Parses the already-extracted text using regex + heuristics.
    """
    text = raw.full_text
    if not text or not text.strip():
        return ExtractedFields(extraction_notes="Empty text")

    # Strip icon-font glyphs (Unicode PUA) and emoji BEFORE any regex runs.
    # Design tools (Canva, etc.) embed these as decorative icons next to — or
    # even inside — URLs and emails in the PDF text stream, corrupting matches.
    text = _ascii_text(text)

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

    company_email   = _extract_company_email(text)
    candidate_email = _extract_candidate_email(text)
    domain          = _extract_domain(company_email, text)
    website         = _extract_website(text)
    phone           = _extract_phone(text)
    cin             = _extract_cin(text)
    company_name    = _extract_company_name(text, lines)
    candidate_name  = _extract_candidate_name(text, lines)
    job_title       = _extract_job_title(text, lines)
    emp_type        = _extract_employment_type(text)
    location        = _extract_location(text, lines)
    offer_date      = _extract_offer_date(text, lines)
    joining_date    = _extract_joining_date(text, offer_date)  # Pass offer_date for validation
    address         = _extract_address(text, lines)
    salary          = _extract_salary(text)
    hr_name, hr_desig, hr_email = _extract_hr(text, lines)

    # If company name equals or is contained within candidate name, it's a mismatch
    if company_name and candidate_name:
        if (company_name.lower() == candidate_name.lower()
                or company_name.lower() in candidate_name.lower()
                or candidate_name.lower() in company_name.lower()):
            company_name = None

    # A job title that duplicates the company or candidate name is a mis-grab.
    if job_title and (
        job_title.strip().lower() == (company_name or "").strip().lower()
        or job_title.strip().lower() == (candidate_name or "").strip().lower()
    ):
        job_title = None

    # HR name sometimes swallows the candidate name from an adjacent line
    # ("Puneet" + "Mohammad Akram Pasha"). Strip the candidate back out.
    if hr_name and candidate_name and candidate_name.lower() in hr_name.lower():
        stripped = re.sub(re.escape(candidate_name), "", hr_name, flags=re.IGNORECASE).strip()
        hr_name = _clean_person_name(stripped)

    fields = ExtractedFields(
        company_name      = company_name,
        company_address   = address,
        company_email     = company_email,
        company_domain    = domain,
        company_phone     = phone,
        company_website   = website,
        company_cin       = cin,
        candidate_name    = candidate_name,
        candidate_email   = candidate_email,
        job_title         = job_title,
        employment_type   = emp_type,
        work_location     = location,
        hr_name           = hr_name,
        hr_designation    = hr_desig,
        hr_email          = hr_email,
        offer_date        = offer_date,
        joining_date      = joining_date,
        salary            = salary,
        extraction_notes  = "regex",
    )

    filled = sum(1 for v in [
        company_name, candidate_name, job_title, offer_date,
        joining_date, salary.ctc_annual, domain,
    ] if v)
    logger.info(
        f"[REGEX EXTRACT] {filled}/7 key fields found — "
        f"company={company_name!r} candidate={candidate_name!r} "
        f"job={job_title!r} ctc={salary.ctc_annual} "
        f"offer={offer_date} joining={joining_date} domain={domain!r}"
    )
    return fields


def is_low_confidence(fields: ExtractedFields) -> bool:
    """
    True when regex extraction is too sparse to trust for pipeline checks.
    The caller can then fall back to Claude extract_fields().
    A company name that looks like a sentence is treated as missing.
    """
    # No contact info at all → letterhead may be an embedded image that regex
    # cannot read.  Force Claude fallback so it can find domain/email/website
    # from its text-comprehension pass (it still won't see images at this stage,
    # but its NLP picks up contact details regex misses).
    has_contact = bool(
        fields.company_domain or fields.company_email or fields.company_website
    )
    if not has_contact:
        return True

    # Implausible values are worse than missing ones: a wrong CTC (e.g. 4,116
    # from a mis-parsed "3.43 Lakh") or a date grabbed from a statutory clause
    # silently routes a genuine letter down the fraud path. Treat any internally
    # impossible extraction as low-confidence so Claude re-extracts the letter.
    if _plausibility_problems(fields):
        return True

    company_ok = bool(fields.company_name and _is_valid_company_name(fields.company_name))
    critical = [
        company_ok,
        bool(fields.candidate_name),
        bool(fields.offer_date or fields.joining_date),
    ]
    return sum(1 for v in critical if v) < 2


def _plausibility_problems(fields: ExtractedFields) -> list[str]:
    """
    Sanity-check the extracted values. A non-empty list means regex produced a
    value that is internally impossible — a wrong CTC, a date grabbed from a
    statutory clause, components that exceed CTC, etc. Such values must NOT be
    trusted: the caller (is_low_confidence) routes the letter to the Claude
    extraction fallback instead of letting the bad value skew the verdict.

    These are the cheap, magnitude-independent checks that quantified the
    parsing breakage in audit_extraction.py — kept here as the single source of
    truth so the audit and the live pipeline test exactly the same conditions.
    """
    problems: list[str] = []
    s = fields.salary
    ctc = s.ctc_annual

    if ctc is not None:
        if ctc < 20_000:
            problems.append(f"CTC implausibly low ({ctc:,.0f}) — likely mis-parsed")
        if s.basic and s.basic > ctc:
            problems.append(f"basic ({s.basic:,.0f}) exceeds CTC ({ctc:,.0f})")
        comp = sum(x or 0 for x in [s.basic, s.hra, s.special_allowance,
                                    s.pf_employer, s.gratuity])
        if comp > 100 and comp > ctc * 1.05:
            problems.append(f"salary components ({comp:,.0f}) exceed CTC ({ctc:,.0f})")

    od, jd = fields.offer_date, fields.joining_date
    if od and jd and jd < od:
        problems.append(f"joining date {jd} precedes offer date {od}")

    this_year = datetime.now().year
    for label, d in (("offer", od), ("joining", jd)):
        if not d:
            continue
        try:
            yr = int(d[:4])
        except ValueError:
            problems.append(f"{label} date unparseable ({d})")
            continue
        if yr < 2018 or yr > this_year + 2:
            problems.append(f"{label} date year out of range ({d})")

    return problems


# ── Dates ────────────────────────────────────────────────────────

# Shared date sub-patterns. Real Indian offer letters mix 4- and 2-digit years,
# ordinal suffixes ("16th"), and '-' '.' ' ' ',' separators — e.g.
# "16th February 2026", "16-Feb-26", "16/02/2026", "16.02.26". The old patterns
# only accepted a 4-digit year with a plain-space separator, so a joining date
# written as "16-Feb-26" or "16th February 2026" was silently missed.
_MONTH = (
    r'(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|'
    r'Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)'
)
_DAY = r'\d{1,2}(?:st|nd|rd|th)?'   # day-of-month with optional ordinal suffix
_YR  = r'(?:\d{4}|\d{2})'           # 4-digit year, or 2-digit (e.g. "26" → 2026)

# A single date token in any common offer-letter format. ISO branch is first so
# a full 4-digit year is preferred when both interpretations could apply.
_DATE_TOKEN = (
    r'(?:'
    r'\d{4}-\d{2}-\d{2}'                                          # 2026-02-16 (ISO)
    r'|\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]' + _YR +                    # 16/02/2026, 16-02-26
    r'|' + _DAY + r'[\s\-\.]+' + _MONTH + r'[\s\-\.,]+' + _YR +   # 16th February 2026, 16-Feb-26
    r'|' + _MONTH + r'[\s\-\.,]+' + _DAY + r'[\s\-\.,]+' + _YR +  # February 16, 2026
    r')'
)

_DATE_RE = re.compile(r'\b(' + _DATE_TOKEN + r')\b', re.IGNORECASE)


def _plausible_joining(joining: Optional[str], offer_date: Optional[str]) -> bool:
    """
    A joining date is plausible only when it does not precede the offer date.
    When the offer date is unknown, reject clearly stale dates (e.g. a statutory
    "with effect from 01.07.2017" clause buried in a multi-page contract) by
    requiring a recent year. This is what stops stray legal-boilerplate dates
    from being mistaken for the joining date.
    """
    if not joining:
        return False
    if offer_date:
        return joining >= offer_date           # cannot join before being offered
    return joining[:4] >= str(datetime.now().year - 1)

def _parse_date(raw: str) -> Optional[str]:
    """Parse a date string to YYYY-MM-DD. Returns None on failure."""
    try:
        dt = dateparser.parse(raw, dayfirst=True)
        return dt.strftime("%Y-%m-%d") if dt else None
    except Exception:
        return None

def _find_date_near(text: str, keywords: list[str], window: int = 120) -> Optional[str]:
    """Find the first date within `window` chars after any of the given keywords."""
    text_lc = text.lower()
    for kw in keywords:
        idx = text_lc.find(kw.lower())
        if idx == -1:
            continue
        snippet = text[idx: idx + window]
        m = _DATE_RE.search(snippet)
        if m:
            raw = next(g for g in m.groups() if g)
            parsed = _parse_date(raw)
            if parsed:
                return parsed
    return None


def _find_dates_near(text: str, keywords: list[str], window: int = 120) -> list[str]:
    """Find ALL dates within `window` chars after any of the given keywords.
    Returns a list of parsed dates (YYYY-MM-DD format), in order of appearance."""
    dates = []
    text_lc = text.lower()
    for kw in keywords:
        idx = text_lc.find(kw.lower())
        if idx == -1:
            continue
        snippet = text[idx: idx + window]
        for m in _DATE_RE.finditer(snippet):
            raw = next(g for g in m.groups() if g)
            parsed = _parse_date(raw)
            if parsed and parsed not in dates:  # Avoid duplicates
                dates.append(parsed)
    return dates

def _extract_offer_date(text: str, lines: list[str]) -> Optional[str]:
    # Look for "Date:" at start of a line (letterhead date)
    for line in lines[:20]:
        m = re.match(r'^[Dd]ate\s*[:\-]\s*(.+)$', line)
        if m:
            d = _parse_date(m.group(1).strip())
            if d:
                return d
    # Look for "dated:" / "issue date"
    d = _find_date_near(text, ['dated:', 'date of issue', 'issue date', 'letter date'])
    if d:
        return d
    # Fall back: first date in the document header (first 500 chars)
    m = _DATE_RE.search(text[:500])
    if m:
        raw = next(g for g in m.groups() if g)
        return _parse_date(raw)
    return None

def _extract_joining_date(text: str, offer_date: Optional[str] = None) -> Optional[str]:
    """
    Extract joining date from offer letter with intelligent validation.
    
    Strategy (in priority order):
    1. Look for "Date of Joining" / "Joining date" patterns (usually in Annexure)
    2. Filter to only recent dates (2024+)
    3. If offer_date is known, reject any joining_date < offer_date (impossible)
    4. Pick the most recent valid date if multiple found
    5. Return None to trigger Claude fallback if no valid dates
    
    This avoids hallucinating old metadata dates while capturing true joining dates
    from the letter body and annexures.
    """
    # Look for explicit "Date of Joining: " patterns (usually in Annexure tables).
    # Highest-confidence signal. The separator is optional ([:\-|] or just
    # whitespace/newline) so it also matches table cells where pdfplumber places
    # the label and value next to each other without a colon (e.g. the cell pair
    # "Date of Joining" / "16-Feb-26").
    explicit_pattern = re.search(
        r'(?:Date\s+of\s+Joining|Joining\s+Date|Expected\s+Joining\s+Date|'
        r'Joining\s+On|Expected\s+to\s+Join|Date\s+of\s+Commencement)'
        r'\s*[:\-|]?\s*(' + _DATE_TOKEN + r')',
        text,
        re.IGNORECASE,
    )
    if explicit_pattern:
        parsed = _parse_date(explicit_pattern.group(1))
        if _plausible_joining(parsed, offer_date):
            return parsed

    # Fallback: keyword-based search for broader context.
    candidates = _find_dates_near(text, [
        'joining date', 'date of joining', 'join on', 'joining on',
        'report on', 'report to duty', 'reporting date', 'commencement',
        'start date', 'starting from', 'effective from', 'with effect from', 'w.e.f',
        'date of commencement', 'expected to join', 'you are expected to report',
    ], window=150)

    valid_candidates = [d for d in candidates if _plausible_joining(d, offer_date)]
    if valid_candidates:
        # The earliest plausible date on/after the offer is the most likely
        # joining date; later keyword-near matches tend to be review/appraisal
        # dates or statutory "with effect from <future>" clauses.
        return min(valid_candidates)

    # Nothing plausible — return None so the Claude extraction fallback handles
    # this critical field, rather than emitting a wrong date.
    return None


# ── Emails ───────────────────────────────────────────────────────

_EMAIL_RE = re.compile(r'([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})')
# More permissive version that handles malformed spacing from PDF extraction
_EMAIL_RE_PERMISSIVE = re.compile(r'([A-Za-z0-9._%+\-]+\s*@\s*[A-Za-z0-9.\-]+\.[A-Za-z]{2,})')

_PERSONAL_DOMAINS = {
    'gmail.com', 'yahoo.com', 'yahoo.co.in', 'outlook.com', 'hotmail.com',
    'live.com', 'rediffmail.com', 'protonmail.com', 'icloud.com', 'aol.com',
}

def _extract_company_email(text: str) -> Optional[str]:
    """First non-personal email found in the document."""
    # Try strict pattern first
    for m in _EMAIL_RE.finditer(text):
        addr = m.group(1).lower().replace(' ', '')
        domain = addr.split('@')[1]
        if domain not in _PERSONAL_DOMAINS:
            return addr
    # Try permissive pattern (handles spacing issues from PDF extraction)
    for m in _EMAIL_RE_PERMISSIVE.finditer(text):
        addr = m.group(1).lower().replace(' ', '')
        domain = addr.split('@')[1]
        if domain not in _PERSONAL_DOMAINS:
            return addr
    # Fall back to any email
    m = _EMAIL_RE.search(text)
    if m:
        return m.group(1).lower().replace(' ', '')
    return None

def _extract_candidate_email(text: str) -> Optional[str]:
    """Look for candidate email near "To:", "Dear", or personal domain emails."""
    header = text[:2000]  # Expanded to catch emails not in first 1500 chars
    
    # Priority 1: Look for personal domain emails (gmail, yahoo, etc.) in header
    for m in _EMAIL_RE.finditer(header):
        addr = m.group(1).lower().replace(' ', '')
        domain = addr.split('@')[1]
        if domain in _PERSONAL_DOMAINS:
            return addr
    
    # Priority 2: Look near "Dear " salutation
    dear_match = re.search(r'dear\s+([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})', text[:500], re.IGNORECASE)
    if dear_match:
        return dear_match.group(1).lower().replace(' ', '')
    
    return None


# ── Domain / website ─────────────────────────────────────────────

def _ascii_text(text: str) -> str:
    """Replace non-ASCII characters (icon font glyphs, emoji) with a space.
    Icon fonts embed glyphs as Unicode Private Use Area chars that land inside
    URLs/emails in the PDF text stream and break regex matches."""
    return re.sub(r'[^\x00-\x7F]', ' ', text)


def _extract_domain(company_email: Optional[str], text: str) -> Optional[str]:
    """Prefer company email domain. Fall back to website URL domain."""
    ascii_text = _ascii_text(text)

    # Priority 1: Company email domain
    if company_email:
        email_domain = company_email.split('@')[1].lower().strip()
        # Strip any residual non-ASCII that may have survived (e.g. trailing icon)
        email_domain = _ascii_text(email_domain).strip()
        if email_domain and re.match(r'^[a-z0-9][a-z0-9.\-]*\.[a-z]{2,}$', email_domain):
            return email_domain

    # Priority 2: Website URL extracted from ASCII-cleaned text
    site = _extract_website(ascii_text)
    if site:
        site_clean = site.lower().strip()
        m = re.search(r'(?:https?://)?(?:www\.)?([A-Za-z0-9][A-Za-z0-9.\-]*\.[A-Za-z]{2,})', site_clean)
        if m:
            domain = m.group(1).lower().lstrip('.')
            if domain and domain.count('.') >= 1:
                return domain

    # Priority 3: Broad domain scan on ASCII-cleaned text (last resort)
    # Catches domains that appear without www. prefix or email context
    _GENERIC = {'gmail.com', 'yahoo.com', 'yahoo.co.in', 'outlook.com',
                'hotmail.com', 'live.com', 'rediffmail.com'}
    m = re.search(
        r'\b([a-z0-9][a-z0-9\-]{2,}\.(com|in|co\.in|org\.in|net|org))\b',
        ascii_text, re.IGNORECASE,
    )
    if m:
        candidate = m.group(0).lower()
        if candidate not in _GENERIC:
            return candidate

    return None

def _extract_website(text: str) -> Optional[str]:
    # Pattern 1: Labeled websites (Website:, Web:, URL:, www:)
    m = re.search(
        r'(?:Website|Web|URL|www)[:\s]+'
        r'((?:https?://)?(?:www\.)?[A-Za-z0-9.\-]+\.[A-Za-z]{2,}(?:/[^\s]*)?)',
        text, re.IGNORECASE,
    )
    if m:
        url = m.group(1).strip().rstrip('.,;')
        return url if url else None
    
    # Pattern 2: Bare www. reference (most common in letterheads)
    m = re.search(r'(www\.[A-Za-z0-9.\-]+\.[A-Za-z]{2,}(?:/[^\s]*)?)', text, re.IGNORECASE)
    if m:
        return m.group(1)
    
    # Pattern 3: Domain-only reference (edge case)
    m = re.search(r'(?:site|domain)[:\s]+([A-Za-z0-9.\-]+\.[A-Za-z]{2,})', text, re.IGNORECASE)
    if m:
        url = m.group(1).strip().rstrip('.,;')
        return url if url and url.count('.') >= 1 else None
    
    return None


# ── Phone ────────────────────────────────────────────────────────

def _extract_phone(text: str) -> Optional[str]:
    m = re.search(
        r'(?:\+91[\s\-]?)?(?:\(0\d{2,4}\)[\s\-]?)?\d{10}'
        r'|(?:\+91[\s\-]?)?\d{5}[\s\-]\d{5}',
        text,
    )
    return m.group(0).strip() if m else None


# ── CIN / GST / Registration ─────────────────────────────────────

def _extract_cin(text: str) -> Optional[str]:
    # CIN: L/U + 5digits + 2letters + 4digits + 3letters + 6digits
    m = re.search(r'\b([LU]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6})\b', text)
    if m:
        return m.group(1)
    # GST: 2digits + 5letters + 4digits + 1letter + 1alphanumeric + Z + 1alphanumeric
    m = re.search(r'\b(\d{2}[A-Z]{5}\d{4}[A-Z][A-Z\d]Z[A-Z\d])\b', text)
    if m:
        return m.group(1)
    # UDYAM
    m = re.search(r'\b(UDYAM-[A-Z]{2}-\d{2}-\d{7})\b', text, re.IGNORECASE)
    if m:
        return m.group(1).upper()
    return None


# ── Company name ─────────────────────────────────────────────────

# Suffix must appear at END of line — prevents body sentences like
# "your services may be transferred" from matching as a company name.
_COMPANY_LINE_RE = re.compile(
    r'^([A-Za-z0-9 &\.\'\-]{2,60}'
    r'(?:Pvt\.?\s*Ltd\.?|Private\s+Limited|Public\s+Limited|Limited|'
    r'LLP|LLC|Inc\.?|Corp\.?|Corporation|Industries|Solutions|'
    r'Technologies|Tech|Services|Consulting|Foundation|Trust|'
    r'Enterprises|Associates|Group|Holdings)\.?)\s*$',
    re.IGNORECASE,
)

# Words that indicate a sentence, not a company name
_SENTENCE_WORDS = {
    'during', 'employment', 'with', 'the', 'your', 'our', 'you', 'will',
    'may', 'be', 'transferred', 'any', 'operating', 'office',
    'or', 'and', 'but', 'that', 'this', 'these', 'those', 'from', 'into',
    'by', 'for', 'at', 'on', 'in', 'is', 'are', 'was', 'were', 'have',
    'has', 'had', 'all', 'which', 'who', 'when', 'where', 'how', 'if',
    'not', 'an', 'as', 'it', 'its',
}

# Common non-company header/footer phrases that can appear as standalone lines
_COMPANY_NAME_BLACKLIST = {
    'privileged & confidential', 'privileged and confidential',
    'private & confidential', 'private and confidential',
    'strictly confidential', 'confidential',
    'accepted and agreed', 'without prejudice',
    'human resources', 'human resource', 'hr department',
    'offer letter', 'appointment letter', 'employment letter',
    'terms and conditions', 'terms & conditions',
    # Document-title headings that were being grabbed as the company name
    'offer of employment', 'offer of appointment', 'letter of intent',
    'letter of appointment', 'job offer', 'offer of internship',
    'internship offer letter', 'employment agreement', 'appointment order',
    'letter of employment', 'private limited',
}

def _is_valid_company_name(name: str) -> bool:
    """Reject strings that look like body sentences or reference codes, not company names."""
    if not name or len(name) < 3 or len(name) > 80:
        return False
    # Must start with a letter or digit, and contain at least one uppercase
    # letter somewhere. This allows lowercase-styled brand names common in
    # Indian IT (e.g. "iEnergizer", "eClerx", "iGate") while still rejecting
    # all-lowercase sentence fragments like "during employment with".
    if not name[0].isalnum():
        return False
    if not any(c.isupper() for c in name):
        return False
    # Blacklisted phrases
    if name.lower().strip() in _COMPANY_NAME_BLACKLIST:
        return False
    # Contains characters typical of reference numbers / file paths / contact rows
    if re.search(r'[!/|@]', name):
        return False
    # Contact / address lines are not company names (e.g. "Tel: +91 80 ...",
    # "New Delhi 110049"). Reject phone/email markers and 6-digit PIN codes.
    if re.search(r'\b(tel|phone|mobile|fax|email|e-mail|www|http)\b', name, re.IGNORECASE):
        return False
    if re.search(r'\b\d{6}\b', name):
        return False
    # All-uppercase string mixed with digits = reference code (e.g. "TTBS120705", "REF123")
    if re.match(r'^[A-Z0-9 \-]+$', name) and re.search(r'\d', name):
        return False
    # If ≥ 2 sentence-words appear in the string, it's a sentence not a name
    words = set(name.lower().split())
    if len(words & _SENTENCE_WORDS) >= 2:
        return False
    # Real company names are short phrases (≤ 6 words). Anything longer is a
    # clause fragment ("...out the facility management services entrusted to").
    if len(name.split()) > 6:
        return False
    # A bare job-title with no corporate suffix is not a company name
    # ("Associate Data Processing"). Fails safe: a false reject routes to Claude.
    _has_suffix = re.search(
        r'\b(pvt|ltd|limited|llp|inc|corp|corporation|industries|solutions|'
        r'technologies|services|consulting|enterprises|group|holdings|'
        r'associates|ventures|systems|foundation|trust|company|co)\b',
        name, re.IGNORECASE)
    _job_lead = {'associate', 'executive', 'trainee', 'officer', 'manager',
                 'analyst', 'intern', 'engineer', 'consultant', 'specialist',
                 'assistant', 'coordinator', 'senior', 'junior'}
    if not _has_suffix and name.split() and name.split()[0].lower() in _job_lead:
        return False
    # Must not contain sentence punctuation (commas, semicolons in middle)
    if re.search(r'[,;]', name):
        return False
    return True

def _extract_company_name(text: str, lines: list[str]) -> Optional[str]:
    # 1. "For <Company Pvt. Ltd.>" in sign-off — strongest signal
    m = re.search(
        r'\bFor\s+([A-Za-z][A-Za-z0-9 &\.\-\']{2,60}'
        r'(?:Pvt\.?\s*Ltd\.?|Limited|LLP|Inc\.?|Corp\.?|'
        r'Technologies|Solutions|Services|Foundation|Trust)\.?)',
        text, re.IGNORECASE,
    )
    if m:
        name = _clean_company(m.group(1))
        if _is_valid_company_name(name):
            return name

    # 2. Line that ENDS with a recognised company suffix (first 30 lines)
    for line in lines[:30]:
        m = _COMPANY_LINE_RE.match(line)
        if m and not re.match(r'^(Dear|To|Date|Ref|Subject|From)', line, re.IGNORECASE):
            name = _clean_company(m.group(1))
            if _is_valid_company_name(name):
                return name

    # 3. "Company Name:" / "Organisation:" explicit label
    m = re.search(r'(?:Company\s+Name|Organisation|Organization)\s*[:\-]\s*([^\n]{3,80})', text, re.IGNORECASE)
    if m:
        name = _clean_company(m.group(1))
        if _is_valid_company_name(name):
            return name

    # 4. Near CIN / GSTIN — company name usually on the line just before
    m = re.search(r'(?:CIN|GSTIN|GST\s*No)\s*[:\s#]+([A-Z0-9]{15,21})', text, re.IGNORECASE)
    if m:
        pos = m.start()
        before = text[max(0, pos - 200): pos]
        before_lines = [ln.strip() for ln in before.splitlines() if ln.strip()]
        for bl in reversed(before_lines[-3:]):
            if _is_valid_company_name(bl) and len(bl) > 5:
                return _clean_company(bl)

    # 5. First short Title-Case-only line in the header (no lowercase body words)
    for line in lines[:12]:
        if (6 < len(line) < 70
                and re.match(r'^[A-Za-z][A-Za-z0-9 &\.\'\-]+$', line)
                and not re.match(r'^(Dear|To|Date|Ref|Subject|From|Re)', line, re.IGNORECASE)
                and _is_valid_company_name(line)):
            return _clean_company(line)

    return None   # give up — let is_low_confidence() trigger Claude fallback

def _clean_company(name: str) -> str:
    return re.sub(r'\s+', ' ', name).strip().strip('.,;:')


# ── Shared name / role / clause helpers ──────────────────────────

# Phrases that mark a value as a sentence/clause rather than a name/title.
_CLAUSE_RE = re.compile(
    r'\b(aforesaid|hereby|gone through|terms (?:and|&) conditions|undersigned|'
    r'shall|will be|will report|herein|whereas|i have|i accept|required to|'
    r'reporting to|effective from|with effect|background check|discharging)\b',
    re.IGNORECASE,
)

# Role / designation phrases that are NOT a person's name. When one of these
# appears in the "name" slot of a sign-off, it IS the designation and there is
# simply no named signatory (legitimate and common for small companies / LOIs).
_ROLE_RE = re.compile(
    r'\b(authoris\w*|authoriz\w*|signatory|manager|director|executive|officer|'
    r'head|human\s+resour\w*|recruit\w*|talent|chro|hrd|partner|founder|'
    r'proprietor|president|ceo|cfo|coo|department|payroll|hr)\b',
    re.IGNORECASE,
)

# Field-label words that get glued onto a name when a table cell spills over.
_NAME_LABEL_RE = re.compile(
    r'\b(designation|department|position|role|title|date|email|e-mail|'
    r'mobile|phone|employee\s+id|emp\s+id|ctc|salary)\b',
    re.IGNORECASE,
)

# Heading / label / sign-off tokens that are never part of a person's name
# (e.g. "Offer Letter", "From", "To" mis-grabbed as hr_name / candidate_name).
_NON_NAME_TOKENS = {
    'offer', 'letter', 'appointment', 'annexure', 'from', 'to', 'date',
    'subject', 'ref', 'dear', 'sincerely', 'faithfully', 'regards', 'thanks',
    'employee', 'candidate', 'name', 'designation', 'company', 'signature',
    # job-title words — a person's name never contains these (they mark a
    # designation line mis-grabbed as a name, e.g. "Associate Data Processing")
    'associate', 'analyst', 'trainee', 'intern', 'engineer', 'consultant',
    'specialist', 'processing', 'operations', 'assistant', 'coordinator',
}


def _looks_like_role(s: Optional[str]) -> bool:
    return bool(s and _ROLE_RE.search(s))


def _clean_person_name(name: Optional[str]) -> Optional[str]:
    """Trim a captured name to a real person name: stop at a newline, drop any
    trailing field-label word (the "Manasa Kateer R\\nDesignation" bug), reject
    role phrases ("Authorized Signatory") and anything clause-like."""
    if not name:
        return None
    name = name.split("\n")[0].strip()
    name = _NAME_LABEL_RE.split(name)[0].strip().strip(".,:-")
    if not (2 < len(name) < 50):
        return None
    if _looks_like_role(name) or _CLAUSE_RE.search(name):
        return None
    if any(ch.isdigit() for ch in name):
        return None
    # A person's name never carries a company suffix — guards against a
    # "Company Name:" label feeding an org into candidate/HR name fields.
    if re.search(r'\b(pvt|ltd|limited|llp|inc|corp|technologies|solutions|'
                 r'services|enterprises|industries)\b', name, re.IGNORECASE):
        return None
    # Reject heading / label / sign-off words ("Offer Letter", "From", "To")
    # that get mis-grabbed as a name.
    if any(w.lower() in _NON_NAME_TOKENS for w in name.split()):
        return None
    return name


def _clean_designation(d: Optional[str]) -> Optional[str]:
    """Keep a clean job/HR designation; drop clause spill-over, trailing
    'Date:'/'Ref' text, or a company name that landed in the slot."""
    if not d:
        return None
    d = d.split("\n")[0].strip().strip(".,;:")
    d = re.split(r'\s{2,}|\bdate\b\s*:|\bref\b', d, flags=re.IGNORECASE)[0].strip().strip(".,;:")
    if not d or len(d) > 45 or len(d.split()) > 6:
        return None
    if _CLAUSE_RE.search(d):
        return None
    if re.search(r'\b(pvt|ltd|limited|llp|inc)\b', d, re.IGNORECASE):
        return None
    return d


# ── Candidate name ───────────────────────────────────────────────

def _extract_candidate_name(text: str, lines: list[str]) -> Optional[str]:
    # "Dear Mr./Ms./Mrs./Dr. [Name]" — allow single-letter initials (e.g. "NAVEEN A")
    # Use [ \t] instead of \s to avoid crossing newlines.
    m = re.search(
        r'\bDear[ \t]+(?:Mr\.?|Ms\.?|Mrs\.?|Dr\.?|Prof\.?)?[ \t]*([A-Z][a-zA-Z]+(?:[ \t]+[A-Z][a-zA-Z]*){0,3})',
        text,
    )
    if m:
        name = _clean_person_name(m.group(1))
        if name:
            return name

    # "Name: [Name]" or "Employee Name:" — [ \t] (not \s) so the capture cannot
    # cross a newline into the next table label (e.g. "...R\nDesignation").
    m = re.search(
        r'(?:Candidate|Employee|Applicant)?[ \t]*Name[ \t]*[:\-][ \t]*([A-Z][a-zA-Z]+(?:[ \t]+[A-Z][a-zA-Z]*){0,3})',
        text,
    )
    if m:
        name = _clean_person_name(m.group(1))
        if name:
            return name

    # "To," followed by a name on the next line (first 500 chars)
    m = re.search(r'\bTo,?\s*\n\s*([A-Z][a-zA-Z]+(?:[ \t]+[A-Z][a-zA-Z]+){1,3})', text[:500])
    if m:
        name = _clean_person_name(m.group(1))
        if name:
            return name

    return None


# ── Job title ────────────────────────────────────────────────────

_TITLE_REJECT_WORDS = {
    'result', 'breach', 'breaches', 'any', 'all', 'such', 'other',
    'above', 'below', 'therefore', 'whereas', 'herein', 'thereof',
    'indemnity', 'liability', 'obligation', 'pursuant', 'agreement',
    'clause', 'section', 'provision', 'shall', 'will', 'may', 'must',
    'upon', 'under', 'you', 'this', 'employs', 'contract', 'compensation',
    # qualifications / education are never a job title (guards "post-graduation" etc.)
    'graduation', 'post-graduation', 'postgraduation', 'graduate', 'graduates',
    'diploma', 'intermediate', 'matriculation', 'schooling', 'certificates',
    'testimonials', 'memos', 'degree',
}

def _is_valid_job_title(title: str) -> bool:
    if not title or len(title) < 3 or len(title) > 70:
        return False
    words = set(title.lower().split())
    if words & _TITLE_REJECT_WORDS:
        return False
    # A real title is a short phrase, not a clause: reject sentence punctuation
    # ("Termination Competition: Background. Tech Mahindra") and clause markers.
    if re.search(r'[:;.]', title) or _CLAUSE_RE.search(title):
        return False
    # Titles don't contain "of" or "by" after the first word (legal clauses do)
    tail_words = title.lower().split()[1:]
    if sum(1 for w in tail_words if w in {'of', 'by', 'and', 'or', 'any', 'all'}) >= 2:
        return False
    return True

def _extract_job_title(text: str, lines: list[str]) -> Optional[str]:
    labels = [
        # Quoted title after "position/role of" — e.g. position of "AI & Automation Associate"
        r'(?:position|role|post|designation)\s+of\s*["“]([^"”\n]{3,60})["”]',
        # Labelled field: "Position: X" / "Designation - X". A dash separator REQUIRES a
        # trailing space, so a hyphenated word ("post-graduation") is NOT split into a
        # "Post" label + "graduation" value.
        r'(?:Position|Designation|Role|Post|Job\s+Title|Job\s+Profile)\b\s*(?::\s*|[-–]\s+)([^\n,;]{3,70})',
        r'(?:you\s+(?:will\s+be|are)\s+(?:joining|appointed|hired|engaged)\s+as\s+(?:a\s+|an\s+)?)([A-Za-z][^\n,;\.]{3,50})',
        # "term on the position of Steward" / "offer you the position of Manager" (also unquoted)
        r'(?:position|role|post|designation)\s+of\s+["“]?([A-Za-z][^\n,;\.\(\)"”]{2,50}?)(?:["”]|\s*\n|\s*\(|\s*with\s)',
        r'(?:offer(?:ed)?\s+(?:you\s+)?(?:the\s+)?(?:position|role|post)\s+of\s+)["“]?([A-Za-z][^\n,;\.]{3,50})',
        # "appointed / joining as a Senior Engineer" — last, most ambiguous
        r'(?:appointed|joining|hired|engaged)\s+as\s+(?:a\s+|an\s+)?([A-Z][a-zA-Z\s]{3,50})(?:\s+in|\s+at|\s+for|\s+with|\n|,)',
    ]
    for pattern in labels:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            title = m.group(1).strip().strip('.,;:')
            # Truncate at clause-starting words
            title = re.split(r'\s+(?:subject\s+to|effective|commencing|w\.e\.f|with\s+effect)', title, flags=re.IGNORECASE)[0].strip().strip('.,;:')
            if _is_valid_job_title(title):
                return title
    return None


# ── Employment type ──────────────────────────────────────────────

_EMP_TYPE_PATTERNS = [
    (r'\binternship\b|\bstipend\b', 'Internship'),
    (r'\btrainee\b|\btraineeship\b|\bgraduate\s+trainee\b', 'Trainee'),
    (r'\bapprentice\b', 'Apprentice'),
    (r'\bfixed.?term\b|\bcontract(?:ual)?\b', 'Contract'),
    (r'\bpart.?time\b', 'Part-time'),
    (r'\bconsultant\b|\bconsultancy\b|\bfreelance\b', 'Consultant'),
    (r'\bpermanent\b|\bfull.?time\b|\bregular\s+employment\b', 'Full-time'),
]

def _extract_employment_type(text: str) -> Optional[str]:
    text_lc = text.lower()
    for pattern, label in _EMP_TYPE_PATTERNS:
        if re.search(pattern, text_lc):
            return label
    return None


# ── Work location ────────────────────────────────────────────────

def _extract_location(text: str, lines: list[str]) -> Optional[str]:
    m = re.search(
        r'(?:Work\s+Location|Place\s+of\s+(?:Work|Posting|Duty)|'
        r'Location|Based\s+[Aa]t|Office\s+Location|Duty\s+Station|'
        r'Posting\s+Location)\s*[:\-]\s*([^\n,;]{3,60})',
        text, re.IGNORECASE,
    )
    if m:
        loc = m.group(1).strip().strip('.,;')
        # Reject clause spill-over ("You are required to work at client's ...")
        if _CLAUSE_RE.search(loc) or len(loc.split()) > 7:
            return None
        # Reject unfilled placeholders ("____________", "--------")
        if len(re.sub(r'[^A-Za-z0-9]', '', loc)) < 3:
            return None
        return loc
    return None


# ── Address ──────────────────────────────────────────────────────

def _extract_address(text: str, lines: list[str]) -> Optional[str]:
    """Look for an Indian PIN code — address is usually the 2-3 lines above it."""
    pin_m = re.search(r'\b(\d{6})\b', text)
    if not pin_m:
        return None
    pos = pin_m.start()
    snippet = text[max(0, pos - 250): pos + 10]
    addr_lines = [ln.strip() for ln in snippet.splitlines() if ln.strip()]
    if addr_lines:
        return ', '.join(addr_lines[-3:])
    return None


# ── Salary ───────────────────────────────────────────────────────

def _parse_amount(raw: str) -> Optional[float]:
    """Convert an Indian number string to float (handles commas, ₹, /-).
    KEEPS the decimal point: the old code stripped '.', so "3.43" became 343 →
    ×12 → a bogus 4,116 CTC. Comma grouping ("1,04,294") is still removed."""
    try:
        clean = re.sub(r'[₹Rs,/\-\s]|Rs\.?', '', raw, flags=re.IGNORECASE)
        return float(clean) if clean else None
    except ValueError:
        return None

def _salary_near(text: str, keywords: list[str]) -> Optional[float]:
    """Find the first currency amount within 180 chars after any keyword.
    Honours Indian magnitude words: "3.43 Lakh" → 343000, "1.2 Crore" → 12000000.
    Without this, "INR 3.43 Lakh" parsed to 3.43 and was dropped or mis-scaled."""
    text_lc = text.lower()
    # Unit words are word-bounded so "cr" never matches inside "credited",
    # "lac" never matches inside "lack", etc.
    amount_re = re.compile(
        r'(?:₹|Rs\.?|INR)?\s*([\d,]+(?:\.\d{1,2})?)\s*'
        r'(?:(lakhs?|lacs?|lpa|crores?|cr)\b)?',
        re.IGNORECASE,
    )
    for kw in keywords:
        idx = text_lc.find(kw.lower())
        if idx == -1:
            continue
        snippet = text[idx: idx + 180]
        m = amount_re.search(snippet)
        if m:
            val = _parse_amount(m.group(1))
            if val is None:
                continue
            unit = (m.group(2) or "").lower()
            if unit.startswith(("lakh", "lac")) or unit == "lpa":
                val *= 100_000
            elif unit.startswith("cr"):
                val *= 10_000_000
            if val > 100:   # filter noise like "12%" or tiny values
                return val
    return None

def _extract_salary(text: str) -> SalaryBreakup:
    s = SalaryBreakup()

    # CTC / Annual
    ctc = _salary_near(text, [
        'ctc', 'cost to company', 'total fixed pay', 'tfp',
        'annual package', 'annual ctc', 'annual salary',
        'gross annual', 'total annual', 'total compensation',
    ])
    if ctc:
        # If value looks monthly (< 200000 for annual), multiply
        s.ctc_annual = ctc if ctc >= 50000 else ctc * 12

    # Gross monthly
    gross = _salary_near(text, [
        'gross monthly', 'gross salary', 'gross pay', 'total gross',
        'gross per month', 'monthly gross',
    ])
    if gross:
        s.gross_monthly = gross if gross < 500000 else gross / 12

    # Net / take-home
    net = _salary_near(text, [
        'net monthly', 'net pay', 'take home', 'take-home',
        'in hand', 'net salary',
    ])
    if net:
        s.net_monthly = net if net < 500000 else net / 12

    # Basic
    basic = _salary_near(text, ['basic salary', 'basic pay', 'basic'])
    if basic:
        s.basic = basic if basic < 500000 else basic / 12

    # HRA
    hra = _salary_near(text, ['hra', 'house rent allowance'])
    if hra:
        s.hra = hra if hra < 500000 else hra / 12

    # PF
    pf_emp = _salary_near(text, ["employee's pf", 'employee pf', 'pf employee', 'epf employee'])
    if pf_emp:
        s.pf_employee = pf_emp if pf_emp < 500000 else pf_emp / 12

    pf_er = _salary_near(text, ["employer's pf", 'employer pf', 'pf employer', 'epf employer'])
    if pf_er:
        s.pf_employer = pf_er if pf_er < 500000 else pf_er / 12

    # Special allowance
    sp = _salary_near(text, ['special allowance', 'other allowance', 'variable allowance'])
    if sp:
        s.special_allowance = sp if sp < 500000 else sp / 12

    # Gratuity
    gr = _salary_near(text, ['gratuity'])
    if gr:
        s.gratuity = gr if gr < 500000 else gr / 12

    # Joining bonus
    jb = _salary_near(text, ['joining bonus', 'sign-on bonus', 'sign on bonus', 'joining incentive'])
    if jb:
        s.joining_bonus = jb

    # Derive ctc_annual from gross_monthly when not stated explicitly
    if not s.ctc_annual and s.gross_monthly:
        s.ctc_annual = round(s.gross_monthly * 12, 2)

    # Sanity cap: real Indian offer letter CTCs don't exceed ~5 crore annually
    _MAX_ANNUAL = 50_000_000
    if s.ctc_annual and s.ctc_annual > _MAX_ANNUAL:
        s.ctc_annual = None
    if s.gross_monthly and s.gross_monthly > _MAX_ANNUAL / 12:
        s.gross_monthly = None

    return s


# ── HR details ───────────────────────────────────────────────────

_HR_LABELS = re.compile(
    r'\b(HR\s+(?:Manager|Head|Director|Executive|Officer)|'
    r'Human\s+Resources?|Head\s+of\s+HR|Chief\s+Human|'
    r'Talent\s+Acquisition|Recruitment\s+(?:Manager|Head)|'
    r'People\s+(?:Operations|Manager)|CHRO|HRD)\b',
    re.IGNORECASE,
)

_SIGNOFF_RE = re.compile(
    r'(?:Yours\s+(?:sincerely|faithfully|truly)|Regards|Thanks\s+and\s+regards|'
    r'With\s+regards|Best\s+regards|Warm\s+regards)',
    re.IGNORECASE,
)

_FOR_COMPANY_RE = re.compile(
    r'For[ \t]+[A-Z][A-Za-z0-9 &\.\-\']{2,60}'
    r'(?:Pvt\.?\s*Ltd\.?|Private\s+Limited|Limited|LLP|Inc\.?|Corp\.?|'
    r'Technologies|Solutions|Services|Foundation|Trust|Enterprises|Group)\.?'
    r'[ \t]*\n[ \t]*([A-Z][a-zA-Z]+(?:[ \t]+[A-Z][a-zA-Z]+){1,4})[ \t]*\n[ \t]*([A-Za-z][^\n]{3,70})',
    re.IGNORECASE,
)

def _extract_hr(text: str, lines: list[str]) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Return (hr_name, hr_designation, hr_email)."""

    # Find sign-off section
    signoff_idx = None
    for m in _SIGNOFF_RE.finditer(text):
        signoff_idx = m.start()
        break   # first sign-off

    if signoff_idx is None:
        signoff_idx = max(0, len(text) - 2500)

    tail = text[signoff_idx:]
    tail_lines = [ln.strip() for ln in tail.splitlines() if ln.strip()]

    # HR email
    hr_email = None
    for m in _EMAIL_RE.finditer(tail):
        hr_email = m.group(1).lower()
        break

    # ── Pattern 1: "For <Company>\n<Line1>\n<Line2>" sign-off ──
    # Search full text (not just tail) — this block can be 2000+ chars from end.
    # The line after "For <Company>" is OFTEN a role ("Authorized Signatory"),
    # not a person's name — route that to the designation, leave the name empty.
    hr_desig = None
    hr_name  = None
    fc = _FOR_COMPANY_RE.search(text)
    if fc:
        line1 = fc.group(1).strip().strip('.,')
        line2 = fc.group(2).strip().strip('.,')
        if _looks_like_role(line1):
            hr_desig = _clean_designation(line1)
            hr_name  = None
        else:
            hr_name  = _clean_person_name(line1)
            # the designation follows the name; validate it (drops clauses/dates)
            hr_desig = _clean_designation(line2) or (_clean_designation(line1) if hr_name is None else None)

    # ── Pattern 2: Explicit HR label line ("HR Manager", "CHRO", etc.) ──
    if hr_desig is None:
        for i, line in enumerate(tail_lines):
            if _HR_LABELS.search(line):
                hr_desig = _clean_designation(line)
                if i > 0 and hr_name is None:
                    hr_name = _clean_person_name(tail_lines[i - 1])
                break

    # ── Pattern 3: "Authorized Signatory" fallback ──────────────────
    if hr_desig is None:
        m = re.search(r'(Authoris\w*\s+Signatory|Authoriz\w*\s+Signatory)', tail, re.IGNORECASE)
        if m:
            hr_desig = m.group(1)

    # ── Name fallback: scan sign-off block for a Title-Case person name ──
    if hr_name is None:
        for line in tail_lines[1:10]:
            if re.match(r'^[A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3}$', line) and not _looks_like_role(line):
                hr_name = _clean_person_name(line)
                if hr_name:
                    break

    # Final safety net: a role phrase must never be returned as the name.
    if _looks_like_role(hr_name):
        hr_name = None

    return hr_name, hr_desig, hr_email

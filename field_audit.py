"""
field_audit.py — Quality check on EVERY extracted field (not just salary/dates).

Runs regex extraction over all text-based letters (free, no Claude) and applies
per-field sanity heuristics to surface where extraction is dirty: company name
that's a sentence, candidate name that's a heading, HR name that's actually a
designation, HR designation that's a whole clause, invalid CIN/phone, etc.

Prints, per field: how many letters produced a SUSPECT value, with examples.
Usage:  ./.venv/Scripts/python.exe field_audit.py
"""
import io
import re
from pathlib import Path
from collections import defaultdict

import pdfplumber

from app.models import RawPDFData
from app.field_extractor import extract_fields_from_text

ROOT = Path(__file__).parent
DIRS = [ROOT / "processed", ROOT / "inbox"]

# Words that signal a value is a sentence/clause, not a name/title/designation.
CLAUSE_WORDS = re.compile(
    r"\b(aforesaid|hereby|gone through|terms and conditions|accept|undersigned|"
    r"shall|will be|herein|whereas|agree|the company|i have|reporting|effective)\b",
    re.IGNORECASE,
)
DESIG_AS_NAME = re.compile(
    r"\b(authorized|authorised|signatory|manager|executive|officer|director|"
    r"head|department|human resources|hr|recruiter|partner|associate)\b",
    re.IGNORECASE,
)
HEADING_WORDS = re.compile(
    r"\b(offer|appointment|letter|annexure|designation|position|salary|ctc)\b",
    re.IGNORECASE,
)
EMP_TYPES = {"Full-time", "Part-time", "Contract", "Internship", "Trainee",
             "Apprentice", "Consultant"}


def text_only(b: bytes) -> str:
    try:
        with pdfplumber.open(io.BytesIO(b)) as pdf:
            return "\n".join((p.extract_text() or "") for p in pdf.pages)
    except Exception:
        return ""


def words(s):
    return len((s or "").split())


def field_problems(f) -> dict:
    """Return {field: reason} for each field whose value looks wrong."""
    p = {}

    cn = f.company_name
    if cn:
        if len(cn) > 60:                       p["company_name"] = f"too long: {cn[:50]!r}"
        elif CLAUSE_WORDS.search(cn):          p["company_name"] = f"looks like a clause: {cn[:50]!r}"
        elif "@" in cn or sum(c.isdigit() for c in cn) > 4:
            p["company_name"] = f"has email/digits: {cn[:50]!r}"

    can = f.candidate_name
    if can:
        if words(can) > 5 or len(can) > 40:    p["candidate_name"] = f"too long: {can[:50]!r}"
        elif DESIG_AS_NAME.search(can):        p["candidate_name"] = f"contains a role word: {can[:50]!r}"
        elif HEADING_WORDS.search(can):        p["candidate_name"] = f"looks like a heading: {can[:50]!r}"
        elif any(ch.isdigit() for ch in can):  p["candidate_name"] = f"has digits: {can[:50]!r}"

    jt = f.job_title
    EDU_WORDS = {"graduation", "post-graduation", "postgraduation", "graduate",
                 "graduates", "diploma", "intermediate", "matriculation", "degree",
                 "certificates", "testimonials", "memos", "schooling"}
    if jt:
        if len(jt) > 60:                       p["job_title"] = f"too long: {jt[:50]!r}"
        elif jt.strip().lower() == (cn or "").strip().lower(): p["job_title"] = "equals company name"
        elif jt.strip().lower() == (can or "").strip().lower(): p["job_title"] = "equals candidate name"
        elif any(w.lower().strip('.,/') in EDU_WORDS for w in jt.split()):
            p["job_title"] = f"education/qualification word, not a title: {jt[:50]!r}"
        elif CLAUSE_WORDS.search(jt):          p["job_title"] = f"looks like a clause: {jt[:50]!r}"

    hn = f.hr_name
    NON_NAME = {"offer", "letter", "from", "to", "date", "subject", "ref",
                "dear", "annexure", "company", "name"}
    if hn:
        if DESIG_AS_NAME.search(hn):           p["hr_name"] = f"is a designation, not a name: {hn[:50]!r}"
        elif CLAUSE_WORDS.search(hn) or len(hn) > 40: p["hr_name"] = f"looks like a clause: {hn[:50]!r}"
        elif any(ch.isdigit() for ch in hn):   p["hr_name"] = f"has digits: {hn[:50]!r}"
        elif any(w.lower() in NON_NAME for w in hn.split()): p["hr_name"] = f"heading/label word: {hn[:50]!r}"
        elif can and can.lower() in hn.lower(): p["hr_name"] = f"contains candidate name: {hn[:50]!r}"

    hd = f.hr_designation
    if hd:
        if len(hd) > 46 or words(hd) > 7:      p["hr_designation"] = f"too long / a clause: {hd[:50]!r}"
        elif CLAUSE_WORDS.search(hd):          p["hr_designation"] = f"looks like a clause: {hd[:50]!r}"

    em = f.company_email
    if em and not re.match(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", em, re.I):
        p["company_email"] = f"invalid format: {em!r}"

    dm = f.company_domain
    if dm and not re.match(r"^[a-z0-9][a-z0-9.\-]*\.[a-z]{2,}$", dm, re.I):
        p["company_domain"] = f"invalid format: {dm!r}"

    ph = f.company_phone
    if ph:
        digits = re.sub(r"\D", "", ph)
        if not (10 <= len(digits) <= 13):      p["company_phone"] = f"implausible: {ph!r}"

    cin = f.company_cin
    if cin:
        ok = (re.match(r"^[A-Z]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6}$", cin)            # CIN (21)
              or re.match(r"^\d{2}[A-Z]{5}\d{4}[A-Z][A-Z\d]Z[A-Z\d]$", cin, re.I)  # GST (15)
              or cin.upper().startswith("UDYAM"))
        if not ok:                             p["company_cin"] = f"invalid CIN/GST/UDYAM: {cin!r}"

    et = f.employment_type
    if et and et not in EMP_TYPES:             p["employment_type"] = f"unknown type: {et!r}"

    wl = f.work_location
    if wl:
        if len(wl) > 50 or CLAUSE_WORDS.search(wl):
            p["work_location"] = f"too long / a clause: {wl[:50]!r}"
        elif len(re.sub(r'[^A-Za-z0-9]', '', wl)) < 3:
            p["work_location"] = f"placeholder blank: {wl[:50]!r}"

    return p


def main():
    pdfs = []
    for d in DIRS:
        if d.exists():
            pdfs += sorted(d.glob("*.pdf"))

    counts = defaultdict(int)
    missing = defaultdict(int)
    examples = defaultdict(list)
    audited = 0
    FIELDS = ["company_name", "candidate_name", "job_title", "hr_name",
              "hr_designation", "company_email", "company_domain",
              "company_phone", "company_cin", "employment_type", "work_location"]

    for p in pdfs:
        text = text_only(p.read_bytes())
        if not text.strip():
            continue
        audited += 1
        f = extract_fields_from_text(RawPDFData(full_text=text))
        for fld in FIELDS:
            if not getattr(f, fld):
                missing[fld] += 1
        for fld, reason in field_problems(f).items():
            counts[fld] += 1
            if len(examples[fld]) < 4:
                examples[fld].append(f"{reason}   [{p.name[:30]}]")

    print(f"Audited {audited} text-based letters\n")
    print(f"{'field':18}{'SUSPECT':>9}{'missing':>9}   examples")
    print("-" * 72)
    for fld in FIELDS:
        print(f"{fld:18}{counts[fld]:>9}{missing[fld]:>9}")
        for ex in examples[fld]:
            print(f"      - {ex}")


if __name__ == "__main__":
    main()

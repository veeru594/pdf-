"""
final_check.py — one consolidated, free (no-Claude) pass over the corpus to
confirm the extraction/parsing layer is clean before moving on to the prompt.

Per letter it checks: (1) no exception, (2) no implausible value
(_plausibility_problems), (3) no dirty field (field_audit heuristics),
(4) cross-field consistency (job != company != candidate, hr_name not a role).
Then it dumps the FULL extracted field set for the previously-broken letters.
"""
from pathlib import Path

from app.models import RawPDFData
from app.field_extractor import extract_fields_from_text, _plausibility_problems
from field_audit import field_problems, text_only

ROOT = Path(__file__).parent
DIRS = [ROOT / "processed", ROOT / "inbox"]

SPOTLIGHT = [
    "Job Offer - S Suresh_Shared by aspirant.pdf",  # original 4116 / 2017 bug
    "Abhishek Axis Max Life.pdf",                    # clause-as-company + 100x salary
    "Offer Letter  ARPITHA RESOURCE PRO.pdf",        # heading-as-company + gmail
    "Anik Offer Letter.pdf",                         # wrong old offer date
    "Akram Pasha Offer Letter.pdf",                  # hr_name = Authorized Signatory
    "Offer Letter Arun S.pdf",                       # candidate "\nDesignation"
]


def get_fields(p: Path):
    text = text_only(p.read_bytes())
    if not text.strip():
        return None, "no-text"
    return extract_fields_from_text(RawPDFData(full_text=text)), None


def main():
    pdfs = []
    for d in DIRS:
        if d.exists():
            pdfs += sorted(d.glob("*.pdf"))

    crashed, implausible, dirty, audited = [], [], [], 0
    for p in pdfs:
        try:
            f, skip = get_fields(p)
        except Exception as e:
            crashed.append((p.name, f"{type(e).__name__}: {e}"))
            continue
        if skip:
            continue
        audited += 1
        if _plausibility_problems(f):
            implausible.append(p.name)
        if field_problems(f):
            dirty.append((p.name, field_problems(f)))

    print("=" * 72)
    print(f"FINAL CHECK — {audited} text-based letters")
    print("=" * 72)
    print(f"  crashed during extraction : {len(crashed)}")
    print(f"  implausible (route->Claude): {len(implausible)}")
    print(f"  dirty field (suspect)      : {len(dirty)}")
    for name, e in crashed[:10]:
        print(f"    CRASH  {name[:45]}  {e}")
    for name, probs in dirty[:10]:
        print(f"    DIRTY  {name[:45]}  {probs}")

    print("\n" + "=" * 72)
    print("SPOTLIGHT — full field set for previously-broken letters")
    print("=" * 72)
    for name in SPOTLIGHT:
        p = next((d / name for d in DIRS + [ROOT] if (d / name).exists()), None)
        if not p:
            print(f"\n[{name}]  (not found)")
            continue
        f, _ = get_fields(p)
        s = f.salary
        print(f"\n[{name}]")
        print(f"   company   : {f.company_name!r}")
        print(f"   candidate : {f.candidate_name!r}")
        print(f"   job_title : {f.job_title!r}")
        print(f"   emp_type  : {f.employment_type!r}   location: {f.work_location!r}")
        print(f"   hr_name   : {f.hr_name!r}   hr_desig: {f.hr_designation!r}")
        print(f"   domain    : {f.company_domain!r}   email: {f.company_email!r}   cin: {f.company_cin!r}")
        print(f"   offer/join: {f.offer_date} -> {f.joining_date}")
        print(f"   ctc={s.ctc_annual} basic={s.basic} hra={s.hra} gross_m={s.gross_monthly}")
        print(f"   plausibility: {_plausibility_problems(f) or 'CLEAN'}")


if __name__ == "__main__":
    main()

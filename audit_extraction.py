"""
audit_extraction.py — Corpus-wide sanity check on the REGEX extraction layer.

Runs app.field_extractor.extract_fields_from_text over every PDF in processed/
(and inbox/) using only pdfplumber text — NO Claude calls, so it is free and
fast. For each letter it applies plausibility checks and reports how many have
extractions that would route the letter down a wrong verdict path.

Usage:  ./.venv/Scripts/python.exe audit_extraction.py
"""
import io
from pathlib import Path

import pdfplumber

from app.models import RawPDFData
from app.field_extractor import (
    extract_fields_from_text, is_low_confidence, _plausibility_problems as check,
)

ROOT = Path(__file__).parent
DIRS = [ROOT / "processed", ROOT / "inbox"]


def text_only(pdf_bytes: bytes) -> str:
    """Extract the text layer only — no OCR, no Claude."""
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            return "\n".join((p.extract_text() or "") for p in pdf.pages)
    except Exception:
        return ""


def main():
    pdfs = []
    for d in DIRS:
        if d.exists():
            pdfs += sorted(d.glob("*.pdf"))

    total = len(pdfs)
    no_text = 0
    low_conf = 0          # would trigger Claude fallback today
    flagged = []          # (name, problems) where regex output is implausible
    silent_bad = []       # implausible BUT passes is_low_confidence -> flows downstream

    print(f"Auditing {total} letter(s) from {[str(d.name) for d in DIRS]}\n")

    for i, p in enumerate(pdfs, 1):
        text = text_only(p.read_bytes())
        if not text.strip():
            no_text += 1
            continue
        raw = RawPDFData(full_text=text)
        fields = extract_fields_from_text(raw)
        problems = check(fields)
        lc = is_low_confidence(fields)
        if lc:
            low_conf += 1
        if problems:
            flagged.append((p.name, problems, lc))
            if not lc:
                # Implausible values that STILL pass the confidence gate are the
                # dangerous ones: they flow into scoring and skew the verdict.
                silent_bad.append((p.name, problems))
        if i % 50 == 0:
            print(f"  ...{i}/{total}")

    audited = total - no_text
    print("\n" + "=" * 72)
    print(f"AUDIT SUMMARY")
    print("=" * 72)
    print(f"  PDFs found            : {total}")
    print(f"  No text layer (skip)  : {no_text}")
    print(f"  Audited (text-based)  : {audited}")
    print(f"  Would hit Claude (lc) : {low_conf}")
    print(f"  Implausible extraction: {len(flagged)}")
    print(f"  >>> SILENT-BAD (implausible AND passes confidence gate): {len(silent_bad)}")
    if audited:
        print(f"  Silent-bad rate       : {len(silent_bad)/audited*100:.1f}% of text-based letters")

    if silent_bad:
        print("\n" + "-" * 72)
        print("SILENT-BAD LETTERS (wrong values flowing into the verdict):")
        print("-" * 72)
        for name, problems in silent_bad[:60]:
            print(f"  • {name[:55]}")
            for pr in problems:
                print(f"        - {pr}")

    return silent_bad


if __name__ == "__main__":
    main()

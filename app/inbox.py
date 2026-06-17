"""
Inbox management â€” safe file state transitions for the offer letter pipeline.

States:
  unprocessed  â†’ .pdf in inbox/
  processing   â†’ .pdf.processing in inbox/  (atomically claimed by one worker)
  processed    â†’ .pdf in processed/

Startup safety: any stale .processing files (left by a crash) are reverted to
.pdf automatically so they can be retried.
"""
import re
import os
import logging
from pathlib import Path
from typing import Optional
from datetime import datetime

logger = logging.getLogger(__name__)

PROJECT_ROOT  = Path(__file__).parent.parent
INBOX_DIR     = PROJECT_ROOT / "inbox"
PROCESSED_DIR = PROJECT_ROOT / "processed"
REPORTS_DIR   = PROJECT_ROOT / "reports"

# Folder name on disk for each verdict value
VERDICT_FOLDER = {
    "LEGITIMATE":    "LEGITIMATE",
    "MANUAL REVIEW": "MANUAL_REVIEW",
    "SUSPICIOUS":    "SUSPICIOUS",
}


def ensure_dirs() -> None:
    """Create all required directories if they do not yet exist."""
    INBOX_DIR.mkdir(exist_ok=True)
    PROCESSED_DIR.mkdir(exist_ok=True)
    for folder in VERDICT_FOLDER.values():
        (REPORTS_DIR / folder).mkdir(parents=True, exist_ok=True)


def startup_cleanup() -> None:
    """
    On server start, revert stale .pdf.processing files back to .pdf.
    These are left behind when the server crashes mid-analysis.
    """
    ensure_dirs()
    recovered = 0
    for stale in INBOX_DIR.glob("*.pdf.processing"):
        original = stale.with_suffix("")   # strip .processing â†’ .pdf
        try:
            stale.rename(original)
            recovered += 1
            logger.warning(f"[INBOX] Recovered stale: {stale.name} â†’ {original.name}")
        except Exception as e:
            logger.error(f"[INBOX] Recovery failed for {stale.name}: {e}")
    if recovered:
        logger.info(f"[INBOX] Startup cleanup: {recovered} file(s) recovered")


def list_inbox() -> list[dict]:
    """
    Return all PDFs across inbox/ (unprocessed + in-progress) and processed/
    with their current status and, for processed files, a link to the report.
    Sorted: unprocessed first (alphabetical), then processing, then processed.
    """
    ensure_dirs()
    unprocessed, processing, processed = [], [], []

    for f in sorted(INBOX_DIR.glob("*.pdf")):
        unprocessed.append({
            "name":     f.name,
            "status":   "unprocessed",
            "size_kb":  f.stat().st_size // 1024,
        })

    for f in sorted(INBOX_DIR.glob("*.pdf.processing")):
        processing.append({
            "name":     f.stem,          # strip .processing to show original name
            "status":   "processing",
            "size_kb":  f.stat().st_size // 1024,
        })

    for f in sorted(PROCESSED_DIR.glob("*.pdf"), key=lambda x: x.stat().st_mtime, reverse=True):
        report = _find_report(f.stem)
        processed_dt = report.get("processed_dt") or datetime.fromtimestamp(f.stat().st_mtime)
        mtime  = processed_dt.strftime("%d %b %Y, %H:%M")
        processed.append({
            "name":           f.name,
            "status":         "processed",
            "size_kb":        f.stat().st_size // 1024,
            "report_verdict": report.get("verdict"),
            "report_score":   report.get("score"),
            "report_url":     report.get("url"),
            "processed_at":   mtime,
            "processed_at_iso": processed_dt.isoformat(timespec="seconds"),
        })

    return unprocessed + processing + processed


def _find_report(pdf_stem: str) -> dict:
    """
    Find the saved HTML report that was generated from this PDF.
    The report filename is slugified (spaces/dots â†’ underscores), so we must
    apply the same transformation before globbing â€” otherwise 'A.Saikirishna
    offer letter' never matches 'A_Saikirishna_offer_letter_*.html'.
    Score is embedded in the filename as '_s{score}_' and parsed back out.
    Returns url relative to the /reports static mount.
    """
    slug = _slug_stem(pdf_stem)[:30]   # matches _report_filename() truncation
    for verdict, folder in VERDICT_FOLDER.items():
        report_dir = REPORTS_DIR / folder
        for r in report_dir.glob(f"{slug}*.html"):
            rel   = r.relative_to(REPORTS_DIR)
            score = _parse_score_from_report(r)
            report_dt = datetime.fromtimestamp(r.stat().st_mtime)
            return {
                "verdict": verdict,
                "score":   score,
                "url":     f"/reports/{rel.as_posix()}",
                "processed_dt": report_dt,
            }
    return {}


def _parse_score_from_report(report_path: Path) -> int | None:
    """
    Extract the overall score from a saved HTML report.
    Works for all reports â€” old filenames without _s{score}_ and new ones alike.
    Reads only the first 2 KB (score is always near the top of the file).
    """
    try:
        head = report_path.read_text(encoding="utf-8", errors="ignore")[:12000]
        m = re.search(r'class="score-num-big"[^>]*>(\d+)<', head)
        return int(m.group(1)) if m else None
    except Exception:
        return None


def _slug_stem(text: str) -> str:
    """Replace non-alphanumeric characters with underscores (mirrors report_writer._slug)."""
    return re.sub(r"[^a-zA-Z0-9]+", "_", str(text)).strip("_")


def claim_pdf(filename: str) -> Optional[Path]:
    """
    Atomically claim a PDF for processing by renaming it to .pdf.processing.
    Uses os.rename (atomic on Linux/Windows NTFS) â€” safe for concurrent callers.
    Returns the .processing path on success, None if already claimed or missing.
    """
    name = _safe_name(filename)
    src  = INBOX_DIR / name
    dst  = INBOX_DIR / (name + ".processing")
    try:
        src.rename(dst)
        logger.info(f"[INBOX] Claimed: {name}")
        return dst
    except FileNotFoundError:
        logger.warning(f"[INBOX] Claim failed â€” not found: {name}")
        return None
    except Exception as e:
        logger.error(f"[INBOX] Claim error for {name}: {e}")
        return None


def complete_pdf(filename: str) -> None:
    """Move .processing â†’ processed/ after a successful analysis."""
    name = _safe_name(filename)
    src  = INBOX_DIR / (name + ".processing")
    dst  = PROCESSED_DIR / name

    # Avoid collision with an existing file in processed/
    if dst.exists():
        stem, suffix = Path(name).stem, Path(name).suffix
        i = 1
        while dst.exists():
            dst = PROCESSED_DIR / f"{stem}_{i}{suffix}"
            i += 1

    try:
        src.rename(dst)
        now_ts = datetime.now().timestamp()
        os.utime(dst, (now_ts, now_ts))
        logger.info(f"[INBOX] Completed: {name} → processed/")
    except Exception as e:
        logger.error(f"[INBOX] Complete failed for {name}: {e}")


def revert_pdf(filename: str) -> None:
    """On analysis failure rename .processing back to .pdf so it can be retried."""
    name = _safe_name(filename)
    src  = INBOX_DIR / (name + ".processing")
    dst  = INBOX_DIR / name
    try:
        src.rename(dst)
        logger.info(f"[INBOX] Reverted: {name} back to inbox")
    except Exception as e:
        logger.error(f"[INBOX] Revert failed for {name}: {e}")


def _safe_name(filename: str) -> str:
    """Strip to basename only â€” prevents path traversal attacks."""
    return Path(filename).name



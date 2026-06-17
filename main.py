import time
import json
import asyncio
import traceback
import logging
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.config import settings
from app.models import AnalysisResult
from app.pdf_reader import read_pdf
from app.ai_client import (
    extract_fields,
    analyze_letter,
    analyze_images,
)
from app.checker import check_domain, check_company_online
from app.rules import (
    compute_date_logic,
    compute_salary_math,
    compute_completeness,
    compute_final_score,
)
from app.inbox import (
    list_inbox,
    claim_pdf,
    complete_pdf,
    revert_pdf,
    startup_cleanup,
    INBOX_DIR,
    REPORTS_DIR,
)
from app.report_writer import save_report, save_adhoc_report, SAVED_REPORTS_DIR
from app.batch_processor import (
    start_batch,
    stop_batch,
    get_status as get_batch_status,
)

logger = logging.getLogger(__name__)

# ── APScheduler ──────────────────────────────────────────────────
_scheduler = AsyncIOScheduler()
_SCHEDULE_FILE = Path(__file__).parent / "schedule_config.json"

def _load_schedule_config() -> dict:
    if _SCHEDULE_FILE.exists():
        try:
            return json.loads(_SCHEDULE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"enabled": False, "interval_minutes": 30, "delay_seconds": 12}

def _save_schedule_config(cfg: dict) -> None:
    _SCHEDULE_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

def _scheduled_batch_trigger():
    """Called by APScheduler — kicks off batch if not already running."""
    state = get_batch_status()
    if state["status"] not in ("running", "stopping"):
        cfg = _load_schedule_config()
        start_batch(delay_seconds=cfg.get("delay_seconds", 12))
        logger.info("[SCHEDULER] Auto-triggered batch run")

app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="Full offer letter authenticity pipeline — extraction, analysis, scoring",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)

# ── Static mounts ───────────────────────────────────────────────
static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# Serve saved HTML reports (verdict subfolders: LEGITIMATE, MANUAL_REVIEW, SUSPICIOUS)
REPORTS_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/reports", StaticFiles(directory=str(REPORTS_DIR)), name="reports")

# Serve the persistent ad-hoc /analyze archive (separate from the disposable reports/)
SAVED_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/saved-reports", StaticFiles(directory=str(SAVED_REPORTS_DIR)), name="saved-reports")


# ── Startup ──────────────────────────────────────────────────────
@app.on_event("startup")
async def on_startup():
    """Recover stale .processing files and restore cron schedule if enabled."""
    startup_cleanup()
    # Restore scheduled job if it was enabled before restart
    cfg = _load_schedule_config()
    if cfg.get("enabled"):
        _apply_schedule(cfg["interval_minutes"])
    _scheduler.start()
    logger.info("[STARTUP] APScheduler started")


@app.on_event("shutdown")
async def on_shutdown():
    if _scheduler.running:
        _scheduler.shutdown(wait=False)


def _apply_schedule(interval_minutes: int) -> None:
    """Add or replace the scheduled batch job."""
    if _scheduler.get_job("auto_batch"):
        _scheduler.remove_job("auto_batch")
    _scheduler.add_job(
        _scheduled_batch_trigger,
        trigger=IntervalTrigger(minutes=interval_minutes),
        id="auto_batch",
        replace_existing=True,
    )
    logger.info(f"[SCHEDULER] Job set — every {interval_minutes} min")


# ── UI ──────────────────────────────────────────────────────────
@app.get("/")
def serve_ui():
    static_file = Path(__file__).parent / "static" / "index.html"
    if static_file.exists():
        return FileResponse(str(static_file))
    return {"message": "UI not found"}


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(status_code=204)


@app.get("/health")
def health():
    return {
        "status":  "ok",
        "service": settings.app_name,
        "version": settings.app_version,
    }


# ── INBOX endpoints ──────────────────────────────────────────────

@app.get("/inbox")
def get_inbox():
    """List all PDFs in the inbox with their current status."""
    return {
        "files":      list_inbox(),
        "inbox_path": str(INBOX_DIR),
    }


class InboxRequest(BaseModel):
    filename: str


@app.post("/analyze-from-inbox", response_model=AnalysisResult)
async def analyze_from_inbox(req: InboxRequest):
    """
    Analyze a PDF that is already in the inbox/ folder.
    - Atomically claims the file (renames to .processing) — concurrent
      requests for the same file will receive a 409.
    - On success: moves PDF to processed/, saves HTML report.
    - On failure: reverts PDF to inbox/ so it can be retried.
    """
    filename = req.filename

    # Claim the file — atomic, safe for concurrent users
    processing_path = claim_pdf(filename)
    if processing_path is None:
        raise HTTPException(
            409,
            f"'{filename}' is not available — it may already be processing or has been processed."
        )

    start_time = time.time()
    try:
        file_bytes   = processing_path.read_bytes()
        file_size_kb = len(file_bytes) // 1024

        result = await _run_pipeline(
            file_bytes    = file_bytes,
            file_name     = filename,
            file_size_kb  = file_size_kb,
            start_time    = start_time,
        )

        # Move PDF to processed/ and save the report
        complete_pdf(filename)
        report_path = save_report(result, filename)
        logger.info(f"[INBOX] Report saved: {report_path}")

        return result

    except HTTPException:
        revert_pdf(filename)
        raise
    except Exception as e:
        revert_pdf(filename)
        traceback.print_exc()
        raise HTTPException(500, f"Pipeline error: {str(e)}")


# ── BATCH endpoints ──────────────────────────────────────────────

class BatchStartRequest(BaseModel):
    delay_seconds: int  = 12   # seconds to wait between each PDF (sequential path only)
    max_per_batch: int  = 0    # 0 = process all; any other value = cap per run
    use_batch_api: bool = False  # True = Anthropic Batches API (50% cost, async ~1h)

class ScheduleRequest(BaseModel):
    enabled: bool
    interval_minutes: int = 30
    delay_seconds: int = 12


@app.post("/batch/start")
async def batch_start(req: BatchStartRequest):
    """Start batch processing of all unprocessed inbox PDFs.
    use_batch_api=True routes scoring through Anthropic's Batches API (50% cost, async)."""
    result = start_batch(
        delay_seconds=req.delay_seconds,
        max_per_batch=req.max_per_batch,
        use_batch_api=req.use_batch_api,
    )
    return result


@app.post("/batch/stop")
async def batch_stop():
    """Gracefully stop batch — finishes current PDF then halts."""
    return stop_batch()


@app.get("/batch/status")
async def batch_status():
    """Return current batch processing state and progress."""
    return get_batch_status()


@app.post("/batch/schedule")
async def batch_schedule(req: ScheduleRequest):
    """Enable or update the automatic cron schedule for batch processing."""
    cfg = {
        "enabled":          req.enabled,
        "interval_minutes": req.interval_minutes,
        "delay_seconds":    req.delay_seconds,
    }
    _save_schedule_config(cfg)

    if req.enabled:
        _apply_schedule(req.interval_minutes)
        next_run = _scheduler.get_job("auto_batch")
        next_run_str = str(next_run.next_run_time) if next_run else "unknown"
        return {"ok": True, "message": f"Schedule enabled — every {req.interval_minutes} min", "next_run": next_run_str}
    else:
        if _scheduler.get_job("auto_batch"):
            _scheduler.remove_job("auto_batch")
        return {"ok": True, "message": "Schedule disabled"}


@app.get("/batch/schedule")
async def batch_schedule_get():
    """Return current schedule configuration."""
    cfg = _load_schedule_config()
    job = _scheduler.get_job("auto_batch")
    cfg["next_run"] = str(job.next_run_time) if job else None
    cfg["scheduler_running"] = _scheduler.running
    return cfg


# ── DIRECT UPLOAD endpoint ───────────────────────────────────────

@app.post("/analyze", response_model=AnalysisResult)
async def analyze(file: UploadFile = File(...)):
    """
    Ad-hoc analysis via direct file upload.
    Result is shown on screen AND archived as a standalone HTML report under
    saved_reports/ (browsable at /saved-reports/) so it survives a page refresh.
    This archive is independent of the inbox→processed→reports flow.
    """
    if file.content_type not in ["application/pdf", "application/octet-stream"]:
        raise HTTPException(400, f"Expected PDF, got {file.content_type}")

    file_bytes = await file.read()

    if len(file_bytes) == 0:
        raise HTTPException(400, "Empty file received")

    max_bytes = settings.max_file_size_mb * 1024 * 1024
    if len(file_bytes) > max_bytes:
        raise HTTPException(400, f"File too large — max {settings.max_file_size_mb} MB")

    start_time = time.time()
    try:
        result = await _run_pipeline(
            file_bytes   = file_bytes,
            file_name    = file.filename or "unknown.pdf",
            file_size_kb = len(file_bytes) // 1024,
            start_time   = start_time,
        )

        # Archive a standalone report so the result survives a refresh.
        # Saving is auxiliary — never fail the analysis if the disk write fails.
        try:
            saved = save_adhoc_report(result, file.filename or "unknown.pdf")
            logger.info(f"[ANALYZE] Report archived: {saved}")
        except Exception:
            logger.exception("[ANALYZE] Could not archive report (analysis still returned)")

        return result
    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(500, f"Pipeline error: {str(e)}")


# ── Shared pipeline ──────────────────────────────────────────────

async def _run_pipeline(
    file_bytes: bytes,
    file_name: str,
    file_size_kb: int,
    start_time: float,
) -> AnalysisResult:
    """
    Full analysis pipeline — shared by both /analyze and /analyze-from-inbox.

    Steps:
      1. Read PDF  — text, images, metadata, page renders
      2. Claude 1  — field extraction
      3. Sync      — image metadata (pure Python, no I/O)
      4. Parallel  — DNS, company online check, rule-based checks
      5. Claude 2  — full 9-pillar scoring with rendered page images
      6. Scoring   — compute final score, verdict, hard gate
    """
    # ── STEP 1: PDF reading ──────────────────────────────────────
    raw = read_pdf(file_bytes)

    if not raw.full_text.strip():
        raise HTTPException(422, "Could not extract text from PDF — may be a scanned image")

    # ── STEP 2: Field extraction ─────────────────────────────────
    fields = await asyncio.to_thread(extract_fields, raw)

    # ── STEP 3: Image metadata (pure Python, no I/O) ─────────────
    images = analyze_images(raw)

    # ── STEP 4: Parallel checks ──────────────────────────────────
    (
        dns,
        company_online,
        date_logic,
        salary_math,
        completeness,
    ) = await asyncio.gather(
        asyncio.to_thread(check_domain,         fields.company_domain or ""),
        asyncio.to_thread(check_company_online, fields.company_name, fields.company_domain),
        asyncio.to_thread(compute_date_logic,   fields),
        asyncio.to_thread(compute_salary_math,  fields),
        asyncio.to_thread(compute_completeness, fields, raw),
    )

    # ── STEP 5: AI deep analysis ─────────────────────────────────
    analysis = await asyncio.to_thread(
        analyze_letter,
        fields         = fields,
        raw            = raw,
        company_online = company_online,
        dns            = dns,
        date_logic     = date_logic,
        salary_math    = salary_math,
        completeness   = completeness,
    )

    # ── STEP 5a: Company-name rescue from visual analysis ───────
    # If text extraction missed the company name (e.g. lowercase-styled brand,
    # or the name sits inside a letterhead image), the multimodal pass may have
    # read it. Populate it now — zero extra API cost (same analyze_letter call).
    # Set before the domain rescue below so its online re-check uses the name.
    if not fields.company_name and analysis.company_name_found:
        rescued_name = analysis.company_name_found.strip()
        logger.info(f"[NAME RESCUE] Visual analysis found company name: {rescued_name!r}")
        fields.company_name = rescued_name

    # ── STEP 5b: Domain rescue from visual analysis ─────────────
    # If regex + Claude-text both failed to find a domain (contact info is
    # inside an embedded image), Claude's multimodal pass may have read the
    # domain from the rendered letterhead.  Re-run DNS + online check now so
    # the final score reflects the actual company domain.
    if not fields.company_domain and analysis.company_domain_found:
        rescued = analysis.company_domain_found.strip().lower()
        logger.info(f"[DOMAIN RESCUE] Visual analysis found domain: {rescued!r} — re-running DNS")
        fields.company_domain = rescued
        dns, company_online = await asyncio.gather(
            asyncio.to_thread(check_domain,         rescued),
            asyncio.to_thread(check_company_online, fields.company_name, rescued),
        )

    # ── STEP 6: Final scoring ────────────────────────────────────
    elapsed_ms = int((time.time() - start_time) * 1000)

    return compute_final_score(
        fields            = fields,
        raw               = raw,
        company_online    = company_online,
        dns               = dns,
        analysis          = analysis,
        images            = images,
        date_logic        = date_logic,
        salary_math       = salary_math,
        completeness      = completeness,
        file_name         = file_name,
        file_size_kb      = file_size_kb,
        processing_time_ms= elapsed_ms,
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app,host='0.0.0.0', port=8003, log_level="info")
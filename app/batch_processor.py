"""
batch_processor.py — Sequential batch processing of all inbox PDFs.

Design:
- One PDF at a time (sequential, no concurrency)
- Configurable delay between PDFs to respect Claude API rate limits
- Stop flag: finishes current PDF then halts cleanly — never kills mid-analysis
- Progress persisted to batch_status.json so a server restart doesn't lose state
- Failed PDFs are reverted to inbox automatically; batch continues to next
"""
import asyncio
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

PROJECT_ROOT   = Path(__file__).parent.parent
STATUS_FILE    = PROJECT_ROOT / "batch_status.json"

# ── In-memory state ──────────────────────────────────────────────
_state: dict = {
    "status":        "idle",      # idle | running | stopping | completed
    "total":         0,
    "processed":     0,
    "failed":        0,
    "current_file":  None,
    "started_at":    None,
    "finished_at":   None,
    "delay_seconds": 12,
    "failed_files":  [],
    "eta_seconds":   None,
}

_stop_requested = False
_batch_task: Optional[asyncio.Task] = None


# ── Public API ───────────────────────────────────────────────────

def get_status() -> dict:
    """Return current batch state (merged with persisted state)."""
    _load_state()
    return dict(_state)


def start_batch(delay_seconds: int = 12, max_per_batch: int = 0, use_batch_api: bool = False) -> dict:
    """
    Kick off the batch processing loop as a background asyncio task.
    max_per_batch=0 means process all unprocessed PDFs.
    use_batch_api=True routes the scoring call through Anthropic's Batches API
    (50% cost, async ~1h) instead of the sequential per-PDF synchronous path.
    Safe to call even if already running — returns current status.
    """
    global _batch_task, _stop_requested

    _load_state()

    if _state["status"] == "running":
        return {"ok": False, "message": "Batch already running", "state": dict(_state)}

    _stop_requested = False
    _state["delay_seconds"]  = delay_seconds
    _state["max_per_batch"]  = max_per_batch

    loop = asyncio.get_event_loop()
    _batch_task = loop.create_task(_run_batch_loop(delay_seconds, max_per_batch, use_batch_api))

    logger.info(f"[BATCH] Started — delay={delay_seconds}s, max={max_per_batch or 'all'}, batch_api={use_batch_api}")
    return {"ok": True, "message": "Batch started", "state": dict(_state)}


def stop_batch() -> dict:
    """
    Request a graceful stop. Current PDF finishes; next one won't start.
    """
    global _stop_requested
    _stop_requested = True
    if _state["status"] == "running":
        _state["status"] = "stopping"
        _save_state()
    logger.info("[BATCH] Stop requested — will halt after current PDF")
    return {"ok": True, "message": "Stop requested — finishing current PDF", "state": dict(_state)}


# ── Core loop ────────────────────────────────────────────────────

async def _run_batch_loop(delay_seconds: int, max_per_batch: int = 0, use_batch_api: bool = False) -> None:
    """
    Main batch loop. Runs in the background via asyncio.create_task().
    Imports pipeline pieces inline to avoid circular imports.
    """
    global _stop_requested

    from app.inbox import list_inbox

    # Snapshot all unprocessed files at start, apply limit if set
    all_unprocessed = [
        f["name"] for f in list_inbox()
        if f["status"] == "unprocessed"
    ]
    inbox_files = all_unprocessed[:max_per_batch] if max_per_batch > 0 else all_unprocessed
    total = len(inbox_files)

    if total == 0:
        _patch_state(status="completed", total=0, processed=0,
                     started_at=_now(), finished_at=_now(),
                     eta_seconds=0)
        logger.info("[BATCH] Inbox is empty — nothing to process")
        return

    _patch_state(
        status="running",
        total=total,
        processed=0,
        failed=0,
        failed_files=[],
        started_at=_now(),
        finished_at=None,
        eta_seconds=total * delay_seconds,
    )

    # ── Batch API path (50% cost, async ~1h) ─────────────────────────
    if use_batch_api:
        proc, fail, ff = await _process_batch_via_api(inbox_files)
        _patch_state(
            status="completed", processed=proc, failed=fail, failed_files=ff,
            eta_seconds=0, current_file=None, finished_at=_now(),
        )
        logger.info(f"[BATCH] Done (Batch API) — {proc} processed, {fail} failed")
        return

    processed = 0
    failed    = 0
    failed_files = []

    for filename in inbox_files:
        if _stop_requested:
            _patch_state(status="idle", eta_seconds=None, current_file=None)
            logger.info("[BATCH] Stopped by user request")
            return

        _patch_state(current_file=filename)
        logger.info(f"[BATCH] Processing ({processed+1}/{total}): {filename}")

        t0 = time.time()
        success = await _process_one(filename)
        elapsed = time.time() - t0

        if success:
            processed += 1
        else:
            failed += 1
            failed_files.append(filename)

        remaining = total - processed - failed
        eta = int(remaining * max(elapsed, delay_seconds))
        _patch_state(
            processed=processed,
            failed=failed,
            failed_files=failed_files,
            eta_seconds=eta,
            current_file=None,
        )

        # Delay before next PDF (skip delay after last one)
        if filename != inbox_files[-1] and not _stop_requested:
            logger.info(f"[BATCH] Waiting {delay_seconds}s before next PDF...")
            await asyncio.sleep(delay_seconds)

    _patch_state(
        status="completed",
        eta_seconds=0,
        current_file=None,
        finished_at=_now(),
    )
    logger.info(f"[BATCH] Done — {processed} processed, {failed} failed")


async def _process_one(filename: str) -> bool:
    """
    Process a single PDF through the full pipeline.
    Returns True on success, False on failure (PDF reverted to inbox).
    """
    import traceback
    import time as _time

    from app.inbox import claim_pdf, complete_pdf, revert_pdf
    from app.pdf_reader import read_pdf
    from app.ai_client import extract_fields, analyze_letter, analyze_images
    from app.checker import check_domain, check_company_online
    from app.rules import (
        compute_date_logic, compute_salary_math,
        compute_completeness, compute_final_score,
    )
    from app.report_writer import save_report

    processing_path = claim_pdf(filename)
    if processing_path is None:
        logger.warning(f"[BATCH] Could not claim {filename} — skipping")
        return False

    start_time = _time.time()
    try:
        file_bytes   = processing_path.read_bytes()
        file_size_kb = len(file_bytes) // 1024

        raw = read_pdf(file_bytes)
        if not raw.full_text.strip():
            # Surface exactly why extraction failed (pdfplumber empty, OCR missing, etc.)
            detail = "; ".join(raw.extraction_warnings) if raw.extraction_warnings else "no details available"
            raise ValueError(f"No text extracted — {detail}")

        fields = await asyncio.to_thread(extract_fields, raw)
        images = analyze_images(raw)

        (dns, company_online, date_logic, salary_math, completeness) = await asyncio.gather(
            asyncio.to_thread(check_domain,         fields.company_domain or ""),
            asyncio.to_thread(check_company_online, fields.company_name, fields.company_domain),
            asyncio.to_thread(compute_date_logic,   fields),
            asyncio.to_thread(compute_salary_math,  fields),
            asyncio.to_thread(compute_completeness, fields, raw),
        )

        analysis = await asyncio.to_thread(
            analyze_letter,
            fields=fields, raw=raw,
            company_online=company_online, dns=dns,
            date_logic=date_logic, salary_math=salary_math,
            completeness=completeness,
        )

        elapsed_ms = int((_time.time() - start_time) * 1000)
        result = compute_final_score(
            fields=fields, raw=raw,
            company_online=company_online, dns=dns,
            analysis=analysis, images=images,
            date_logic=date_logic, salary_math=salary_math,
            completeness=completeness,
            file_name=filename, file_size_kb=file_size_kb,
            processing_time_ms=elapsed_ms,
        )

        complete_pdf(filename)
        save_report(result, filename)
        logger.info(f"[BATCH] ✓ {filename} → {result.verdict} ({result.overall_score}/100)")
        return True

    except Exception as e:
        revert_pdf(filename)
        logger.error(f"[BATCH] ✗ {filename} failed: {e}")
        traceback.print_exc()
        return False


# ── Batch API path (50% cost) ────────────────────────────────────

async def _process_batch_via_api(filenames: list[str]) -> tuple[int, int, list]:
    """
    Score every letter through Anthropic's Batches API (50% of standard price).
    Per-letter local work (read, extract, free checks) still runs up front; only
    the one paid SCORING call per letter is batched. Returns (processed, failed,
    failed_files). One bad PDF never sinks the batch — results are per-letter.

    NOTE: OCR (image-based PDFs) and any regex→Claude extraction fallback are
    still synchronous full-price calls — only analyze_letter is batched.
    """
    import time as _time
    from app.inbox import claim_pdf, complete_pdf, revert_pdf
    from app.pdf_reader import read_pdf
    from app.ai_client import (
        extract_fields, analyze_images, get_client,
        build_batch_request, parse_batch_result,
    )
    from app.checker import check_domain, check_company_online
    from app.rules import (
        compute_date_logic, compute_salary_math,
        compute_completeness, compute_final_score,
    )
    from app.report_writer import save_report

    ctx_by_id: dict = {}
    requests: list = []
    processed = failed = 0
    failed_files: list = []

    # ── Phase 1: per-letter local prep + build scoring requests ──────
    for i, filename in enumerate(filenames):
        if _stop_requested:
            break
        cid = f"req{i}"
        path = claim_pdf(filename)
        if path is None:
            failed += 1; failed_files.append(filename)
            continue
        try:
            fb  = path.read_bytes()
            raw = read_pdf(fb)
            if not raw.full_text.strip():
                detail = "; ".join(raw.extraction_warnings) or "no details"
                raise ValueError(f"No text extracted — {detail}")
            fields = await asyncio.to_thread(extract_fields, raw)
            images = analyze_images(raw)
            dns, co, dl, sm, comp = await asyncio.gather(
                asyncio.to_thread(check_domain,         fields.company_domain or ""),
                asyncio.to_thread(check_company_online, fields.company_name, fields.company_domain),
                asyncio.to_thread(compute_date_logic,   fields),
                asyncio.to_thread(compute_salary_math,  fields),
                asyncio.to_thread(compute_completeness, fields, raw),
            )
            req, images_available = build_batch_request(cid, fields, raw, co, dns, dl, sm, comp)
            requests.append(req)
            ctx_by_id[cid] = dict(
                filename=filename, fields=fields, raw=raw, images=images,
                company_online=co, dns=dns, date_logic=dl, salary_math=sm,
                completeness=comp, images_available=images_available,
                file_size_kb=len(fb) // 1024, start=_time.time(),
            )
        except Exception as e:
            revert_pdf(filename); failed += 1; failed_files.append(filename)
            logger.error(f"[BATCH-API] prep failed for {filename}: {e}")

    if not requests:
        return processed, failed, failed_files

    client = get_client()

    # ── Phase 2: submit one batch ────────────────────────────────────
    batch = await asyncio.to_thread(lambda: client.messages.batches.create(requests=requests))
    logger.info(f"[BATCH-API] Submitted {len(requests)} scoring requests as {batch.id}")
    _patch_state(current_file=f"Batch {batch.id} — {len(requests)} letters scoring (async, ~1h)")

    # ── Phase 3: poll until ended ────────────────────────────────────
    while True:
        b = await asyncio.to_thread(lambda: client.messages.batches.retrieve(batch.id))
        if b.processing_status == "ended":
            break
        logger.info(f"[BATCH-API] {batch.id} status={b.processing_status} "
                    f"processing={b.request_counts.processing}")
        await asyncio.sleep(20)

    # ── Phase 4: per-letter results → score + save ───────────────────
    results = await asyncio.to_thread(lambda: list(client.messages.batches.results(batch.id)))
    for r in results:
        ctx = ctx_by_id.get(r.custom_id)
        if not ctx:
            continue
        fn = ctx["filename"]
        try:
            if r.result.type != "succeeded":
                raise ValueError(f"batch result type={r.result.type}")
            text = r.result.message.content[0].text
            analysis = parse_batch_result(text, ctx["images_available"])
            elapsed_ms = int((_time.time() - ctx["start"]) * 1000)
            result = compute_final_score(
                fields=ctx["fields"], raw=ctx["raw"], company_online=ctx["company_online"],
                dns=ctx["dns"], analysis=analysis, images=ctx["images"],
                date_logic=ctx["date_logic"], salary_math=ctx["salary_math"],
                completeness=ctx["completeness"], file_name=fn,
                file_size_kb=ctx["file_size_kb"], processing_time_ms=elapsed_ms,
            )
            complete_pdf(fn); save_report(result, fn)
            processed += 1
            logger.info(f"[BATCH-API] ✓ {fn} → {result.verdict} ({result.overall_score}/100)")
        except Exception as e:
            revert_pdf(fn); failed += 1; failed_files.append(fn)
            logger.error(f"[BATCH-API] ✗ {fn} result failed: {e}")

    return processed, failed, failed_files


# ── Helpers ──────────────────────────────────────────────────────

def _patch_state(**kwargs) -> None:
    """Update in-memory state and persist to disk."""
    _state.update(kwargs)
    _save_state()


def _save_state() -> None:
    try:
        STATUS_FILE.write_text(json.dumps(_state, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning(f"[BATCH] Could not save state: {e}")


def _load_state() -> None:
    """Load persisted state from disk on first call / after restart."""
    if STATUS_FILE.exists():
        try:
            data = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
            # If server restarted mid-run, mark as idle
            if data.get("status") in ("running", "stopping"):
                data["status"] = "idle"
                data["current_file"] = None
            _state.update(data)
        except Exception:
            pass


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

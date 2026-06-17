import io
import re
import base64
from typing import Optional
from datetime import datetime

import pdfplumber
import fitz
from dateutil import parser as dateparser

from app.models import RawPDFData, ImageData, PDFMetadata
from app.config import settings


def read_pdf(file_bytes: bytes) -> RawPDFData:
    """
    Extract everything we can from the PDF without Claude.
    - Full text (all pages) — with OCR fallback for image-based PDFs
    - Images (logo, signature, stamp)
    - Document metadata
    - Red phrase scan
    - Placeholder scan (unfilled fields: RRRR, ____, [...], etc.)
    Returns RawPDFData which gets passed to Claude for field extraction.
    """
    result = RawPDFData()

    result.full_text = _extract_text(file_bytes, result)
    result.images = _extract_images(file_bytes, result)
    result.metadata = _extract_metadata(file_bytes, result)
    result.red_phrases_found = _scan_red_phrases(result.full_text)
    result.placeholder_scan = _scan_placeholders(file_bytes, result)
    result.tamper_artifacts = _scan_floating_dates(file_bytes, result)
    result.rendered_pages = _render_pages(file_bytes, result)
    result.signature_crop = _render_signature_crop(file_bytes, result)
    result.composite_artifacts = _scan_composite_forgery(file_bytes, result)

    return result


# ── TAMPER / EDIT-ARTIFACT SCAN ─────────────────────────────────

# A line whose entire text is just a date (e.g. "April 2026." or "23rd March 2026").
_FLOATING_DATE_RE = re.compile(
    r'^\d{0,2}\s*(?:st|nd|rd|th)?\s*'
    r'(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s*\d{4}\.?$',
    re.IGNORECASE,
)

def _scan_floating_dates(file_bytes: bytes, result: RawPDFData) -> list[str]:
    """
    Detect a date sitting ALONE as its own text line in the BODY zone (not the
    header/footer). A genuine offer states dates inside sentences or table rows;
    a lone floating date is a classic copy-paste edit artifact — e.g. an online
    PDF editor that changed a joining date pastes "April 2026." back as a NEW
    text object, which renders inline but is structurally a separate line.
    Header/footer dates (letterhead date, sign-off date) are excluded by zone.
    """
    artifacts = []
    try:
        doc = fitz.open(stream=file_bytes, filetype="pdf")
        for pno in range(len(doc)):
            page = doc[pno]
            h = page.rect.height or 1
            for block in page.get_text("dict").get("blocks", []):
                for line in block.get("lines", []):
                    txt = "".join(s.get("text", "") for s in line.get("spans", [])).strip()
                    if not txt:
                        continue
                    frac = line["bbox"][1] / h            # vertical position (0=top, 1=bottom)
                    if 0.15 < frac < 0.85 and _FLOATING_DATE_RE.match(txt):
                        artifacts.append(f"page {pno + 1}: floating date '{txt}'")
        doc.close()
        if artifacts:
            result.extraction_warnings.append(
                f"Tamper scan: {len(artifacts)} floating date line(s) in document body"
            )
    except Exception as e:
        result.extraction_warnings.append(f"Floating-date scan error: {str(e)}")
    return artifacts


# ── TEXT EXTRACTION ─────────────────────────────────────────────

def _extract_text(file_bytes: bytes, result: RawPDFData) -> str:
    """
    Extract text from PDF.
    First tries pdfplumber (fast, accurate for text-based PDFs).
    If that returns nothing, falls back to OCR (for Canva/scanned/image-based PDFs).
    """
    # Try pdfplumber first
    try:
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            pages = []
            for page in pdf.pages:
                text = page.extract_text()
                if text:
                    pages.append(text)
            text = "\n".join(pages)
            if text.strip():
                return text
    except Exception as e:
        result.extraction_warnings.append(f"pdfplumber error: {str(e)}")

    # Fallback: OCR via Claude Vision
    result.extraction_warnings.append("No text layer found — falling back to OCR")
    return _ocr_pdf(file_bytes, result)


def _ocr_pdf(file_bytes: bytes, result: RawPDFData) -> str:
    """
    OCR fallback using Claude Vision — only called for image-based PDFs where
    pdfplumber found no text layer. Renders each page and asks Claude to extract
    the text. NOT called for normal text-based PDFs — zero extra cost for those.
    Capped at 10 pages (offer letters are never longer than that).
    """
    try:
        import anthropic

        doc = fitz.open(stream=file_bytes, filetype="pdf")
        n   = min(len(doc), 10)      # cap — safety + cost guard
        # 1.5× zoom matches _render_pages; JPEG 85% keeps each page ≈200–400 KB
        # (PNG at 2× was hitting 14–15 MB — well over Claude's 5 MB/image limit)
        mat = fitz.Matrix(1.5, 1.5)

        page_images = []
        for i in range(n):
            pix        = doc[i].get_pixmap(matrix=mat)
            jpeg_bytes = pix.tobytes("jpeg", jpg_quality=85)
            page_images.append(base64.b64encode(jpeg_bytes).decode())
        doc.close()

        if not page_images:
            result.extraction_warnings.append("Claude OCR: no pages rendered")
            return ""

        # Build multimodal message — text instruction first, then page images
        content: list = [{
            "type": "text",
            "text": (
                "These are page images from an Indian corporate offer letter PDF. "
                "Extract ALL text exactly as it appears — preserve line breaks, "
                "tables, and numbers. Output only the raw extracted text, "
                "no commentary, no markdown fences."
            ),
        }]
        for b64 in page_images:
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": "image/jpeg", "data": b64},
            })

        client   = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        response = client.messages.create(
            model      = settings.claude_model,
            max_tokens = 6000,
            messages   = [{"role": "user", "content": content}],
        )

        text = response.content[0].text
        if text.strip():
            result.extraction_warnings.append(
                f"Claude Vision OCR: extracted {len(text)} chars "
                f"from {n} page(s) — PDF was image-based"
            )
            return text
        else:
            result.extraction_warnings.append(
                "Claude Vision OCR returned no text — PDF may be blank or unreadable"
            )
            return ""

    except Exception as e:
        result.extraction_warnings.append(f"Claude Vision OCR error: {str(e)}")
        return ""


# ── IMAGE EXTRACTION ────────────────────────────────────────────

def _extract_images(file_bytes: bytes, result: RawPDFData) -> ImageData:
    """Extract images using pymupdf — better image handling than pdfplumber."""
    images = ImageData()

    try:
        doc = fitz.open(stream=file_bytes, filetype="pdf")

        for page_num in range(len(doc)):
            page = doc[page_num]
            page_height = page.rect.height
            page_width = page.rect.width

            img_list = page.get_images(full=True)
            images.total_images_found += len(img_list)

            for img in img_list:
                xref = img[0]
                base_image = doc.extract_image(xref)
                img_bytes = base_image["image"]
                img_ext = base_image["ext"]

                img_rects = page.get_image_rects(xref)
                if not img_rects:
                    continue

                rect = img_rects[0]
                img_w = rect.width
                img_h = rect.height

                # Skip tiny decorative images
                if img_w < 30 or img_h < 30:
                    continue

                # Position
                img_y = rect.y0
                img_x = rect.x0
                vertical = "top" if img_y < page_height * 0.25 else ("bottom" if img_y > page_height * 0.75 else "middle")
                horizontal = "left" if img_x < page_width * 0.33 else ("right" if img_x > page_width * 0.66 else "center")
                position = f"{vertical}-{horizontal}"

                b64 = f"data:image/{img_ext};base64,{base64.b64encode(img_bytes).decode()}"

                # Classify
                if "top" in position and not images.has_logo:
                    images.has_logo = True
                    images.logo_position = position
                    images.logo_base64 = b64
                elif "bottom" in position and not images.has_signature:
                    images.has_signature = True
                    images.signature_position = position
                    images.signature_base64 = b64
                elif abs(img_w - img_h) < 20 and not images.has_stamp:
                    images.has_stamp = True
                    images.stamp_base64 = b64

        doc.close()

    except Exception as e:
        result.extraction_warnings.append(f"Image extraction error: {str(e)}")

    return images


# ── METADATA EXTRACTION ─────────────────────────────────────────

def _extract_metadata(file_bytes: bytes, result: RawPDFData) -> PDFMetadata:
    meta = PDFMetadata()

    try:
        doc = fitz.open(stream=file_bytes, filetype="pdf")
        raw = doc.metadata

        meta.author = raw.get("author") or None
        meta.created_with = raw.get("creator") or raw.get("producer") or None

        created = _parse_pdf_date(raw.get("creationDate", ""))
        modified = _parse_pdf_date(raw.get("modDate", ""))

        if created:
            meta.created_date = created.strftime("%Y-%m-%d")
        if modified:
            meta.modified_date = modified.strftime("%Y-%m-%d")

        suspicious = []

        # ── CHECK 1: Fully stripped metadata ────────────────────────
        if not meta.author and not meta.created_date and not meta.created_with:
            suspicious.append("Metadata fully stripped — no author, date, or software found")

        # ── CHECK 2: Future creation date (impossible) ───────────────
        if created:
            days_ahead = (created - datetime.now()).days
            if days_ahead > 1:
                suspicious.append(f"Creation date is {days_ahead} days in the future — impossible, clock tampering suspected")

        # ── CHECK 3: Document edited after creation ──────────────────
        if created and modified:
            gap_days = (modified - created).days
            if gap_days > 0:
                meta.modified_after_creation = True
                meta.modification_gap_days   = gap_days
            if gap_days > 1:
                suspicious.append(
                    f"Modified {gap_days} days after creation — document was edited post-creation"
                )

        # ── CHECK 4: Fraud / consumer software ──────────────────────
        if meta.created_with:
            # Design tools — not used by corporate HR systems (strong signal)
            design_tools = [
                "Canva", "Photoshop", "GIMP", "Illustrator",
                "CorelDraw", "Paint.NET", "Inkscape", "Affinity"
            ]
            # PDF editing suites — strong signal of document manipulation
            editing_suites = [
                "Foxit PhantomPDF", "Foxit PDF Editor",
                "WPS Office", "WPS PDF"
            ]
            # Online PDF processors — lighter signal (may just be compression/conversion)
            # These trigger a minor penalty only, NOT suspicious_metadata
            online_processors = [
                "iLovePDF", "Smallpdf", "PDF24", "PDFescape",
                "PDFsam", "Sejda"
            ]

            strong_fraud_sw = design_tools + editing_suites
            for sw in strong_fraud_sw:
                if sw.lower() in meta.created_with.lower():
                    suspicious.append(f"Created/edited with '{sw}' — not a corporate HR tool")

            # Online processors: set lighter flag, not full suspicious_metadata
            for sw in online_processors:
                if sw.lower() in meta.created_with.lower():
                    meta.online_edit_detected = True
                    meta.online_edit_tool = sw
                    break   # one tool is enough

        if suspicious:
            meta.suspicious_metadata = True
            meta.suspicious_reason = "; ".join(suspicious)

        doc.close()

    except Exception as e:
        result.extraction_warnings.append(f"Metadata error: {str(e)}")

    return meta


# ── PAGE RENDERER ───────────────────────────────────────────────

_SALARY_KW = frozenset([
    "basic", "hra", "ctc", "gross salary", "net salary", "net pay",
    "total compensation", "salary breakup", "annexure", "allowance",
    "deductions", "provident fund", "gratuity", "cost to company",
    "total fixed pay", "tfp", "stipend",
])
_SIGNATURE_KW = frozenset([
    "authorized signatory", "authorised signatory", "yours sincerely",
    "yours faithfully", "with regards", "warm regards", "best regards",
    "hr manager", "human resources", "signing authority", "for and on behalf",
    "acceptance", "employee signature",
])

def _detect_key_pages(doc, n: int) -> tuple[list[int], list[int]]:
    """Return (salary_pages, signature_pages) by keyword-scanning each page."""
    salary_pages = []
    signature_pages = []
    for i in range(n):
        text_lc = doc[i].get_text().lower()
        if any(kw in text_lc for kw in _SALARY_KW):
            salary_pages.append(i)
        if any(kw in text_lc for kw in _SIGNATURE_KW):
            signature_pages.append(i)
    return salary_pages, signature_pages


def _render_pages(file_bytes: bytes, result: RawPDFData) -> list[str]:
    """
    Render key PDF pages as base64 PNG images for Claude Vision scoring.

    For 1–4 page docs: render all pages (up to 4 images).
    For 5+ page docs: keyword-detect salary/annexure and signature pages instead
    of blindly picking [0,1,2,last]. Always include page 0 (letterhead), then
    add the most relevant salary and signature pages. Cap at 5 images.

    Anthropic charges ~1500 tokens per image flat (within 1568px limit).
    """
    rendered = []
    try:
        doc = fitz.open(stream=file_bytes, filetype="pdf")
        n = len(doc)

        if n <= 4:
            pages_to_render = list(range(n))
        else:
            salary_pages, signature_pages = _detect_key_pages(doc, n)

            # Always start with page 0 (letterhead) and page 1 (body/terms)
            candidates = [0, 1]

            # Add the last detected salary/annexure page (most likely to have the table)
            if salary_pages:
                candidates.append(salary_pages[-1])

            # Add the last detected signature page
            if signature_pages:
                candidates.append(signature_pages[-1])

            # If we couldn't find specific pages, fall back to last page
            if not salary_pages and not signature_pages:
                candidates.append(n - 1)

            # Deduplicate while preserving order, cap at 5
            seen: set[int] = set()
            pages_to_render = [p for p in candidates if p < n and not (p in seen or seen.add(p))][:5]

        # Deduplicate while preserving order (safety for ≤4 path)
        seen2: set[int] = set()
        ordered = [p for p in pages_to_render if not (p in seen2 or seen2.add(p))]

        mat = fitz.Matrix(1.5, 1.5)
        for page_num in ordered:
            page = doc[page_num]
            pix = page.get_pixmap(matrix=mat)
            png_bytes = pix.tobytes("png")
            b64 = base64.b64encode(png_bytes).decode()
            rendered.append(f"data:image/png;base64,{b64}")

        doc.close()
        result.extraction_warnings.append(
            f"Rendered {len(rendered)} page(s) for visual analysis: {[p+1 for p in ordered]}"
        )
    except Exception as e:
        result.extraction_warnings.append(f"Page render error: {str(e)}")

    return rendered


def _render_signature_crop(file_bytes: bytes, result: RawPDFData) -> Optional[str]:
    """
    Option A — one high-resolution close-up of the signature zone.

    A full-page render is downscaled by Anthropic to ~1.15 MP, so the signature
    occupies only ~150 px and paste/relocation artifacts are unresolvable. Here we
    crop the LOWER portion of the signature page and render it at high zoom, so the
    signature fills the frame and Claude can judge signature_tampering properly.

    Cost-aware: returns exactly ONE extra image (the single most-likely signature
    page), or None if the document has no pages. Wired as raw.signature_crop and
    appended to the vision payload in ai_client.analyze_letter.
    """
    try:
        doc = fitz.open(stream=file_bytes, filetype="pdf")
        n = len(doc)
        if n == 0:
            doc.close()
            return None

        # Pick the signature page: last page whose text shows a sign-off keyword;
        # fall back to the last page (image-based scans have no text to match).
        sig_page = n - 1
        for i in range(n):
            if any(kw in doc[i].get_text().lower() for kw in _SIGNATURE_KW):
                sig_page = i  # keep the LAST match — sign-off is near the end
        page = doc[sig_page]

        # Clip to the lower 45% of the page (where signatures/sign-off sit) and
        # render at 3× so that band fills ~1 MP after Anthropic's downscale.
        rect = page.rect
        clip = fitz.Rect(rect.x0, rect.y0 + rect.height * 0.55, rect.x1, rect.y1)
        pix = page.get_pixmap(matrix=fitz.Matrix(3, 3), clip=clip)
        png_bytes = pix.tobytes("png")
        doc.close()

        b64 = base64.b64encode(png_bytes).decode()
        result.extraction_warnings.append(
            f"Signature close-up rendered from page {sig_page + 1} "
            f"({pix.width}x{pix.height}px) for signature_tampering"
        )
        return f"data:image/png;base64,{b64}"
    except Exception as e:
        result.extraction_warnings.append(f"Signature crop error: {str(e)}")
        return None


def _scan_composite_forgery(file_bytes: bytes, result: RawPDFData) -> list[str]:
    """
    Detect a LAYERED / COMPOSITED forgery — a document fabricated by overlaying
    content onto a real company's blank letterhead, then flattening it.

    The Ajay-letter signature: a page with (a) NO selectable text layer, (b) a
    large background image (the blank letterhead), (c) one or more small pasted
    images (a signature scribble and/or a rubber stamp), (d) heavy overlaid vector
    drawing content (the body text rendered as paths), and (e) no embedded fonts.

    A genuine letter is never built this way: real digital letters carry a text
    layer + fonts; real scans are a SINGLE flattened image with no pasted overlays
    or vector text. Validated free over the full corpus: 1/422 matches (only the
    known forgery), 0 false positives. Fitz-only — no Claude, no extra cost.
    """
    artifacts: list[str] = []
    try:
        doc = fitz.open(stream=file_bytes, filetype="pdf")
        for pno in range(len(doc)):
            page = doc[pno]
            text_len = len(page.get_text().strip())
            areas = []
            for im in page.get_images(full=True):
                try:
                    d = doc.extract_image(im[0])
                    areas.append(d["width"] * d["height"])
                except Exception:
                    pass
            small_imgs = sum(1 for a in areas if a < 80_000)        # ~signature/stamp sized
            big_imgs   = sum(1 for a in areas if a > 1_000_000)     # ~full-page background
            drawings   = len(page.get_drawings())                  # overlaid vector content
            fonts      = len(page.get_fonts(full=True))

            if text_len < 20 and big_imgs >= 1 and small_imgs >= 1 and drawings > 150 and fonts == 0:
                artifacts.append(
                    f"page {pno + 1}: composited document — overlaid text + "
                    f"{small_imgs} pasted image(s) on a background letterhead, no text layer"
                )
        doc.close()
        if artifacts:
            result.extraction_warnings.append(
                f"Composite-forgery scan: {len(artifacts)} layered page(s) detected"
            )
    except Exception as e:
        result.extraction_warnings.append(f"Composite-forgery scan error: {str(e)}")
    return artifacts


# ── PLACEHOLDER SCAN ────────────────────────────────────────────

_PLACEHOLDER_PATTERNS = [
    (re.compile(r'R{5,}', re.IGNORECASE),          'RRRR-style blank'),
    (re.compile(r'_{4,}'),                           'underscore blank'),
    (re.compile(r'\[[A-Z][A-Z ]{2,}\]'),             'bracket placeholder'),
    (re.compile(r'-{5,}'),                            'dash blank'),
    (re.compile(r'\bX{4,}\b', re.IGNORECASE),        'XXX-style blank'),
    (re.compile(r'\{\{[^}]+\}\}'),                   'template variable'),
    (re.compile(r'<[A-Z][A-Z _]+>'),                 'angle-bracket placeholder'),
]

def _scan_placeholders(file_bytes: bytes, result: RawPDFData) -> list[dict]:
    """
    Scan each page for unfilled placeholder patterns before Claude sees anything.
    Regex can reliably catch RRRRRR, dashes, brackets that Claude may miss in noisy text.
    Returns per-page findings: [{"page": 1, "patterns": [{"type": ..., "examples": [...]}]}]
    """
    findings = []
    try:
        doc = fitz.open(stream=file_bytes, filetype="pdf")
        for page_num in range(len(doc)):
            text = doc[page_num].get_text()
            page_findings = []
            for pattern, label in _PLACEHOLDER_PATTERNS:
                matches = pattern.findall(text)
                if matches:
                    page_findings.append({"type": label, "examples": matches[:3]})
            if page_findings:
                findings.append({"page": page_num + 1, "patterns": page_findings})
        doc.close()
        if findings:
            total = sum(len(f["patterns"]) for f in findings)
            result.extraction_warnings.append(
                f"Placeholder scan: {total} pattern type(s) found across {len(findings)} page(s)"
            )
    except Exception as e:
        result.extraction_warnings.append(f"Placeholder scan error: {str(e)}")
    return findings


# ── RED PHRASE SCAN ─────────────────────────────────────────────

def _scan_red_phrases(text: str) -> list[str]:
    if not text:
        return []
    text_lower = text.lower()
    return [phrase for phrase in settings.red_phrases if phrase.lower() in text_lower]


# ── HELPERS ─────────────────────────────────────────────────────

def _parse_pdf_date(date_str: str) -> Optional[datetime]:
    if not date_str:
        return None
    try:
        clean = re.sub(r"D:|[Z'].*$", "", date_str).strip()
        return datetime.strptime(clean[:14], "%Y%m%d%H%M%S")
    except Exception:
        try:
            return dateparser.parse(date_str)
        except Exception:
            return None
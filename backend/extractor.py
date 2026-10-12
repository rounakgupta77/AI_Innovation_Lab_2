"""Reads bill number, bill date and amount (plus a bill-type guess) from an uploaded bill.

Order of attempts:
  1. Claude vision model (when ANTHROPIC_API_KEY is set)
  2. Local OCR with Tesseract (or the embedded text of a PDF) + pattern matching
  3. Blank fields, which the employee fills in by hand
"""
import base64
import io
import logging
import math
import os
import re
from datetime import date
from typing import Literal

log = logging.getLogger("extractor")

MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-5-5")
BILL_TYPES = ("Fuel", "Repair", "Maintenance", "Other")

try:
    import anthropic
    from pydantic import BaseModel, Field
except ImportError:
    anthropic = None

try:
    from PIL import Image, ImageOps
except ImportError:
    Image = None

try:
    import pytesseract
except ImportError:
    pytesseract = None

try:
    import fitz  # PyMuPDF
except ImportError:
    fitz = None


# --------------------------------------------------------------------------- #
# Capability checks
# --------------------------------------------------------------------------- #

_tesseract_state = None


def ai_ready():
    return anthropic is not None and bool(os.environ.get("ANTHROPIC_API_KEY"))


def ocr_ready():
    global _tesseract_state
    if _tesseract_state is not None:
        return _tesseract_state
    _tesseract_state = False
    if pytesseract is None or Image is None:
        return False
    cmd = os.environ.get("TESSERACT_CMD")
    if not cmd:
        for candidate in (
            r"C:\Program Files\Tesseract-OCR\tesseract.exe",
            r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe"),
            os.path.expandvars(r"%LOCALAPPDATA%\Tesseract-OCR\tesseract.exe"),
        ):
            if os.path.exists(candidate):
                cmd = candidate
                break
    if cmd:
        pytesseract.pytesseract.tesseract_cmd = cmd
    try:
        pytesseract.get_tesseract_version()
        _tesseract_state = True
    except Exception:
        _tesseract_state = False
    return _tesseract_state


def capabilities():
    return {
        "ai": ai_ready(),
        "model": MODEL if ai_ready() else None,
        "ocr": ocr_ready(),
        "pdf_text": fitz is not None,
    }


# --------------------------------------------------------------------------- #
# Claude extraction
# --------------------------------------------------------------------------- #

PROMPT = """This is a bill or receipt an employee submitted for reimbursement of fuel or \
car repair/maintenance expenses. Read it and extract:

- bill_number: the bill / invoice / receipt / transaction number exactly as printed. \
Empty string if none is visible.
- bill_date: the date of the bill in YYYY-MM-DD format. Indian bills usually print dates \
as DD/MM/YYYY or DD-MM-YY. Empty string if no date is visible.
- amount: the final total amount paid, including taxes, as a plain number without currency \
symbols or commas. 0 if no amount is visible.
- bill_type: "Fuel" for petrol, diesel, CNG or EV charging; "Repair" for repair work, spare \
parts or bodywork; "Maintenance" for periodic servicing, oil change, tyres, battery, washing; \
otherwise "Other".

If the image is not a bill at all, return empty strings, 0 and "Other"."""

if anthropic is not None:

    class BillFields(BaseModel):
        bill_number: str = Field(description="Bill/invoice/receipt number as printed, or empty string")
        bill_date: str = Field(description="Bill date as YYYY-MM-DD, or empty string")
        amount: float = Field(description="Final total amount paid, 0 if not visible")
        bill_type: Literal["Fuel", "Repair", "Maintenance", "Other"]

    _client = None

    def _claude():
        global _client
        if _client is None:
            _client = anthropic.Anthropic(timeout=120, max_retries=2)
        return _client


_NATIVE_IMAGE = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp", "GIF": "image/gif"}
_EXT_MEDIA = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
              ".webp": "image/webp", ".gif": "image/gif"}
_MAX_IMAGE_BYTES = 4_500_000


def _image_for_claude(path):
    """Returns (bytes, media_type), converting/downscaling when the API would reject the file."""
    with open(path, "rb") as fh:
        raw = fh.read()
    if Image is None:
        media = _EXT_MEDIA.get(os.path.splitext(path)[1].lower())
        if media is None:
            raise ValueError("Pillow is needed to convert this image format")
        return raw, media
    img = Image.open(io.BytesIO(raw))
    if img.format in _NATIVE_IMAGE and len(raw) <= _MAX_IMAGE_BYTES and max(img.size) <= 2600:
        return raw, _NATIVE_IMAGE[img.format]
    img = ImageOps.exif_transpose(img).convert("RGB")
    img.thumbnail((2400, 2400))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return buf.getvalue(), "image/jpeg"


def _extract_with_claude(path):
    if path.lower().endswith(".pdf"):
        with open(path, "rb") as fh:
            data = base64.standard_b64encode(fh.read()).decode()
        block = {"type": "document",
                 "source": {"type": "base64", "media_type": "application/pdf", "data": data}}
    else:
        raw, media = _image_for_claude(path)
        block = {"type": "image",
                 "source": {"type": "base64", "media_type": media,
                            "data": base64.standard_b64encode(raw).decode()}}

    response = _claude().messages.parse(
        model=MODEL,
        max_tokens=16000,
        messages=[{"role": "user", "content": [block, {"type": "text", "text": PROMPT}]}],
        output_format=BillFields,
    )
    if response.stop_reason == "refusal" or response.parsed_output is None:
        raise RuntimeError(f"no result from Claude (stop_reason={response.stop_reason})")
    fields = response.parsed_output
    return {
        "bill_number": fields.bill_number.strip()[:64],
        "bill_date": _normalise_date(fields.bill_date),
        "amount": _clean_amount(fields.amount),
        "bill_type": fields.bill_type,
    }


# --------------------------------------------------------------------------- #
# Local OCR fallback
# --------------------------------------------------------------------------- #

def _ocr_text(path):
    if path.lower().endswith(".pdf"):
        if fitz is None:
            return ""
        with fitz.open(path) as doc:
            text = "\n".join(page.get_text() for page in doc)
            if len(text.strip()) >= 20 or not ocr_ready() or doc.page_count == 0:
                return text
            pix = doc[0].get_pixmap(dpi=250)
            img = Image.open(io.BytesIO(pix.tobytes("png")))
    else:
        if not ocr_ready():
            return ""
        img = ImageOps.exif_transpose(Image.open(path))
    return pytesseract.image_to_string(ImageOps.grayscale(img))


_MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}
_YMD = re.compile(r"\b(20\d{2})[/\-.](\d{1,2})[/\-.](\d{1,2})\b")
_DMY = re.compile(r"\b(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})\b")
_D_MON_Y = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?[\s\-/.,]*([A-Za-z]{3,9})[\s\-/.,']*(\d{2,4})\b")
_MON_D_Y = re.compile(r"\b([A-Za-z]{3,9})[\s\-.]*(\d{1,2})(?:st|nd|rd|th)?,?[\s\-.]*(\d{4})\b")

_BILL_NO = re.compile(
    r"(?:bill|invoice|inv|receipt|rcpt|txn|transaction|trans|memo|voucher|reference|ref)"
    r"\s*\.?\s*(?:no|num(?:ber)?|#|id)?\s*\.?\s*[:#\-]?\s*([A-Za-z0-9][A-Za-z0-9\-/]{1,30})",
    re.I)

_AMOUNT_TIERS = [
    re.compile(r"grand\s*total|net\s*(?:amount|amt|payable|total)|total\s*(?:amount|amt|payable|value)"
               r"|amount\s*(?:paid|payable)|bill\s*amount|sale\s*amount|invoice\s*amount", re.I),
    re.compile(r"total", re.I),
    re.compile(r"amount|amt|rs\.?|inr|₹|paid", re.I),
]
_NUMBER = re.compile(r"(?<![\d.])(\d{1,3}(?:,\d{2,3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)(?!\d)")

_TYPE_KEYWORDS = [
    ("Fuel", re.compile(r"petrol|diesel|fuel|cng|litre|ltr|nozzle|pump|hpcl|bpcl|iocl|indian\s*oil|"
                        r"bharat\s*petroleum|hindustan\s*petroleum|jio-?bp|shell|nayara|ev\s*charg", re.I)),
    ("Repair", re.compile(r"repair|spare|labour|labor|denting|painting|body\s*shop|replace", re.I)),
    ("Maintenance", re.compile(r"service|servicing|maintenance|oil\s*change|engine\s*oil|tyre|tire|"
                               r"wheel\s*alignment|balancing|battery|wash", re.I)),
]


def _make_date(y, m, d):
    y = int(y)
    if y < 100:
        y += 2000
    try:
        result = date(y, int(m), int(d))
    except ValueError:
        return None
    if result.year < 2000 or result > date.today():
        return None
    return result


def _dates_in(text):
    found = []
    for m in _YMD.finditer(text):
        found.append(_make_date(*m.groups()))
    for m in _DMY.finditer(text):
        d, mo, y = m.groups()
        found.append(_make_date(y, mo, d) or _make_date(y, d, mo))
    for m in _D_MON_Y.finditer(text):
        month = _MONTHS.get(m.group(2)[:3].lower())
        if month:
            found.append(_make_date(m.group(3), month, m.group(1)))
    for m in _MON_D_Y.finditer(text):
        month = _MONTHS.get(m.group(1)[:3].lower())
        if month:
            found.append(_make_date(m.group(3), month, m.group(2)))
    return [d for d in found if d]


def _find_date(text):
    date_lines = "\n".join(l for l in text.splitlines() if re.search(r"date|dt\b|dated", l, re.I))
    for chunk in (date_lines, text):
        found = _dates_in(chunk)
        if found:
            return found[0].isoformat()
    return ""


def _find_bill_number(text):
    for m in _BILL_NO.finditer(text):
        candidate = m.group(1).strip("-/")
        if any(c.isdigit() for c in candidate) and not _DMY.fullmatch(candidate):
            return candidate[:64]
    return ""


def _numbers(line):
    line = _DMY.sub(" ", _YMD.sub(" ", line))
    values = []
    for raw in _NUMBER.findall(line):
        digits = raw.replace(",", "")
        if "." not in digits and len(digits) > 7:  # phone numbers, ids
            continue
        values.append(float(digits))
    return values


def _find_amount(text):
    lines = text.splitlines()
    for pattern in _AMOUNT_TIERS:
        values = []
        for i, line in enumerate(lines):
            if not pattern.search(line):
                continue
            nums = _numbers(line)
            if not nums and i + 1 < len(lines):  # value printed on the next line
                nums = _numbers(lines[i + 1])
            values.extend(v for v in nums if 0 < v < 1_000_000)
        if values:
            return round(max(values), 2)
    return 0.0


def _guess_type(text):
    for bill_type, pattern in _TYPE_KEYWORDS:
        if pattern.search(text):
            return bill_type
    return ""


def parse_text(text):
    return {
        "bill_number": _find_bill_number(text),
        "bill_date": _find_date(text),
        "amount": _find_amount(text),
        "bill_type": _guess_type(text),
    }


# --------------------------------------------------------------------------- #
# Helpers + entry point
# --------------------------------------------------------------------------- #

def _normalise_date(value):
    value = (value or "").strip()
    if not value:
        return ""
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return _find_date(value)


def _clean_amount(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(value) or value < 0:
        return 0.0
    return round(value, 2)


def extract(path):
    """Returns dict with bill_number, bill_date, amount, bill_type, source ('ai'|'ocr'|'manual'), note."""
    result = {"bill_number": "", "bill_date": "", "amount": 0.0, "bill_type": "",
              "source": "manual", "note": ""}

    if ai_ready():
        try:
            result.update(_extract_with_claude(path))
            result["source"] = "ai"
            return result
        except Exception as exc:
            log.warning("Claude extraction failed for %s: %s", os.path.basename(path), exc)
            result["note"] = f"AI reading failed ({type(exc).__name__}), used local fallback. "

    try:
        text = _ocr_text(path)
    except Exception as exc:
        log.warning("OCR failed for %s: %s", os.path.basename(path), exc)
        text = ""

    if text.strip():
        result.update(parse_text(text))
        result["source"] = "ocr"
        result["note"] += "Read with local OCR, please check the values."
    else:
        result["note"] += "Couldn't read this bill automatically, please enter the details."
    result["note"] = result["note"].strip()
    return result

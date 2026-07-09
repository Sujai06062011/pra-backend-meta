"""
lab_ocr_service.py — OCR + AI extraction pipeline for lab reports.

Flow:
  PDF/image → Google Vision OCR (all pages, batched) →
  Claude Sonnet chunked parallel extraction → merged structured values

Env vars required:
  GOOGLE_CLOUD_VISION_KEY  — Google Cloud API key (Vision API enabled)
  ANTHROPIC_API_KEY        — already set
"""

import asyncio
import base64
import json
import os
import tempfile
from typing import Optional

import httpx

VISION_API_KEY    = os.getenv("GOOGLE_CLOUD_VISION_KEY", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
VISION_URL        = "https://vision.googleapis.com/v1/images:annotate"
ANTHROPIC_URL     = "https://api.anthropic.com/v1/messages"

_CHUNK_SYSTEM = """You are a medical lab report parser.
Extract ALL test parameters and values from this lab report text chunk.
This may be a partial section of a larger report.
Return ONLY valid JSON — no markdown, no explanation.

{
  "test_name": "overall test name or null",
  "lab_name": "lab name or null",
  "report_date": "YYYY-MM-DD or null",
  "patient_name": "patient name from report or null",
  "parameters": [
    {
      "name": "exact parameter name e.g. WBC, HDL Cholesterol",
      "category": "Blood|Diabetes|Liver|Kidney|Lipids|Thyroid|Vitamins|Iron|Cardiac|Other",
      "value": numeric_value_only,
      "unit": "string e.g. cells/cu.mm, mg/dL, %",
      "ref_low": numeric_or_null,
      "ref_high": numeric_or_null,
      "status": "Normal|Low|High|Critical Low|Critical High",
      "original_text": "exact line from report"
    }
  ]
}

Rules:
- Extract EVERY parameter you can find in this chunk
- Determine status from reference range in the report when available
- If no reference range given, use standard medical ranges:
  WBC: 4000-11000 /μL | HGB Male: 13.5-17.5, Female: 12-15.5 g/dL
  Platelets: 150000-400000 /μL | HbA1c: 4.0-5.6% Normal, 5.7-6.4% Pre-diabetic
  Fasting Glucose: 70-100 mg/dL | Creatinine: 0.7-1.3 mg/dL
  ALT/SGPT: 7-56 U/L | AST/SGOT: 10-40 U/L | Total Cholesterol: <200 mg/dL
  LDL: <100 mg/dL | HDL Male: >40, Female: >50 mg/dL
  TSH: 0.4-4.0 mIU/L | Vitamin D: 30-100 ng/mL | Vitamin B12: 200-900 pg/mL
- Critical thresholds:
  WBC>30000 or <2000 | HGB<7 | Platelets<50000 | Glucose>400
  Creatinine>10 | Sodium<120 or >155 | Potassium<2.5 or >6.5
- Never invent values not present in the text
- If chunk has no lab parameters, return {"parameters": []}"""


# ─── PDF → images ──────────────────────────────────────────────────────────────

async def pdf_to_images_bytes(pdf_bytes: bytes) -> list[bytes]:
    """Convert all pages of PDF to JPEG bytes using PyMuPDF."""
    try:
        import fitz  # PyMuPDF
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(pdf_bytes)
            pdf_path = f.name
        doc = fitz.open(pdf_path)
        mat = fitz.Matrix(2, 2)  # 2x zoom for OCR quality
        pages = []
        for page in doc:
            pix = page.get_pixmap(matrix=mat)
            pages.append(pix.tobytes("jpeg"))
        doc.close()
        os.unlink(pdf_path)
        print(f"[OCR] PDF has {len(pages)} pages")
        return pages
    except Exception as e:
        print(f"[OCR] PDF to image failed: {e}")
        raise


# ─── Google Vision OCR ─────────────────────────────────────────────────────────

async def ocr_image_bytes(image_bytes: bytes) -> str:
    """OCR a single image via Google Vision API."""
    if not VISION_API_KEY:
        raise RuntimeError("GOOGLE_CLOUD_VISION_KEY not configured")

    b64 = base64.b64encode(image_bytes).decode("utf-8")
    payload = {
        "requests": [{
            "image": {"content": b64},
            "features": [{"type": "DOCUMENT_TEXT_DETECTION", "maxResults": 1}],
        }]
    }
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(f"{VISION_URL}?key={VISION_API_KEY}", json=payload)
        r.raise_for_status()
        data = r.json()

    responses = data.get("responses", [])
    if not responses:
        return ""
    return responses[0].get("fullTextAnnotation", {}).get("text", "")


async def ocr_pdf_all_pages(pdf_bytes: bytes) -> str:
    """OCR all pages of a PDF in Vision API batches (max 16 per request)."""
    if not VISION_API_KEY:
        raise RuntimeError("GOOGLE_CLOUD_VISION_KEY not configured")

    pages = await pdf_to_images_bytes(pdf_bytes)
    BATCH_SIZE = 16
    all_text_parts = []

    for batch_start in range(0, len(pages), BATCH_SIZE):
        batch = pages[batch_start:batch_start + BATCH_SIZE]
        requests = [
            {
                "image": {"content": base64.b64encode(img).decode("utf-8")},
                "features": [{"type": "DOCUMENT_TEXT_DETECTION", "maxResults": 1}],
            }
            for img in batch
        ]
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(
                f"{VISION_URL}?key={VISION_API_KEY}",
                json={"requests": requests},
            )
            r.raise_for_status()
            data = r.json()

        for resp in data.get("responses", []):
            text = resp.get("fullTextAnnotation", {}).get("text", "")
            if text:
                all_text_parts.append(text)

    return "\n\n".join(all_text_parts)


# ─── Text chunking ─────────────────────────────────────────────────────────────

def chunk_ocr_text(
    ocr_text: str,
    chunk_size: int = 10000,
    overlap: int = 500,
) -> list[str]:
    """Split OCR text into overlapping chunks so parameters near boundaries aren't missed."""
    chunks = []
    start = 0
    while start < len(ocr_text):
        end = start + chunk_size
        chunks.append(ocr_text[start:end])
        if end >= len(ocr_text):
            break
        start = end - overlap
    return chunks


# ─── Claude Sonnet extraction ──────────────────────────────────────────────────

def _parse_claude_json(raw: str) -> dict:
    """Parse Claude response to JSON, stripping markdown fences."""
    text = raw.strip()
    if text.startswith("```"):
        parts = text.split("```")
        text = parts[1] if len(parts) > 1 else text
        if text.startswith("json"):
            text = text[4:]
    text = text.strip()
    return json.loads(text)


async def _call_claude(chunk: str) -> dict:
    """Single Claude Sonnet call for one chunk. Returns parsed dict."""
    async with httpx.AsyncClient(timeout=90) as client:
        r = await client.post(
            ANTHROPIC_URL,
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": 4096,
                "system": _CHUNK_SYSTEM,
                "messages": [{
                    "role": "user",
                    "content": f"Extract all lab values from this report section:\n\n{chunk}",
                }],
            },
        )
        r.raise_for_status()
    return _parse_claude_json(r.json()["content"][0]["text"])


async def extract_from_single_chunk(chunk: str, chunk_idx: int) -> Optional[dict]:
    """Extract lab values from one chunk. Returns None on failure (never raises)."""
    try:
        result = await _call_claude(chunk)
        n = len(result.get("parameters", []))
        print(f"[OCR] chunk {chunk_idx}: {n} parameters")
        return result
    except Exception as e:
        print(f"[OCR] chunk {chunk_idx} failed (skipping): {e}")
        return None


# ─── Merge ─────────────────────────────────────────────────────────────────────

def merge_chunk_results(chunk_results: list[dict]) -> dict:
    """
    Merge parameters from all chunks.
    Deduplicates by parameter name; keeps version with most complete reference range.
    """
    merged: dict = {
        "test_name": None,
        "lab_name": None,
        "report_date": None,
        "patient_name": None,
        "parameters": [],
    }

    # Metadata from first chunk that has it
    for result in chunk_results:
        for key in ("test_name", "lab_name", "report_date", "patient_name"):
            if not merged[key] and result.get(key):
                merged[key] = result[key]

    # Deduplicate parameters by name; prefer entry with full reference range
    seen: dict[str, dict] = {}
    for result in chunk_results:
        for param in result.get("parameters", []):
            name_key = param["name"].strip().lower()
            if name_key not in seen:
                seen[name_key] = param
            else:
                existing = seen[name_key]
                existing_has_ref = (
                    existing.get("ref_low") is not None
                    and existing.get("ref_high") is not None
                )
                new_has_ref = (
                    param.get("ref_low") is not None
                    and param.get("ref_high") is not None
                )
                if new_has_ref and not existing_has_ref:
                    seen[name_key] = param

    merged["parameters"] = list(seen.values())
    return merged


# ─── Summary ───────────────────────────────────────────────────────────────────

def generate_result_summary(parameters: list) -> str:
    """Generate a plain-English 1–2 line summary from the merged parameter list."""
    if not parameters:
        return ""

    critical = [p for p in parameters if "Critical" in (p.get("status") or "")]
    abnormal = [p for p in parameters if p.get("status") in ("High", "Low")]
    normal_count = len(parameters) - len(critical) - len(abnormal)

    parts = []
    if critical:
        names = ", ".join(p["name"] for p in critical[:3])
        suffix = f" + {len(critical) - 3} more" if len(critical) > 3 else ""
        parts.append(f"CRITICAL: {names}{suffix}")
    if abnormal:
        names = ", ".join(p["name"] for p in abnormal[:4])
        suffix = f" + {len(abnormal) - 4} more" if len(abnormal) > 4 else ""
        parts.append(f"{len(abnormal)} value(s) need attention: {names}{suffix}")
    parts.append(f"{normal_count} of {len(parameters)} parameters within normal range.")

    return " | ".join(parts)


# ─── Main extraction entry point ───────────────────────────────────────────────

async def extract_lab_values(ocr_text: str) -> dict:
    """
    Full extraction: chunk → parallel Claude Sonnet calls → merge → summary.
    No text truncation. Short reports use a single call.
    """
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY not configured")

    if len(ocr_text) <= 10000:
        result = await extract_from_single_chunk(ocr_text, 0) or {"parameters": []}
    else:
        chunks = chunk_ocr_text(ocr_text)
        print(f"[OCR] splitting into {len(chunks)} chunks for parallel extraction")
        raw_results = await asyncio.gather(
            *[extract_from_single_chunk(chunk, idx) for idx, chunk in enumerate(chunks)],
            return_exceptions=True,
        )
        valid = [r for r in raw_results if isinstance(r, dict)]
        result = merge_chunk_results(valid)

    result["result_summary"] = generate_result_summary(result.get("parameters", []))
    return result


# ─── Status classification ─────────────────────────────────────────────────────

def determine_report_status(parameters: list) -> str:
    """Derive report-level status from individual parameter statuses."""
    statuses = {p.get("status", "Normal") for p in parameters}
    if "Critical Low" in statuses or "Critical High" in statuses:
        return "Critical"
    if "High" in statuses or "Low" in statuses:
        return "Needs Review"
    return "Pending Review"


# ─── Pipeline ──────────────────────────────────────────────────────────────────

async def run_ocr_pipeline(
    file_bytes: bytes,
    mime_type: str,
) -> tuple[Optional[str], Optional[dict], str]:
    """
    Full pipeline: file → OCR → chunked extraction → merge.
    Returns (ocr_raw_text, extracted_dict, auto_status).
    Never raises — returns (None, None, 'Pending Review') on any failure.
    """
    ocr_text  = None
    extracted = None
    auto_status = "Pending Review"

    try:
        if "pdf" in mime_type.lower():
            ocr_text = await ocr_pdf_all_pages(file_bytes)
        else:
            ocr_text = await ocr_image_bytes(file_bytes)
        print(f"[OCR] extracted {len(ocr_text)} chars")

        if ocr_text:
            extracted   = await extract_lab_values(ocr_text)
            params      = extracted.get("parameters", [])
            auto_status = determine_report_status(params)
            print(f"[OCR] {len(params)} parameters merged, status={auto_status}")

    except Exception as e:
        print(f"[OCR] pipeline error (non-fatal): {e}")

    return ocr_text, extracted, auto_status

"""
lab_ocr_service.py — OCR + AI extraction pipeline for lab reports.

Flow:
  PDF/image file → Google Vision OCR → Claude extraction → structured values

Env vars required:
  GOOGLE_CLOUD_VISION_KEY  — Google Cloud API key (Vision API enabled)
  ANTHROPIC_API_KEY        — already set
"""

import base64
import json
import os
import tempfile
from typing import Optional

import httpx

VISION_API_KEY    = os.getenv("GOOGLE_CLOUD_VISION_KEY", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
VISION_URL        = "https://vision.googleapis.com/v1/images:annotate"

_EXTRACT_SYSTEM = """You are a medical lab report parser.
Extract all test parameters and values from this lab report text.
Return ONLY valid JSON — no markdown, no explanation, just the JSON object.

Format:
{
  "test_name": "overall test name e.g. CBC, LFT, Thyroid Profile",
  "lab_name": "lab name or null",
  "report_date": "YYYY-MM-DD or null",
  "patient_name": "patient name from report or null",
  "parameters": [
    {
      "name": "parameter name e.g. WBC",
      "category": "Blood|Diabetes|Liver|Kidney|Lipids|Thyroid|Cardiac|Other",
      "value": 14200,
      "unit": "/μL",
      "ref_low": 4000,
      "ref_high": 11000,
      "status": "Normal|Low|High|Critical Low|Critical High",
      "original_text": "exact line from report"
    }
  ],
  "result_summary": "2-line plain English summary of key findings"
}

Rules:
- Extract ALL parameters you can find
- Determine status from reference range in the report when available
- If no reference range provided, use standard adult medical reference ranges
- Never invent values not present in the text
- Critical thresholds: WBC>30000 or <2000, HGB<7, PLT<50000, Glucose>400,
  Creatinine>10, Sodium<120 or >155, Potassium<2.5 or >6.5"""


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


async def ocr_image_bytes(image_bytes: bytes) -> str:
    """Send image bytes to Google Vision API, return raw extracted text."""
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
    full_ann = responses[0].get("fullTextAnnotation", {})
    return full_ann.get("text", "")


async def ocr_pdf_all_pages(pdf_bytes: bytes) -> str:
    """OCR all pages of a PDF in batch and return concatenated text."""
    if not VISION_API_KEY:
        raise RuntimeError("GOOGLE_CLOUD_VISION_KEY not configured")

    pages = await pdf_to_images_bytes(pdf_bytes)

    # Vision API batch: up to 16 images per request
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
            r = await client.post(f"{VISION_URL}?key={VISION_API_KEY}", json={"requests": requests})
            r.raise_for_status()
            data = r.json()

        for resp in data.get("responses", []):
            text = resp.get("fullTextAnnotation", {}).get("text", "")
            if text:
                all_text_parts.append(text)

    return "\n\n".join(all_text_parts)


async def extract_lab_values(ocr_text: str) -> dict:
    """Send OCR text to Claude Haiku and return structured lab values dict."""
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY not configured")

    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 4096,
                "system": _EXTRACT_SYSTEM,
                "messages": [{
                    "role": "user",
                    "content": f"Extract values from this lab report:\n\n{ocr_text[:12000]}",
                }],
            },
        )
        r.raise_for_status()
        content = r.json()["content"][0]["text"].strip()

    # Strip markdown fences if present
    if content.startswith("```"):
        content = content.split("```")[1]
        if content.startswith("json"):
            content = content[4:]

    # Repair truncated JSON: close any open array then object
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        # Find the last complete parameter entry and close the structure
        last_brace = content.rfind("},")
        if last_brace != -1:
            content = content[:last_brace + 1] + "]}"
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            # Last resort: close open array+object
            content = content.rstrip().rstrip(",") + "]}"
            return json.loads(content)


def determine_report_status(parameters: list) -> str:
    """Auto-set report status from extracted parameter statuses."""
    statuses = {p.get("status", "Normal") for p in parameters}
    if "Critical Low" in statuses or "Critical High" in statuses:
        return "Critical"
    if "High" in statuses or "Low" in statuses:
        return "Needs Review"
    return "Pending Review"


async def run_ocr_pipeline(
    file_bytes: bytes,
    mime_type: str,
) -> tuple[Optional[str], Optional[dict], str]:
    """
    Full pipeline: file → OCR → extraction.
    Returns (ocr_raw_text, extracted_dict, auto_status).
    Never raises — returns (None, None, 'Pending Review') on any failure.
    """
    ocr_text = None
    extracted = None
    auto_status = "Pending Review"

    try:
        # Step 1 + 2: OCR (multi-page for PDFs)
        if "pdf" in mime_type.lower():
            ocr_text = await ocr_pdf_all_pages(file_bytes)
        else:
            ocr_text = await ocr_image_bytes(file_bytes)
        print(f"[OCR] extracted {len(ocr_text)} chars")

        if ocr_text:
            # Step 3: AI extraction
            extracted = await extract_lab_values(ocr_text)
            params = extracted.get("parameters", [])
            auto_status = determine_report_status(params)
            print(f"[OCR] {len(params)} parameters, status={auto_status}")

    except Exception as e:
        print(f"[OCR] pipeline error (non-fatal): {e}")

    return ocr_text, extracted, auto_status

"""
lab_reports_router.py — Lab Reports endpoints.

Mounted at /api/lab in main.py:
  app.include_router(lab_reports_router.router, prefix="/api/lab", tags=["lab"])

All queries filter by doctor_id — no cross-doctor leakage.
"""

import os
import uuid
from datetime import date, datetime, timedelta
from typing import Optional

import httpx
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from database import supabase

router = APIRouter()

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
META_ACCESS_TOKEN = os.getenv("META_ACCESS_TOKEN", "")
_BUCKET = "lab-reports"

ALLOWED_MIME = {
    "application/pdf", "image/jpeg", "image/jpg", "image/png",
    "image/heic", "image/heif", "image/webp",
}


# ─── helpers ────────────────────────────────────────────────────────────────

def _ext(mime: str) -> str:
    return {
        "application/pdf": "pdf", "image/jpeg": "jpg", "image/jpg": "jpg",
        "image/png": "png", "image/heic": "heic", "image/heif": "heif",
        "image/webp": "webp",
    }.get(mime, "bin")


async def _upload_to_storage(doctor_id: str, patient_id: str,
                              file_bytes: bytes, mime: str) -> str:
    """Upload file to Supabase Storage, return public URL."""
    ext = _ext(mime)
    path = f"{doctor_id}/{patient_id}/{uuid.uuid4()}.{ext}"
    supabase.storage.from_(_BUCKET).upload(
        path, file_bytes, {"content-type": mime, "upsert": "true"}
    )
    return supabase.storage.from_(_BUCKET).get_public_url(path)


def _trend_direction(history: list) -> tuple[str, str]:
    """Given list of {date, value, status} sorted oldest→newest, return (direction, comment)."""
    if len(history) < 2:
        return "stable", "First report — no trend yet"
    vals = [h["value"] for h in history[-3:]]
    if len(vals) >= 2:
        if all(vals[i] < vals[i + 1] for i in range(len(vals) - 1)):
            return "rising", "Consistently rising — monitor closely ↑"
        if all(vals[i] > vals[i + 1] for i in range(len(vals) - 1)):
            return "falling", "Improving over recent reports ↓"
    last_status = history[-1].get("status", "Normal")
    prev_status = history[-2].get("status", "Normal")
    if prev_status != "Normal" and last_status == "Normal":
        return "stable", "Normalised from last report ✅"
    return "stable", "Stable →"


# ─── POST /upload ────────────────────────────────────────────────────────────

@router.post("/upload")
async def upload_lab_report(
    file: UploadFile = File(...),
    patient_id: str = Form(...),
    doctor_id: str = Form(...),
    test_name: str = Form(...),
    lab_name: str = Form(""),
    report_date: str = Form(""),
    order_id: str = Form(""),
    report_source: str = Form("dashboard_upload"),
):
    mime = file.content_type or "application/octet-stream"
    if mime not in ALLOWED_MIME:
        raise HTTPException(400, f"Unsupported file type: {mime}")

    file_bytes = await file.read()

    # Upload original to storage
    try:
        public_url = await _upload_to_storage(doctor_id, patient_id, file_bytes, mime)
    except Exception as e:
        raise HTTPException(500, f"Storage upload failed: {e}")

    # OCR + extraction (non-blocking on failure)
    from lab_ocr_service import run_ocr_pipeline
    ocr_text, extracted, auto_status = await run_ocr_pipeline(file_bytes, mime)

    # Resolve fields from extraction if not provided
    resolved_test   = test_name or (extracted or {}).get("test_name", "Lab Report")
    resolved_lab    = lab_name  or (extracted or {}).get("lab_name") or ""
    resolved_date   = report_date or (extracted or {}).get("report_date") or None
    result_summary  = (extracted or {}).get("result_summary") or ""
    extracted_vals  = None
    if extracted and extracted.get("parameters"):
        extracted_vals = {
            p["name"]: {
                "value": p.get("value"), "unit": p.get("unit"),
                "ref_low": p.get("ref_low"), "ref_high": p.get("ref_high"),
                "status": p.get("status", "Normal"),
            }
            for p in extracted["parameters"]
        }

    is_pdf = "pdf" in mime.lower()

    row = {
        "patient_id":      patient_id,
        "doctor_id":       doctor_id,
        "order_id":        order_id or None,
        "report_name":     resolved_test,
        "test_name":       resolved_test,
        "test_category":   (extracted or {}).get("parameters", [{}])[0].get("category") if extracted else None,
        "lab_name":        resolved_lab or None,
        "report_date":     resolved_date,
        "received_date":   date.today().isoformat(),
        "report_source":   report_source,
        "pdf_url":         public_url if is_pdf else None,
        "image_url":       public_url if not is_pdf else None,
        "status":          auto_status,
        "result_summary":  result_summary or None,
        "ocr_raw_text":    ocr_text,
        "extracted_values": extracted_vals,
    }

    res = supabase.table("lab_reports").insert(row).execute()
    if not res.data:
        raise HTTPException(500, "Failed to save lab report")
    report_id = res.data[0]["id"]

    # Save individual parameter rows
    if extracted and extracted.get("parameters"):
        param_rows = [
            {
                "report_id":          report_id,
                "patient_id":         patient_id,
                "parameter_name":     p["name"],
                "parameter_category": p.get("category"),
                "value":              p["value"],
                "unit":               p.get("unit"),
                "ref_low":            p.get("ref_low"),
                "ref_high":           p.get("ref_high"),
                "status":             p.get("status", "Normal"),
                "report_date":        resolved_date or date.today().isoformat(),
            }
            for p in extracted["parameters"]
            if p.get("value") is not None
        ]
        if param_rows:
            supabase.table("lab_report_values").insert(param_rows).execute()

    return {
        "report_id":       report_id,
        "status":          auto_status,
        "extracted_values": extracted_vals,
        "result_summary":  result_summary,
        "ocr_chars":       len(ocr_text) if ocr_text else 0,
    }


# ─── POST /upload-photo ──────────────────────────────────────────────────────

@router.post("/upload-photo")
async def upload_photo_report(
    file: UploadFile = File(...),
    patient_id: str = Form(...),
    doctor_id: str = Form(...),
    test_name: str = Form(""),
    lab_name: str = Form(""),
    report_date: str = Form(""),
    order_id: str = Form(""),
):
    mime = file.content_type or "image/jpeg"
    file_bytes = await file.read()

    try:
        public_url = await _upload_to_storage(doctor_id, patient_id, file_bytes, mime)
    except Exception as e:
        raise HTTPException(500, f"Storage upload failed: {e}")

    from lab_ocr_service import run_ocr_pipeline
    ocr_text, extracted, auto_status = await run_ocr_pipeline(file_bytes, mime)

    resolved_test  = test_name or (extracted or {}).get("test_name", "Lab Report")
    resolved_lab   = lab_name  or (extracted or {}).get("lab_name") or ""
    resolved_date  = report_date or (extracted or {}).get("report_date") or None
    result_summary = (extracted or {}).get("result_summary") or ""
    extracted_vals = None
    if extracted and extracted.get("parameters"):
        extracted_vals = {
            p["name"]: {
                "value": p.get("value"), "unit": p.get("unit"),
                "ref_low": p.get("ref_low"), "ref_high": p.get("ref_high"),
                "status": p.get("status", "Normal"),
            }
            for p in extracted["parameters"]
        }

    row = {
        "patient_id": patient_id, "doctor_id": doctor_id,
        "order_id": order_id or None,
        "report_name": resolved_test, "test_name": resolved_test,
        "lab_name": resolved_lab or None,
        "report_date": resolved_date,
        "received_date": date.today().isoformat(),
        "report_source": "photo_ocr",
        "image_url": public_url, "status": auto_status,
        "result_summary": result_summary or None,
        "ocr_raw_text": ocr_text,
        "extracted_values": extracted_vals,
    }
    res = supabase.table("lab_reports").insert(row).execute()
    if not res.data:
        raise HTTPException(500, "Failed to save lab report")
    report_id = res.data[0]["id"]

    if extracted and extracted.get("parameters"):
        param_rows = [
            {
                "report_id": report_id, "patient_id": patient_id,
                "parameter_name": p["name"], "parameter_category": p.get("category"),
                "value": p["value"], "unit": p.get("unit"),
                "ref_low": p.get("ref_low"), "ref_high": p.get("ref_high"),
                "status": p.get("status", "Normal"),
                "report_date": resolved_date or date.today().isoformat(),
            }
            for p in extracted["parameters"] if p.get("value") is not None
        ]
        if param_rows:
            supabase.table("lab_report_values").insert(param_rows).execute()

    return {"report_id": report_id, "status": auto_status,
            "extracted_values": extracted_vals, "result_summary": result_summary}


# ─── GET /reports ────────────────────────────────────────────────────────────

@router.get("/reports")
async def get_reports(
    doctor_id: str,
    status: Optional[str] = None,
    source: Optional[str] = None,
    patient_id: Optional[str] = None,
    days: int = 30,
):
    since = (date.today() - timedelta(days=days)).isoformat()

    q = supabase.table("lab_reports") \
        .select("id, patient_id, doctor_id, test_name, lab_name, received_date, "
                "result_summary, status, report_source, pdf_url, image_url, "
                "extracted_values, created_at, patients(name, age, gender)") \
        .eq("doctor_id", doctor_id) \
        .gte("created_at", since) \
        .order("created_at", desc=True)

    if status:
        q = q.eq("status", status)
    if source:
        q = q.eq("report_source", source)
    if patient_id:
        q = q.eq("patient_id", patient_id)

    res = q.limit(200).execute()
    rows = res.data or []

    summary = {"critical": 0, "needs_review": 0, "pending_review": 0, "reviewed": 0}
    for r in rows:
        s = r.get("status", "")
        if s == "Critical":         summary["critical"] += 1
        elif s == "Needs Review":   summary["needs_review"] += 1
        elif s == "Pending Review": summary["pending_review"] += 1
        elif s == "Reviewed":       summary["reviewed"] += 1

    reports = []
    for r in rows:
        pat = r.get("patients") or {}
        ev = r.get("extracted_values") or {}
        abnormal = sum(1 for v in ev.values() if v.get("status") not in ("Normal", None))
        critical = sum(1 for v in ev.values() if "Critical" in (v.get("status") or ""))

        # Check if patient has previous reports for trend indicator
        prev = supabase.table("lab_report_values") \
            .select("id", count="exact") \
            .eq("patient_id", r["patient_id"]) \
            .neq("report_id", r["id"]) \
            .limit(1).execute()
        has_trend = bool(prev.data)

        reports.append({
            "id":            r["id"],
            "patient_id":    r["patient_id"],
            "patient_name":  pat.get("name", ""),
            "patient_age":   pat.get("age"),
            "patient_gender": pat.get("gender"),
            "test_name":     r.get("test_name") or r.get("report_name", ""),
            "lab_name":      r.get("lab_name", ""),
            "received_date": str(r.get("received_date") or r.get("created_at", ""))[:10],
            "result_summary": r.get("result_summary", ""),
            "status":        r.get("status", "Pending Review"),
            "report_source": r.get("report_source", "dashboard_upload"),
            "has_pdf":       bool(r.get("pdf_url")),
            "has_image":     bool(r.get("image_url")),
            "has_trend":     has_trend,
            "abnormal_count": abnormal,
            "critical_count": critical,
        })

    return {"summary": summary, "reports": reports}


# ─── GET /reports/{report_id} ────────────────────────────────────────────────

@router.get("/reports/{report_id}")
async def get_report_detail(report_id: str, doctor_id: str):
    res = supabase.table("lab_reports") \
        .select("*, patients(name, age, gender, mobile, language)") \
        .eq("id", report_id) \
        .eq("doctor_id", doctor_id) \
        .limit(1).execute()

    if not res.data:
        raise HTTPException(404, "Report not found")
    r = res.data[0]

    # Extracted parameter rows
    pvals = supabase.table("lab_report_values") \
        .select("*") \
        .eq("report_id", report_id) \
        .order("parameter_name").execute()
    param_rows = pvals.data or []

    # Trend: for each parameter, fetch history across all reports for this patient
    trend = []
    seen_params = set()
    for p in param_rows:
        pname = p["parameter_name"]
        if pname in seen_params:
            continue
        seen_params.add(pname)

        hist_res = supabase.table("lab_report_values") \
            .select("value, status, report_date") \
            .eq("patient_id", r["patient_id"]) \
            .eq("parameter_name", pname) \
            .order("report_date").execute()
        history = [
            {"date": str(h["report_date"])[:10],
             "value": float(h["value"]),
             "status": h.get("status", "Normal")}
            for h in (hist_res.data or [])
        ]
        if len(history) >= 2:
            direction, comment = _trend_direction(history)
            trend.append({
                "parameter_name":   pname,
                "unit":             p.get("unit"),
                "ref_low":          float(p["ref_low"]) if p.get("ref_low") is not None else None,
                "ref_high":         float(p["ref_high"]) if p.get("ref_high") is not None else None,
                "history":          history,
                "trend_direction":  direction,
                "trend_comment":    comment,
            })

    # Linked order
    order_info = None
    if r.get("order_id"):
        ord_res = supabase.table("lab_orders") \
            .select("*, doctors(name)") \
            .eq("id", r["order_id"]).limit(1).execute()
        if ord_res.data:
            o = ord_res.data[0]
            order_info = {
                "ordered_by":  (o.get("doctors") or {}).get("name", ""),
                "ordered_at":  str(o.get("ordered_at", ""))[:10],
                "priority":    o.get("priority", "Routine"),
            }

    return {
        "report":            r,
        "patient":           r.get("patients") or {},
        "order":             order_info,
        "extracted_values":  param_rows,
        "trend":             trend,
    }


# ─── PATCH /reports/{report_id} ──────────────────────────────────────────────

class ReportUpdateBody(BaseModel):
    status: Optional[str] = None
    doctor_notes: Optional[str] = None
    result_summary: Optional[str] = None


@router.patch("/reports/{report_id}")
async def update_report(report_id: str, body: ReportUpdateBody, doctor_id: str):
    updates: dict = {"updated_at": datetime.utcnow().isoformat()}
    if body.status is not None:
        updates["status"] = body.status
    if body.doctor_notes is not None:
        updates["doctor_notes"] = body.doctor_notes
    if body.result_summary is not None:
        updates["result_summary"] = body.result_summary

    res = supabase.table("lab_reports") \
        .update(updates) \
        .eq("id", report_id) \
        .eq("doctor_id", doctor_id) \
        .execute()
    if not res.data:
        raise HTTPException(404, "Report not found or not yours")
    return {"ok": True, "report": res.data[0]}


# ─── POST /reports/{report_id}/send-patient ──────────────────────────────────

@router.post("/reports/{report_id}/send-patient")
async def send_report_to_patient(report_id: str, doctor_id: str):
    res = supabase.table("lab_reports") \
        .select("*, patients(name, mobile, language), doctors(name)") \
        .eq("id", report_id) \
        .eq("doctor_id", doctor_id) \
        .limit(1).execute()
    if not res.data:
        raise HTTPException(404, "Report not found")
    r = res.data[0]

    pat = r.get("patients") or {}
    pat_name  = pat.get("name", "Patient")
    pat_mobile = pat.get("mobile", "")
    lang      = (pat.get("language") or "english").lower()
    doc_name  = (r.get("doctors") or {}).get("name", "Doctor")
    test_name = r.get("test_name") or r.get("report_name", "Lab Report")
    lab_name  = r.get("lab_name") or ""
    report_date_str = str(r.get("report_date") or r.get("received_date") or "")[:10]
    pdf_url   = r.get("pdf_url") or ""
    doctor_notes = r.get("doctor_notes") or ""

    # Build parameter summary
    pvals = supabase.table("lab_report_values") \
        .select("parameter_name, value, unit, ref_low, ref_high, status") \
        .eq("report_id", report_id) \
        .order("parameter_name").execute()
    params = pvals.data or []

    normal_params   = [p for p in params if p.get("status") == "Normal"]
    abnormal_params = [p for p in params if p.get("status") not in ("Normal",) and p.get("status")]
    critical_params = [p for p in params if "Critical" in (p.get("status") or "")]

    def _status_icon(s: str) -> str:
        if "Critical" in s: return "🔴"
        if s in ("High", "Low"): return "⚠️"
        return "✅"

    findings_lines = []
    for p in critical_params:
        ref = ""
        if p.get("ref_low") is not None and p.get("ref_high") is not None:
            ref = f" — normal is {p['ref_low']}-{p['ref_high']}"
        findings_lines.append(
            f"🔴 {p['parameter_name']}: {p['value']} {p.get('unit','')}{ref} ({p['status']})"
        )
    for p in [x for x in abnormal_params if "Critical" not in (x.get("status") or "")]:
        findings_lines.append(
            f"⚠️ {p['parameter_name']}: {p['value']} {p.get('unit','')}"
        )
    if normal_params:
        names = ", ".join(p["parameter_name"] for p in normal_params[:5])
        if len(normal_params) > 5:
            names += f" + {len(normal_params)-5} more"
        findings_lines.append(f"✅ Normal: {names}")

    critical_warning = (
        "\n\n🚨 *Please contact the clinic immediately regarding critical values.*"
        if critical_params else ""
    )

    report_link = f"\n\nFull report: {pdf_url}" if pdf_url else ""
    notes_section = f"\n\nDr. {doc_name}'s advice:\n{doctor_notes}" if doctor_notes else ""

    lab_part = f" from {lab_name}" if lab_name else ""
    date_part = f" ({report_date_str})" if report_date_str else ""

    msg = (
        f"Dear {pat_name},\n\n"
        f"Dr. {doc_name} has reviewed your *{test_name}* report{lab_part}{date_part}.\n\n"
        f"📊 *Results Summary:*\n"
        f"✅ Normal values: {len(normal_params)}\n"
        f"⚠️ Needs attention: {len([p for p in abnormal_params if 'Critical' not in (p.get('status') or '')])}\n"
        f"🔴 Critical: {len(critical_params)}\n\n"
        f"*Key findings:*\n" + "\n".join(findings_lines) +
        notes_section + critical_warning + report_link +
        "\n\nReply MENU for options or book a follow-up appointment."
    )

    # Send via Meta WhatsApp
    from main import send_meta_text
    mobile = pat_mobile if pat_mobile.startswith("91") else f"91{pat_mobile}"
    await send_meta_text(mobile, msg)

    # Mark sent
    supabase.table("lab_reports") \
        .update({"whatsapp_sent_to_patient": True, "updated_at": datetime.utcnow().isoformat()}) \
        .eq("id", report_id).execute()

    return {"ok": True, "sent_to": pat_name, "mobile": mobile}


# ─── POST /orders ────────────────────────────────────────────────────────────

class OrderBody(BaseModel):
    patient_id: str
    doctor_id: str
    prescription_id: Optional[str] = None
    test_name: str
    test_category: Optional[str] = None
    priority: str = "Routine"
    lab_type: str = "external"
    lab_name: Optional[str] = None
    notes: Optional[str] = None


@router.post("/orders")
async def create_order(body: OrderBody):
    row = {
        "patient_id":      body.patient_id,
        "doctor_id":       body.doctor_id,
        "prescription_id": body.prescription_id or None,
        "test_name":       body.test_name,
        "test_category":   body.test_category,
        "priority":        body.priority,
        "lab_type":        body.lab_type,
        "lab_name":        body.lab_name,
        "notes":           body.notes,
        "status":          "Ordered",
    }
    res = supabase.table("lab_orders").insert(row).execute()
    if not res.data:
        raise HTTPException(500, "Failed to create order")
    return res.data[0]


# ─── GET /orders ─────────────────────────────────────────────────────────────

@router.get("/orders")
async def get_orders(
    doctor_id: str,
    status: Optional[str] = None,
    patient_id: Optional[str] = None,
):
    q = supabase.table("lab_orders") \
        .select("*, patients(name, age, gender, patient_code)") \
        .eq("doctor_id", doctor_id) \
        .order("ordered_at", desc=True)
    if status:
        q = q.eq("status", status)
    if patient_id:
        q = q.eq("patient_id", patient_id)

    res = q.limit(100).execute()
    return {"orders": res.data or []}


# ─── PATCH /orders/{order_id} ────────────────────────────────────────────────

class OrderUpdateBody(BaseModel):
    status: str
    notes: Optional[str] = None


@router.patch("/orders/{order_id}")
async def update_order(order_id: str, body: OrderUpdateBody, doctor_id: str):
    ts_field = {
        "Sample Collected": "collected_at",
        "Processing":       "processing_at",
        "Ready":            "ready_at",
        "Delivered":        "delivered_at",
    }
    updates: dict = {"status": body.status}
    if body.status in ts_field:
        updates[ts_field[body.status]] = datetime.utcnow().isoformat()
    if body.notes:
        updates["notes"] = body.notes

    res = supabase.table("lab_orders") \
        .update(updates) \
        .eq("id", order_id) \
        .eq("doctor_id", doctor_id) \
        .execute()
    if not res.data:
        raise HTTPException(404, "Order not found")
    return res.data[0]


# ─── GET /trends/{patient_id} ────────────────────────────────────────────────

@router.get("/trends/{patient_id}")
async def get_trends(patient_id: str, doctor_id: str,
                     parameter_name: Optional[str] = None):
    q = supabase.table("lab_report_values") \
        .select("parameter_name, parameter_category, value, unit, "
                "ref_low, ref_high, status, report_date") \
        .eq("patient_id", patient_id) \
        .order("report_date")

    if parameter_name:
        q = q.eq("parameter_name", parameter_name)

    res = q.execute()
    rows = res.data or []

    # Group by parameter
    by_param: dict = {}
    for r in rows:
        pname = r["parameter_name"]
        if pname not in by_param:
            by_param[pname] = {
                "parameter_name": pname,
                "category":       r.get("parameter_category"),
                "unit":           r.get("unit"),
                "ref_low":        float(r["ref_low"]) if r.get("ref_low") is not None else None,
                "ref_high":       float(r["ref_high"]) if r.get("ref_high") is not None else None,
                "history":        [],
            }
        by_param[pname]["history"].append({
            "date":   str(r["report_date"])[:10],
            "value":  float(r["value"]),
            "status": r.get("status", "Normal"),
        })

    results = []
    for pname, data in by_param.items():
        direction, comment = _trend_direction(data["history"])
        results.append({**data, "trend_direction": direction, "trend_comment": comment})

    return {"patient_id": patient_id, "trends": results}


# ─── POST /whatsapp-report ───────────────────────────────────────────────────

class WhatsAppReportBody(BaseModel):
    mobile: str
    document_url: str
    document_name: str
    mime_type: str
    test_category: Optional[str] = None


@router.post("/whatsapp-report")
async def receive_whatsapp_report(body: WhatsAppReportBody):
    """Called from whatsapp_handler when patient sends a document."""
    # Find patient
    clean_mobile = body.mobile.lstrip("91") if body.mobile.startswith("91") else body.mobile
    with_prefix  = f"91{clean_mobile}"

    pat_res = supabase.table("patients") \
        .select("id, name, doctor_id") \
        .or_(f"mobile.eq.{body.mobile},mobile.eq.{clean_mobile},"
             f"mobile.eq.{with_prefix},whatsapp_number.eq.{body.mobile},"
             f"whatsapp_number.eq.{with_prefix}") \
        .limit(1).execute()

    if not pat_res.data:
        return {"ok": False, "reason": "patient_not_found"}

    patient = pat_res.data[0]
    patient_id = patient["id"]
    doctor_id  = patient["doctor_id"]

    # Download document — Meta URLs require Bearer auth
    try:
        headers = {}
        if META_ACCESS_TOKEN and "fbsbx.com" in body.document_url:
            headers["Authorization"] = f"Bearer {META_ACCESS_TOKEN}"
        async with httpx.AsyncClient(timeout=30) as client:
            dl = await client.get(body.document_url, headers=headers)
            dl.raise_for_status()
            file_bytes = dl.content
    except Exception as e:
        print(f"[LAB WA] download failed: {e}")
        return {"ok": False, "reason": "download_failed"}

    # Upload to storage
    try:
        public_url = await _upload_to_storage(doctor_id, patient_id,
                                              file_bytes, body.mime_type)
    except Exception as e:
        print(f"[LAB WA] storage upload failed: {e}")
        return {"ok": False, "reason": "storage_failed"}

    # OCR + extraction
    from lab_ocr_service import run_ocr_pipeline
    ocr_text, extracted, auto_status = await run_ocr_pipeline(file_bytes, body.mime_type)

    is_pdf = "pdf" in body.mime_type.lower()
    resolved_test  = (extracted or {}).get("test_name") or body.document_name or "Lab Report"
    result_summary = (extracted or {}).get("result_summary") or ""
    extracted_vals = None
    if extracted and extracted.get("parameters"):
        extracted_vals = {
            p["name"]: {
                "value": p.get("value"), "unit": p.get("unit"),
                "ref_low": p.get("ref_low"), "ref_high": p.get("ref_high"),
                "status": p.get("status", "Normal"),
            }
            for p in extracted["parameters"]
        }

    row = {
        "patient_id":       patient_id,
        "doctor_id":        doctor_id,
        "report_name":      resolved_test,
        "test_name":        resolved_test,
        "test_category":    body.test_category,
        "received_date":    date.today().isoformat(),
        "report_source":    "whatsapp_patient",
        "pdf_url":          public_url if is_pdf else None,
        "image_url":        public_url if not is_pdf else None,
        "status":           auto_status,
        "result_summary":   result_summary or None,
        "ocr_raw_text":     ocr_text,
        "extracted_values": extracted_vals,
    }
    res = supabase.table("lab_reports").insert(row).execute()
    if not res.data:
        return {"ok": False, "reason": "db_insert_failed"}

    report_id = res.data[0]["id"]

    if extracted and extracted.get("parameters"):
        resolved_date = (extracted or {}).get("report_date") or date.today().isoformat()
        param_rows = [
            {
                "report_id": report_id, "patient_id": patient_id,
                "parameter_name": p["name"], "parameter_category": p.get("category"),
                "value": p["value"], "unit": p.get("unit"),
                "ref_low": p.get("ref_low"), "ref_high": p.get("ref_high"),
                "status": p.get("status", "Normal"),
                "report_date": resolved_date,
            }
            for p in extracted["parameters"] if p.get("value") is not None
        ]
        if param_rows:
            supabase.table("lab_report_values").insert(param_rows).execute()

    return {
        "ok":        True,
        "report_id": report_id,
        "status":    auto_status,
        "patient":   patient.get("name", ""),
    }

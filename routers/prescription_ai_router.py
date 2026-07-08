"""
prescription_ai_router.py — AI-powered prescription endpoints.

  POST /prescriptions/transcribe   — audio file → transcript (Sarvam saarika:v2.5)
  POST /prescriptions/extract      — transcript → structured JSON (Claude Haiku)
  POST /prescriptions/generate-pdf — prescription data → PDF bytes (ReportLab)
"""

import io, json, os, uuid
from datetime import datetime

import httpx
import pytz
from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import Response

router = APIRouter()

SARVAM_API_KEY    = os.getenv("SARVAM_API_KEY", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
IST               = pytz.timezone("Asia/Kolkata")


# ─── TRANSCRIBE ──────────────────────────────────────────────────────────────

_SARVAM_BATCH_BASE = "https://api.sarvam.ai/speech-to-text/job/v1"
_POLL_INTERVAL_S   = 3    # seconds between status polls
_MAX_POLLS         = 80   # 80 × 3s = 4 min max wait


@router.post("/prescriptions/transcribe")
async def transcribe_audio_endpoint(audio: UploadFile = File(...)):
    """
    Transcribe audio via Sarvam Batch STT (saaras:v3).
    Handles any duration up to 2 hours; auto-detects Tamil/English code-switching.

    Flow:
      1. Initiate batch job
      2. Get presigned upload URL
      3. PUT audio to presigned URL
      4. Start job
      5. Poll status until Completed
      6. Get presigned download URL for output JSON
      7. Fetch and return transcript
    """
    import asyncio

    if not SARVAM_API_KEY:
        raise HTTPException(status_code=503, detail="SARVAM_API_KEY not configured")

    audio_bytes = await audio.read()
    mime        = audio.content_type or "audio/webm"
    filename    = audio.filename or "recording.webm"
    print(f"[TRANSCRIBE] {len(audio_bytes)} bytes  mime={mime}")

    sarvam_headers = {"api-subscription-key": SARVAM_API_KEY, "Content-Type": "application/json"}

    try:
        # ── Step 1: Initiate job ──────────────────────────────────────────────
        async with httpx.AsyncClient(timeout=30) as client:
            init_resp = await client.post(
                _SARVAM_BATCH_BASE,
                headers=sarvam_headers,
                json={
                    "job_parameters": {
                        "language_code":    "unknown",   # auto-detect; handles en-IN + ta-IN
                        "model":            "saaras:v3",
                        "mode":             "transcribe",
                        "with_timestamps":  False,
                        "with_diarization": False,
                    }
                },
            )
        _raise_sarvam(init_resp, "initiate")
        init_data = init_resp.json()
        job_id    = init_data["job_id"]
        print(f"[TRANSCRIBE] job_id={job_id}")

        # ── Step 2: Get presigned upload URL ─────────────────────────────────
        async with httpx.AsyncClient(timeout=30) as client:
            up_resp = await client.post(
                f"{_SARVAM_BATCH_BASE}/upload-files",
                headers=sarvam_headers,
                json={"job_id": job_id, "files": [filename]},
            )
        _raise_sarvam(up_resp, "upload-url")
        up_data      = up_resp.json()
        print(f"[TRANSCRIBE] upload-files response keys: {list(up_data.keys())}")
        # API returns upload_urls as dict {filename: url} or list [{url:...}]
        raw_urls = up_data.get("upload_urls") or up_data.get("files") or {}
        print(f"[TRANSCRIBE] raw_urls type={type(raw_urls).__name__} value={str(raw_urls)[:300]}")
        if isinstance(raw_urls, dict):
            first = next(iter(raw_urls.values()), None)
            presigned_upload = first.get("file_url") or first.get("url") or first if isinstance(first, dict) else first
        elif isinstance(raw_urls, list) and raw_urls:
            entry = raw_urls[0]
            presigned_upload = entry if isinstance(entry, str) else (entry.get("url") or entry.get("upload_url"))
        else:
            presigned_upload = None
        print(f"[TRANSCRIBE] presigned_upload={str(presigned_upload)[:120]}")
        if not isinstance(presigned_upload, str):
            raise HTTPException(status_code=502, detail=f"Could not extract presigned URL from upload-files: {up_data}")

        # ── Step 3: PUT audio to presigned URL (Azure Blob — needs BlockBlob header) ──
        async with httpx.AsyncClient(timeout=120) as client:
            put_resp = await client.put(
                presigned_upload,
                content=audio_bytes,
                headers={"Content-Type": mime, "x-ms-blob-type": "BlockBlob"},
            )
        print(f"[TRANSCRIBE] PUT → {put_resp.status_code}")
        if put_resp.status_code not in (200, 201, 204):
            raise HTTPException(status_code=502, detail=f"Audio upload PUT failed: {put_resp.status_code} {put_resp.text[:300]}")
        print(f"[TRANSCRIBE] audio uploaded → {put_resp.status_code}")

        # ── Step 4: Start job ─────────────────────────────────────────────────
        async with httpx.AsyncClient(timeout=30) as client:
            start_resp = await client.post(
                f"{_SARVAM_BATCH_BASE}/{job_id}/start",
                headers=sarvam_headers,
                json={},
            )
        _raise_sarvam(start_resp, "start")
        print(f"[TRANSCRIBE] job started")

        # ── Step 5: Poll until Completed ──────────────────────────────────────
        status_data: dict = {}
        for poll_n in range(_MAX_POLLS):
            await asyncio.sleep(_POLL_INTERVAL_S)
            async with httpx.AsyncClient(timeout=30) as client:
                stat_resp = await client.get(
                    f"{_SARVAM_BATCH_BASE}/{job_id}/status",
                    headers={"api-subscription-key": SARVAM_API_KEY},
                )
            _raise_sarvam(stat_resp, "status")
            status_data = stat_resp.json()
            job_state   = status_data.get("job_state", "")
            print(f"[TRANSCRIBE] poll#{poll_n+1} state={job_state}")
            if job_state in ("Completed", "PartiallyCompleted"):
                break
            if job_state == "Failed":
                raise HTTPException(status_code=500, detail="Sarvam batch job failed")
        else:
            raise HTTPException(status_code=504, detail="Transcription timed out after 4 minutes")

        # ── Step 6: Get download presigned URL ────────────────────────────────
        job_details  = status_data.get("job_details") or []
        output_files = []
        for detail in job_details:
            for out in (detail.get("outputs") or []):
                fname = out.get("file_name") or out.get("name")
                if fname:
                    output_files.append(fname)
        if not output_files:
            raise HTTPException(status_code=502, detail=f"No output files in job_details: {status_data}")
        print(f"[TRANSCRIBE] output files: {output_files}")

        async with httpx.AsyncClient(timeout=30) as client:
            dl_resp = await client.post(
                f"{_SARVAM_BATCH_BASE}/download-files",
                headers=sarvam_headers,
                json={"job_id": job_id, "files": output_files},
            )
        _raise_sarvam(dl_resp, "download-url")
        dl_data = dl_resp.json()
        print(f"[TRANSCRIBE] download-files keys={list(dl_data.keys())} value={str(dl_data)[:300]}")
        raw_dl = dl_data.get("download_urls") or dl_data.get("files") or {}
        if isinstance(raw_dl, dict):
            first_dl = next(iter(raw_dl.values()), None)
            presigned_download = first_dl.get("file_url") or first_dl.get("url") if isinstance(first_dl, dict) else first_dl
        elif isinstance(raw_dl, list) and raw_dl:
            entry = raw_dl[0]
            presigned_download = entry if isinstance(entry, str) else (entry.get("file_url") or entry.get("url"))
        else:
            presigned_download = None
        if not isinstance(presigned_download, str):
            raise HTTPException(status_code=502, detail=f"Could not extract download URL: {dl_data}")

        # ── Step 7: Fetch transcript JSON ─────────────────────────────────────
        async with httpx.AsyncClient(timeout=30) as client:
            result_resp = await client.get(presigned_download)
        if not result_resp.is_success:
            raise HTTPException(status_code=502, detail=f"Download failed: {result_resp.status_code}")

        result     = result_resp.json()
        print(f"[TRANSCRIBE] result keys: {list(result.keys()) if isinstance(result, dict) else type(result)}")
        # Sarvam output JSON may use "transcript", "text", or nested structure
        transcript = (
            result.get("transcript")
            or result.get("text")
            or result.get("transcription")
            or _extract_nested_transcript(result)
            or ""
        )
        lang_code = result.get("language_code", "")
        print(f"[TRANSCRIBE] done  lang={lang_code}  chars={len(transcript)}")
        return {"transcript": transcript, "language_code": lang_code}

    except HTTPException:
        raise
    except httpx.HTTPStatusError as e:
        print(f"[TRANSCRIBE ERROR] HTTP {e.response.status_code}: {e.response.text}")
        raise HTTPException(status_code=502, detail=f"Sarvam error: {e.response.text}")
    except Exception as e:
        print(f"[TRANSCRIBE ERROR] {e}")
        raise HTTPException(status_code=500, detail=f"Transcription failed: {e}")


def _raise_sarvam(resp: httpx.Response, step: str) -> None:
    if not resp.is_success:
        print(f"[TRANSCRIBE] {step} failed {resp.status_code}: {resp.text}")
        raise httpx.HTTPStatusError(f"{step} failed", request=resp.request, response=resp)


def _extract_nested_transcript(data: dict) -> str:
    """Fallback: walk nested dicts/lists to find a non-empty 'transcript' or 'text' value."""
    if not isinstance(data, dict):
        return ""
    for key in ("transcript", "text", "transcription"):
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    # try one level deeper (e.g. {"result": {"transcript": "..."}})
    for v in data.values():
        if isinstance(v, dict):
            result = _extract_nested_transcript(v)
            if result:
                return result
        if isinstance(v, list):
            for item in v:
                if isinstance(item, dict):
                    result = _extract_nested_transcript(item)
                    if result:
                        return result
    return ""


# ─── EXTRACT ─────────────────────────────────────────────────────────────────

_EXTRACT_SYSTEM = """You are a medical prescription extraction assistant.
Extract structured prescription data from the doctor-patient consultation transcript provided.
Return ONLY valid JSON (no markdown fences, no extra text) with exactly these fields:
{
  "chief_complaint": string or null,
  "diagnosis": string or null,
  "past_history": string or null,
  "allergies": string or null,
  "lab_findings": string or null,
  "dietary_instructions": string or null,
  "precautions": string or null,
  "medicines": [
    {
      "name": string,
      "strength": string or null,
      "duration_days": integer,
      "morning": boolean,
      "afternoon": boolean,
      "evening": boolean,
      "night": boolean,
      "instructions": string or null
    }
  ],
  "vitals": {
    "temperature": string or null,
    "bp": string or null,
    "pulse": string or null,
    "spo2": string or null,
    "weight": string or null
  }
}
Field guidance:
- past_history: chronic conditions, surgical history, family history mentioned (e.g. "diabetic and hypertensive for 10 years, appendix surgery 15 years ago")
- allergies: any allergies mentioned; use "NKDA" if doctor explicitly says no known allergies
- lab_findings: any investigation results or observations mentioned (e.g. "no abnormalities in platelet WBC")
Never invent medical information not present in the transcript.
Use null for fields not mentioned. Use empty array [] for medicines if none mentioned."""


@router.post("/prescriptions/extract")
async def extract_prescription(request: Request):
    """Parse consultation transcript into structured prescription fields using Claude."""
    body = await request.json()
    transcript = (body.get("transcript") or "").strip()
    if not transcript:
        raise HTTPException(status_code=400, detail="transcript is required")
    if not ANTHROPIC_API_KEY:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY not configured")

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 1024,
                    "system": _EXTRACT_SYSTEM,
                    "messages": [{"role": "user", "content": transcript}],
                },
            )
        data = resp.json()
        text = data["content"][0]["text"].strip()
        # Strip markdown code fences if model wraps anyway
        if text.startswith("```"):
            text = "\n".join(text.split("\n")[1:])
            if text.endswith("```"):
                text = text[: text.rfind("```")]
        extracted = json.loads(text.strip())
        return extracted
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=500, detail=f"Claude returned invalid JSON: {e}")
    except Exception as e:
        print(f"[EXTRACT ERROR] {e}")
        raise HTTPException(status_code=500, detail=f"Extraction failed: {e}")


# ─── GENERATE PDF ─────────────────────────────────────────────────────────────

@router.post("/prescriptions/generate-pdf")
async def generate_prescription_pdf(request: Request):
    """Build a branded PDF from prescription data and return it as application/pdf."""
    body = await request.json()
    pdf_bytes = build_pdf_bytes(body)

    patient_name = body.get("patient_name", "Patient").replace(" ", "-")
    today_slug   = datetime.now(IST).strftime("%d%b%Y")
    filename     = f"Prescription-{patient_name}-{today_slug}.pdf"

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


# ─── PDF BUILDER (also imported by main.py for WhatsApp send) ────────────────

def build_pdf_bytes(data: dict) -> bytes:
    """Render a prescription PDF from a data dict and return raw bytes."""
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        HRFlowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
    )

    # ── Styles ────────────────────────────────────────────────────────────────
    styles  = getSampleStyleSheet()
    teal    = colors.HexColor("#0d9488")
    dark    = colors.HexColor("#1e293b")
    muted   = colors.HexColor("#64748b")
    lighter = colors.HexColor("#f0fdf4")
    accent  = colors.HexColor("#e2e8f0")

    h1 = ParagraphStyle("h1", fontSize=16, fontName="Helvetica-Bold",
                         textColor=dark, spaceAfter=1)
    h2 = ParagraphStyle("h2", fontSize=10, fontName="Helvetica",
                         textColor=muted, spaceAfter=4)
    sect = ParagraphStyle("sect", fontSize=8,  fontName="Helvetica-Bold",
                          textColor=teal, spaceBefore=8, spaceAfter=3,
                          leading=10)
    body = ParagraphStyle("body", fontSize=10, fontName="Helvetica",
                          textColor=dark, leading=14)
    small = ParagraphStyle("small", fontSize=8,  fontName="Helvetica",
                           textColor=muted, leading=11)
    foot  = ParagraphStyle("foot", fontSize=7,  fontName="Helvetica",
                           textColor=muted, alignment=TA_CENTER)

    # ── Data extraction ───────────────────────────────────────────────────────
    clinic_name    = data.get("clinic_name",    "TrueCare Family Clinic")
    doctor_name    = data.get("doctor_name",    "")
    doctor_qual    = data.get("doctor_qualification", "")
    patient_name   = data.get("patient_name",   "")
    patient_age    = str(data.get("patient_age",  "") or "")
    patient_gender = data.get("patient_gender", "")
    patient_code   = data.get("patient_code",   "")
    visit_date     = data.get("visit_date") or datetime.now(IST).strftime("%d %b %Y")
    chief_complaint= data.get("chief_complaint", "")
    diagnosis      = data.get("diagnosis",       "")
    past_history   = data.get("past_history",    "")
    allergies      = data.get("allergies",       "")
    lab_findings   = data.get("lab_findings",    "")
    medicines      = data.get("medicines",       [])
    dietary        = data.get("dietary_instructions", "")
    precautions    = data.get("precautions",     "")
    notes          = data.get("notes",           "")
    followup_date  = data.get("followup_date",   "")
    vitals         = data.get("vitals",          {})

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=18*mm, rightMargin=18*mm,
        topMargin=16*mm, bottomMargin=16*mm,
    )
    W = A4[0] - 36*mm  # usable width
    story = []

    # ── Header ────────────────────────────────────────────────────────────────
    dr_label = doctor_name
    if doctor_qual:
        dr_label += f"\n{doctor_qual}"
    header_data = [
        [Paragraph(clinic_name, h1),
         Paragraph(f"{dr_label}\n{visit_date}" if dr_label else visit_date,
                   ParagraphStyle("dr", fontSize=9, fontName="Helvetica",
                                  textColor=muted, alignment=TA_RIGHT, leading=13))]
    ]
    header_tbl = Table(header_data, colWidths=[W * 0.62, W * 0.38])
    header_tbl.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
    ]))
    story.append(header_tbl)
    story.append(Spacer(1, 2*mm))
    story.append(HRFlowable(width="100%", thickness=2, color=teal))
    story.append(Spacer(1, 3*mm))

    # ── Patient info row ──────────────────────────────────────────────────────
    gender_label = "Male" if patient_gender == "M" else "Female" if patient_gender == "F" else (patient_gender or "")
    pat_parts = [patient_name or "—"]
    if patient_age:
        pat_parts.append(f"{patient_age} yrs")
    if gender_label:
        pat_parts.append(gender_label)
    if patient_code:
        pat_parts.append(f"({patient_code})")
    pat_str = "  ·  ".join(pat_parts)

    pat_tbl = Table(
        [[Paragraph(pat_str, ParagraphStyle("pat", fontSize=11, fontName="Helvetica-Bold",
                                            textColor=dark))]],
        colWidths=[W],
    )
    pat_tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), lighter),
        ("TOPPADDING",    (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING",   (0, 0), (-1, -1), 8),
        ("ROUNDEDCORNERS", [4]),
    ]))
    story.append(pat_tbl)
    story.append(Spacer(1, 3*mm))

    # ── Vitals bar ────────────────────────────────────────────────────────────
    def _v(label, val):
        return f"{label}: {val}" if val else None

    bp_val = None
    if vitals.get("bp_systolic") and vitals.get("bp_diastolic"):
        bp_val = f"{vitals['bp_systolic']}/{vitals['bp_diastolic']} mmHg"
    elif vitals.get("bp"):
        bp_val = vitals["bp"]

    vital_parts = list(filter(None, [
        _v("BP",     bp_val),
        _v("Pulse",  vitals.get("pulse_bpm") or vitals.get("pulse")),
        _v("Temp",   (f"{vitals['temperature_f']}°F" if vitals.get("temperature_f")
                      else vitals.get("temperature"))),
        _v("SpO2",   vitals.get("spo2_percent") or vitals.get("spo2")),
        _v("Wt",     vitals.get("weight_kg") or vitals.get("weight")),
        _v("Ht",     vitals.get("height_cm")),
    ]))
    if vital_parts:
        vstr = "   |   ".join(vital_parts)
        vtbl = Table(
            [[Paragraph(vstr, ParagraphStyle("vit", fontSize=9, fontName="Helvetica",
                                              textColor=colors.HexColor("#0f766e")))]],
            colWidths=[W],
        )
        vtbl.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1, -1), colors.HexColor("#f0fdfa")),
            ("TOPPADDING",    (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("LEFTPADDING",   (0, 0), (-1, -1), 8),
            ("BOX",           (0, 0), (-1, -1), 1, colors.HexColor("#99f6e4")),
        ]))
        story.append(vtbl)
        story.append(Spacer(1, 3*mm))

    # ── Rx symbol ─────────────────────────────────────────────────────────────
    story.append(Paragraph("℞", ParagraphStyle("rx", fontSize=22, fontName="Helvetica-Bold",
                                                textColor=dark)))
    story.append(Spacer(1, 1*mm))

    # ── Chief Complaint ───────────────────────────────────────────────────────
    if chief_complaint:
        story.append(Paragraph("CHIEF COMPLAINT", sect))
        story.append(Paragraph(chief_complaint, body))

    # ── Diagnosis ─────────────────────────────────────────────────────────────
    if diagnosis:
        story.append(Paragraph("DIAGNOSIS", sect))
        story.append(Paragraph(f"<b>{diagnosis}</b>", body))

    # ── History ───────────────────────────────────────────────────────────────
    if past_history or allergies or lab_findings:
        story.append(Paragraph("HISTORY", sect))
        if past_history:
            story.append(Paragraph(f"<b>Past History:</b> {past_history}", body))
        if allergies:
            allergy_color = "red" if allergies.upper() not in ("NKDA", "NIL", "NONE", "NO KNOWN ALLERGIES") else "grey"
            story.append(Paragraph(f"<b><font color='{allergy_color}'>Allergies:</font></b> {allergies}", body))
        if lab_findings:
            story.append(Paragraph(f"<b>Investigations:</b> {lab_findings}", body))

    # ── Medicines ─────────────────────────────────────────────────────────────
    valid_meds = [m for m in medicines if (m.get("medicine_name") or m.get("name") or "").strip()]
    if valid_meds:
        story.append(Paragraph("MEDICINES", sect))
        timing_map = {"morning": "M", "afternoon": "A", "evening": "E", "night": "N"}

        tbl_data = [[
            Paragraph("#",               ParagraphStyle("th", fontSize=8, fontName="Helvetica-Bold", textColor=muted)),
            Paragraph("Medicine",        ParagraphStyle("th", fontSize=8, fontName="Helvetica-Bold", textColor=muted)),
            Paragraph("Timing",          ParagraphStyle("th", fontSize=8, fontName="Helvetica-Bold", textColor=muted)),
            Paragraph("Duration",        ParagraphStyle("th", fontSize=8, fontName="Helvetica-Bold", textColor=muted)),
        ]]
        for i, m in enumerate(valid_meds, 1):
            med_name = (m.get("medicine_name") or m.get("name") or "").strip()
            strength = m.get("dosage") or m.get("strength") or ""
            name_str = f"<b>{med_name}</b>"
            if strength:
                name_str += f"  <font size='8' color='grey'>{strength}</font>"
            if m.get("instructions"):
                name_str += f"<br/><font size='8' color='grey'><i>{m['instructions']}</i></font>"

            timing_slots = [v for k, v in timing_map.items() if m.get(k)]
            timing_str = "-".join(timing_slots) if timing_slots else "—"

            food_str = ""
            # Detect before_food from timing_details or before_food field
            td = m.get("timing_details") or {}
            first_td = next(iter(td.values()), {}) if td else {}
            bf = first_td.get("before_food", m.get("before_food", False))
            food_str = " · Bef" if bf else " · Aft"

            dur = f"{m.get('duration_days', 5)} days"

            tbl_data.append([
                Paragraph(str(i), small),
                Paragraph(name_str, ParagraphStyle("mn", fontSize=10, fontName="Helvetica",
                                                    textColor=dark, leading=14)),
                Paragraph(timing_str + food_str, small),
                Paragraph(dur, small),
            ])

        med_tbl = Table(tbl_data, colWidths=[8*mm, W - 65*mm, 35*mm, 22*mm])
        med_tbl.setStyle(TableStyle([
            # Header row
            ("BACKGROUND",    (0, 0), (-1, 0), accent),
            ("TOPPADDING",    (0, 0), (-1, 0), 4),
            ("BOTTOMPADDING", (0, 0), (-1, 0), 4),
            # Data rows
            ("TOPPADDING",    (0, 1), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 1), (-1, -1), 5),
            ("LINEBELOW",     (0, 0), (-1, -2), 0.5, colors.HexColor("#e2e8f0")),
            ("VALIGN",        (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING",   (0, 0), (-1, -1), 4),
        ]))
        story.append(med_tbl)

    # ── Advice ────────────────────────────────────────────────────────────────
    advice_parts = [s for s in [dietary, precautions, notes] if s]
    if advice_parts:
        story.append(Paragraph("ADVICE", sect))
        for line in advice_parts:
            story.append(Paragraph(line, body))
            story.append(Spacer(1, 1*mm))

    # ── Follow-up ─────────────────────────────────────────────────────────────
    if followup_date:
        try:
            fu_fmt = datetime.strptime(followup_date, "%Y-%m-%d").strftime("%d %b %Y")
        except Exception:
            fu_fmt = followup_date
        fu_style = ParagraphStyle("fu", fontSize=9, fontName="Helvetica",
                                  textColor=HexColor("#166534"),
                                  backColor=HexColor("#f0fdf4"),
                                  borderPad=4, borderWidth=0.5,
                                  borderColor=HexColor("#bbf7d0"),
                                  borderRadius=4, leading=14)
        story.append(Paragraph(f"<b>Follow-up Review:</b>  {fu_fmt}", fu_style))
        story.append(Spacer(1, 4*mm))

    # ── Signature ────────────────────────────────────────────────────────────
    story.append(Spacer(1, 8*mm))
    story.append(HRFlowable(width="100%", thickness=0.5, color=accent))
    story.append(Spacer(1, 3*mm))

    sig_name = doctor_name or "Doctor"
    sig_qual = doctor_qual or ""
    sig_tbl = Table(
        [[Paragraph(f"<b>{sig_name}</b><br/>"
                    f"<font size='8' color='grey'>{sig_qual}</font>",
                    ParagraphStyle("sig", fontSize=10, fontName="Helvetica",
                                   textColor=dark, alignment=TA_RIGHT))]],
        colWidths=[W],
    )
    sig_tbl.setStyle(TableStyle([("TOPPADDING", (0, 0), (-1, -1), 0)]))
    story.append(sig_tbl)

    # ── Footer ────────────────────────────────────────────────────────────────
    story.append(Spacer(1, 4*mm))
    story.append(HRFlowable(width="100%", thickness=0.5, color=accent))
    story.append(Spacer(1, 2*mm))
    story.append(Paragraph(
        f"Computer-generated prescription  ·  {clinic_name}  ·  Powered by Parro Connect",
        foot,
    ))

    doc.build(story)
    return buf.getvalue()

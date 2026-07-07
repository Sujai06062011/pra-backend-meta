"""
prescription_ai_router.py — AI-powered prescription endpoints.

  POST /prescriptions/transcribe   — audio file → transcript (Groq Whisper)
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

GROQ_API_KEY      = os.getenv("GROQ_API_KEY", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
IST               = pytz.timezone("Asia/Kolkata")


# ─── TRANSCRIBE ──────────────────────────────────────────────────────────────

@router.post("/prescriptions/transcribe")
async def transcribe_audio_endpoint(audio: UploadFile = File(...)):
    """Receive audio blob from browser MediaRecorder, return plain transcript."""
    if not GROQ_API_KEY:
        raise HTTPException(status_code=503, detail="GROQ_API_KEY not configured")

    audio_bytes = await audio.read()
    mime = audio.content_type or "audio/webm"

    try:
        import groq as _groq
        client = _groq.Groq(api_key=GROQ_API_KEY)
        result = client.audio.transcriptions.create(
            file=(audio.filename or "recording.webm", audio_bytes, mime),
            model="whisper-large-v3",
            language="en",
            response_format="text",
        )
        transcript = result if isinstance(result, str) else getattr(result, "text", str(result))
        print(f"[TRANSCRIBE] {len(audio_bytes)} bytes → {len(transcript)} chars")
        return {"transcript": transcript}
    except Exception as e:
        print(f"[TRANSCRIBE ERROR] {e}")
        raise HTTPException(status_code=500, detail=f"Transcription failed: {e}")


# ─── EXTRACT ─────────────────────────────────────────────────────────────────

_EXTRACT_SYSTEM = """You are a medical prescription extraction assistant.
Extract structured prescription data from the doctor-patient consultation transcript provided.
Return ONLY valid JSON (no markdown fences, no extra text) with exactly these fields:
{
  "chief_complaint": string or null,
  "diagnosis": string or null,
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
    medicines      = data.get("medicines",       [])
    dietary        = data.get("dietary_instructions", "")
    precautions    = data.get("precautions",     "")
    notes          = data.get("notes",           "")
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

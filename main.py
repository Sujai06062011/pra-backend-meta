from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse
from dotenv import load_dotenv
import os
from datetime import date, timedelta
from collections import defaultdict
from whatsapp_handler import handle_message
from scheduler import init_scheduler, reschedule
from followup import prewarm_response_audios
import config_loader


load_dotenv()

app = FastAPI(title="PRA - Patient Relationship Assistant")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

twilio_client = Client(
    os.getenv("TWILIO_ACCOUNT_SID"),
    os.getenv("TWILIO_AUTH_TOKEN")
)

TWILIO_FROM = os.getenv("TWILIO_WHATSAPP_FROM")


@app.post("/admin/onboard-clinic")
async def onboard_clinic(request: Request):
    data = await request.json()
    
    # Normalize WhatsApp number - always strip +
    whatsapp = data.get("whatsapp_number", "").replace("+", "").strip()
    
    result = supabase.table("doctors").insert({
        "name": data.get("doctor_name"),
        "clinic_name": data.get("clinic_name"),
        "whatsapp_number": whatsapp,  # always stored without +
        "clinic_timings": data.get("timings", "Mon-Sat: 9AM-1PM, 5PM-8PM"),
        "clinic_address": data.get("address", ""),
        "email": data.get("email", ""),
        "mobile": data.get("mobile", "")
    }).execute()
    
    return {"status": "success", "doctor_id": result.data[0]["id"]}

# ── GLOBAL SCHEDULER REFERENCE (for reload endpoint) ─────
_scheduler = None


@app.on_event("startup")
async def startup_event():
    global _scheduler
    _scheduler = await init_scheduler()
    _scheduler.start()

    # Pre-warm all response audios
    await prewarm_response_audios()

    print("🚀 PRA Backend started with DB-driven scheduler")

def send_whatsapp(to_number: str, message: str):
    try:
        msg = twilio_client.messages.create(
            from_=TWILIO_FROM,
            to=f"whatsapp:+{to_number}",
            body=message
        )
        print(f"✅ Sent to {to_number}: SID {msg.sid}")
    except Exception as e:
        print(f"❌ Twilio error: {e}")


@app.get("/")
async def root():
    return {"status": "PRA Backend Running", "version": "1.0.0"}


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/webhook/whatsapp")
async def whatsapp_webhook(request: Request):
    try:
        form_data = await request.form()
        from_raw = form_data.get("From", "")
        to_raw = form_data.get("To", "")
        body = form_data.get("Body", "").strip()
        media_url = form_data.get("MediaUrl0", "")

        from_number = from_raw.replace("whatsapp:+", "").replace("whatsapp:", "")
        to_number = to_raw.replace("whatsapp:", "")

        print(f"\n📱 Inbound: {from_number} → {to_number}: {body}")

        reply = await handle_message(from_number, body, to_number, media_url)
        print(f"💬 Reply: {reply[:80]}...")

        send_whatsapp(from_number, reply)

    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()

    resp = MessagingResponse()
    return PlainTextResponse(str(resp), status_code=200, media_type="application/xml")


@app.get("/webhook/whatsapp")
async def whatsapp_verify(request: Request):
    params = dict(request.query_params)
    challenge = params.get("hub.challenge", "")
    return PlainTextResponse(challenge)


# ── TEST TRIGGER ENDPOINTS ────────────────────────────────
@app.post("/trigger/morning-reminders")
async def trigger_morning_reminders():
    from scheduler import send_morning_reminders
    await send_morning_reminders()
    return {"status": "Morning reminders sent"}


@app.post("/trigger/evening-reminders")
async def trigger_evening_reminders():
    from scheduler import send_evening_reminders
    await send_evening_reminders()
    return {"status": "Evening reminders sent"}


@app.post("/trigger/visit-summary")
async def trigger_visit_summary():
    from scheduler import send_visit_summary
    await send_visit_summary()
    return {"status": "Visit summaries sent"}


@app.post("/trigger/review-requests")
async def trigger_review_requests():
    from scheduler import send_review_requests
    await send_review_requests()
    return {"status": "Review requests sent"}


# ── CLINIC MEDICINES ──────────────────────────────────────

@app.get("/medicines/categories")
async def get_medicine_categories(doctor_id: str):
    from database import supabase as db
    result = db.table("clinic_medicines").select("category").eq("is_active", True).execute()
    categories = sorted(set(r["category"] for r in (result.data or []) if r.get("category")))
    return categories

@app.get("/medicines")
async def search_medicines(doctor_id: str, search: str = "", limit: int = 10):
    from database import supabase as db
    # Fetch all active medicines, filter by name in Python (ilike not reliable across versions)
    result = db.table("clinic_medicines").select("*").eq("is_active", True).order("usage_count", desc=True).limit(200).execute()
    data = result.data or []
    if search:
        q_lower = search.lower()
        data = [m for m in data if q_lower in (m.get("name") or "").lower()]
    return data[:limit]

@app.post("/medicines")
async def add_medicine(request: Request):
    from database import supabase as db
    import datetime as dt
    body = await request.json()
    result = db.table("clinic_medicines").insert({
        "doctor_id": body["doctor_id"],
        "name": body["name"],
        "category": body.get("category", "Other"),
        "dosages": body.get("dosages", []),
        "form": body.get("form", "tablet"),
        "is_active": True,
    }).execute()
    return result.data[0] if result.data else {}

@app.put("/medicines/{medicine_id}")
async def update_medicine(medicine_id: str, request: Request):
    from database import supabase as db
    import datetime as dt
    body = await request.json()
    update_data = {k: v for k, v in {
        "name": body.get("name"),
        "category": body.get("category"),
        "dosages": body.get("dosages"),
        "form": body.get("form"),
        "is_active": body.get("is_active"),
        "updated_at": dt.datetime.utcnow().isoformat(),
    }.items() if v is not None}
    result = db.table("clinic_medicines").update(update_data).eq("id", medicine_id).execute()
    return result.data[0] if result.data else {}

@app.patch("/medicines/{medicine_id}/increment-usage")
async def increment_usage(medicine_id: str):
    from database import supabase as db
    db.rpc("increment_medicine_usage", {"med_id": medicine_id}).execute()
    return {"ok": True}

@app.delete("/medicines/{medicine_id}")
async def deactivate_medicine(medicine_id: str):
    from database import supabase as db
    db.table("clinic_medicines").update({"is_active": False}).eq("id", medicine_id).execute()
    return {"ok": True}


# ── PRESCRIPTIONS WRITE ───────────────────────────────────

@app.post("/prescriptions/write")
async def write_prescription(request: Request):
    from database import supabase as db
    import datetime as dt
    import pytz

    body = await request.json()
    patient_id       = body["patient_id"]
    doctor_id_req    = body.get("doctor_id", "8c33abe0-5d2e-4613-9437-c7c375e8d162")
    appointment_id   = body.get("appointment_id") or None
    chief_complaint  = body.get("chief_complaint", "")
    diagnosis        = body.get("diagnosis", "")
    notes            = body.get("notes", "")
    dietary          = body.get("dietary_instructions", "")
    precautions      = body.get("precautions", "")
    medicines_input  = body.get("medicines", [])

    IST = pytz.timezone("Asia/Kolkata")
    now_ist = dt.datetime.now(IST)
    today_str = now_ist.date().isoformat()

    # 1. Fetch patient
    pat_res = db.table("patients").select("id, name, mobile, patient_code, language").eq("id", patient_id).execute()
    if not pat_res.data:
        return {"error": "Patient not found"}, 404
    patient = pat_res.data[0]

    # 2. Create visit
    visit_res = db.table("visits").insert({
        "patient_id":      patient_id,
        "doctor_id":       doctor_id_req,
        "appointment_id":  appointment_id,
        "chief_complaint": chief_complaint,
        "diagnosis":       diagnosis,
        "notes":           notes,
        "visit_status":    "Completed",
        "created_at":      now_ist.isoformat(),
    }).execute()
    visit = visit_res.data[0] if visit_res.data else {}
    visit_id = visit.get("id", "")

    # 3. Create prescription
    pres_res = db.table("prescriptions").insert({
        "patient_id":           patient_id,
        "doctor_id":            doctor_id_req,
        "visit_id":             visit_id,
        "prescription_date":    today_str,
        "dietary_instructions": dietary,
        "precautions":          precautions,
        "general_notes":        notes,
        "followup_whatsapp_sent": False,
        "followup_replied":     False,
        "followup_call_sent":   False,
        "created_at":           now_ist.isoformat(),
    }).execute()
    pres = pres_res.data[0] if pres_res.data else {}
    pres_id = pres.get("id", "")

    # 4. Insert medicines
    med_rows = []
    for i, m in enumerate(medicines_input):
        if not m.get("medicine_name", "").strip():
            continue
        med_rows.append({
            "prescription_id": pres_id,
            "medicine_name":   m["medicine_name"],
            "dosage":          m.get("dosage", ""),
            "morning":         m.get("morning", False),
            "afternoon":       m.get("afternoon", False),
            "evening":         m.get("evening", False),
            "night":           m.get("night", False),
            "before_food":     m.get("before_food", False),
            "duration_days":   m.get("duration_days", 5),
            "instructions":    m.get("instructions", ""),
            "sort_order":      m.get("sort_order", i + 1),
        })
    if med_rows:
        db.table("prescription_medicines").insert(med_rows).execute()

    # 5. Create followup record (7 days from today — WhatsApp channel)
    followup_date = (now_ist.date() + dt.timedelta(days=7)).isoformat()
    try:
        db.table("followups").insert({
            "patient_id":    patient_id,
            "doctor_id":     doctor_id_req,
            "visit_id":      visit_id,
            "scheduled_date": followup_date,
            "channel":       "whatsapp",
            "call_status":   "Pending",
            "followup_day":  7,
        }).execute()
    except Exception as fe:
        print(f"⚠️ Followup insert error: {fe}")

    # 6. Send WhatsApp prescription summary
    whatsapp_sent = False
    try:
        mobile   = patient.get("mobile", "")
        pname    = patient.get("name", "Patient")
        pcode    = patient.get("patient_code", "")
        language = patient.get("language", "english")

        # Build medicine lines
        timing_icons = {"morning": "🌅", "afternoon": "☀️", "evening": "🌆", "night": "🌙"}
        timing_labels_en = {"morning": "Morning", "afternoon": "Afternoon", "evening": "Evening", "night": "Night"}
        timing_labels_ta = {"morning": "காலை", "afternoon": "மதியம்", "evening": "மாலை", "night": "இரவு"}

        def med_line(m, lang, idx):
            timings_keys = [k for k in ["morning", "afternoon", "evening", "night"] if m.get(k)]
            icons = " + ".join(timing_icons[k] for k in timings_keys)
            if lang == "tamil":
                labels = " + ".join(timing_labels_ta[k] for k in timings_keys)
                food = "சாப்பிடுவதற்கு முன்" if m.get("before_food") else "சாப்பிட்ட பின்"
                dur = f"{m.get('duration_days', 5)} நாட்கள்"
            else:
                labels = " + ".join(timing_labels_en[k] for k in timings_keys)
                food = "Before food" if m.get("before_food") else "After food"
                dur = f"{m.get('duration_days', 5)} days"
            inst = f"\n   {m['instructions']}" if m.get("instructions") else ""
            return f"{idx}. {m['medicine_name']} — {m.get('dosage','')}\n   {icons} {labels} | {food} | {dur}{inst}"

        med_lines = "\n\n".join(med_line(m, language, i+1) for i, m in enumerate(medicines_input) if m.get("medicine_name","").strip())

        if language == "tamil":
            msg = (
                f"💊 *மருந்துச் சீட்டு*\n"
                f"🏥 Dr. Kumar Child Care Clinic\n\n"
                f"நோயாளி: {pname}" + (f" ({pcode})" if pcode else "") + f"\n"
                f"தேதி: {now_ist.strftime('%d %b %Y')}\n"
                f"நோய்: {diagnosis}\n\n"
                f"மருந்துகள்:\n{med_lines}"
            )
            if dietary:
                msg += f"\n\n🥗 உணவு: {dietary}"
            if precautions:
                msg += f"\n⚠️ எச்சரிக்கை: {precautions}"
            msg += f"\n\nFollow-up: 3 நாட்களில் வரவும்.\nகேள்விகளுக்கு MENU என்று reply பண்ணுங்கள்."
        else:
            msg = (
                f"💊 *Your Prescription*\n"
                f"🏥 Dr. Kumar Child Care Clinic\n\n"
                f"Patient: {pname}" + (f" ({pcode})" if pcode else "") + f"\n"
                f"Date: {now_ist.strftime('%d %b %Y')}\n"
                f"Diagnosis: {diagnosis}\n\n"
                f"Medicines:\n{med_lines}"
            )
            if dietary:
                msg += f"\n\n🥗 Diet: {dietary}"
            if precautions:
                msg += f"\n⚠️ Precautions: {precautions}"
            msg += f"\n\nFollow-up in 3 days if not improving.\nReply MENU for any help."

        if mobile:
            from scheduler import send_whatsapp as _wa
            _wa(mobile, msg)
            whatsapp_sent = True

    except Exception as we:
        print(f"⚠️ WhatsApp send error: {we}")

    return {
        "prescription_id": pres_id,
        "visit_id": visit_id,
        "whatsapp_sent": whatsapp_sent,
        "patient_name": patient.get("name", ""),
    }


# ── CLINIC CONFIG ─────────────────────────────────────────

@app.get("/config/{doctor_id}")
async def get_config(doctor_id: str):
    """Return all config rows for a doctor as typed dict."""
    result = config_loader._sb.table("clinic_config") \
        .select("config_key, config_value, config_type, description, updated_at") \
        .eq("doctor_id", doctor_id) \
        .order("config_key") \
        .execute()
    return result.data or []


@app.patch("/config/{doctor_id}/{config_key}")
async def update_config(doctor_id: str, config_key: str, request: Request):
    """Upsert a single config key for a doctor."""
    import datetime as dt
    body = await request.json()
    config_value = body.get("config_value", "")
    result = config_loader._sb.table("clinic_config").upsert({
        "doctor_id": doctor_id,
        "config_key": config_key,
        "config_value": str(config_value),
        "updated_at": dt.datetime.utcnow().isoformat(),
    }, on_conflict="doctor_id,config_key").execute()
    # Invalidate in-process cache
    config_loader.invalidate_cache()
    return result.data[0] if result.data else {}


@app.post("/config/reload-scheduler")
async def reload_scheduler_endpoint():
    """Invalidate config cache and reschedule all jobs with fresh DB config."""
    global _scheduler
    if _scheduler is None:
        return {"status": "error", "message": "Scheduler not initialized"}
    await reschedule(_scheduler)
    return {"status": "ok", "message": "Scheduler reloaded from DB config"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)


# ── DASHBOARD ─────────────────────────────────────────────
@app.get("/dashboard/stats")
async def dashboard_stats(doctor_id: str):
    from database import supabase
    today = date.today().isoformat()

    today_appts = supabase.table("appointments").select("id", count="exact").eq("doctor_id", doctor_id).eq("appointment_date", today).neq("status", "Cancelled").execute()
    # tokens uses queue_date, not appointment_date
    token_row = supabase.table("tokens").select("current_token").eq("doctor_id", doctor_id).eq("queue_date", today).execute()
    # patients table has no doctor_id — single-clinic deployment, count all patients
    all_patients = supabase.table("patients").select("id", count="exact").execute()
    total_patients = all_patients.count or 0
    # followups table (no underscore), filter by call_status not status
    pending_followups = supabase.table("followups").select("id", count="exact").eq("doctor_id", doctor_id).is_("completed_at", "null").execute()
    # completed = tokens seen today (current_token tracks last done token number)
    current_token_val = token_row.data[0]["current_token"] if token_row.data else 0
    today_completed_count = current_token_val

    week_map = defaultdict(int)
    for i in range(6, -1, -1):
        week_map[(date.today() - timedelta(days=i)).isoformat()] = 0
    week_appts = supabase.table("appointments").select("appointment_date").eq("doctor_id", doctor_id).gte("appointment_date", (date.today() - timedelta(days=6)).isoformat()).lte("appointment_date", today).execute()
    for row in (week_appts.data or []):
        week_map[row["appointment_date"]] += 1
    weekly = [{"date": d, "count": c} for d, c in sorted(week_map.items())]

    # diagnosis lives in visits table, not prescriptions
    visits = supabase.table("visits").select("diagnosis").eq("doctor_id", doctor_id).execute()
    diag_map = defaultdict(int)
    for row in (visits.data or []):
        if row.get("diagnosis"):
            diag_map[row["diagnosis"]] += 1
    top_diagnoses = sorted([{"diagnosis": k, "count": v} for k, v in diag_map.items()], key=lambda x: -x["count"])[:5]

    return {
        "today_appointments": today_appts.count or 0,
        "current_token": current_token_val,
        "total_patients": total_patients,
        "pending_followups": pending_followups.count or 0,
        "today_completed": today_completed_count,
        "weekly_appointments": weekly,
        "top_diagnoses": top_diagnoses,
    }


# ── PATIENTS ──────────────────────────────────────────────
@app.get("/patients")
async def list_patients(doctor_id: str, search: str = ""):
    from database import supabase
    # patients table has no doctor_id — single-clinic deployment, return all patients
    q = supabase.table("patients").select("*")
    if search:
        q = q.or_(f"name.ilike.%{search}%,mobile.ilike.%{search}%")
    result = q.order("created_at", desc=True).execute()
    return result.data or []


@app.get("/patients/family/{head_mobile}")
async def family_members(head_mobile: str):
    from database import supabase
    result = supabase.table("patients").select("*").eq("family_head_mobile", head_mobile).execute()
    return result.data or []


@app.get("/patients/lookup")
async def lookup_patient(mobile: str):
    """Look up patients by mobile number (with or without 91 prefix)."""
    from database import supabase
    m = mobile.strip().lstrip("+")
    candidates = {m}
    if m.startswith("91") and len(m) > 10:
        candidates.add(m[2:])
    else:
        candidates.add("91" + m)
    results = []
    for candidate in candidates:
        res = supabase.table("patients")\
            .select("*")\
            .or_(f"mobile.eq.{candidate},family_head_mobile.eq.{candidate}")\
            .execute()
        for p in (res.data or []):
            if not any(r["id"] == p["id"] for r in results):
                results.append(p)
    return results


@app.post("/patients/register")
async def register_patient(request: Request):
    """Register a new patient and generate patient_code."""
    from database import supabase
    body = await request.json()

    name       = (body.get("name") or "").strip()
    mobile_raw = (body.get("mobile") or "").strip().lstrip("+")
    dob        = body.get("date_of_birth") or ""
    gender     = body.get("gender") or ""
    language   = body.get("language") or ""
    email      = body.get("email") or None
    city       = body.get("city") or None
    fhm        = body.get("family_head_mobile") or None
    doctor_id  = body.get("doctor_id") or ""

    name_clean = name.replace(" ", "")
    prefix = name_clean[:3].upper() if len(name_clean) >= 3 else name_clean.upper().ljust(3, "X")
    suffix = mobile_raw[-4:] if len(mobile_raw) >= 4 else mobile_raw
    year   = dob[:4] if len(dob) >= 4 else "0000"
    base_code = f"{prefix}-{suffix}-{year}"

    # Fetch all codes and filter in Python (.like() triggers Cloudflare 1101 on this deployment)
    existing = supabase.table("patients").select("patient_code").execute()
    existing_codes = {r["patient_code"] for r in (existing.data or []) if (r.get("patient_code") or "").startswith(base_code)}
    code = base_code
    counter = 2
    while code in existing_codes:
        code = f"{base_code}-{counter}"
        counter += 1

    age = None
    if dob:
        try:
            from datetime import date as _date
            born = _date.fromisoformat(dob)
            today = _date.today()
            age = today.year - born.year - ((today.month, today.day) < (born.month, born.day))
        except Exception:
            pass

    # Only columns that actually exist in the patients table (verified from schema)
    # Columns: id, patient_code, name, mobile, age, gender, date_of_birth,
    #          email, address, whatsapp_number, registration_source,
    #          created_at, updated_at, family_head_mobile, language
    row = {
        "name":         name,
        "mobile":       mobile_raw,
        "age":          age,
        "gender":       gender,
        "language":     language,
        "patient_code": code,
        "registration_source": "clinic",
    }
    if dob: row["date_of_birth"] = dob
    if fhm: row["family_head_mobile"] = fhm.lstrip("+")

    from fastapi import HTTPException
    result = supabase.table("patients").insert(row).execute()
    if not result.data:
        raise HTTPException(status_code=500, detail="Insert failed")
    patient = result.data[0]

    # Send WhatsApp welcome message
    is_family = bool(fhm)
    lang = language.lower()
    pat_name = patient.get("name") or name
    pat_code = patient.get("patient_code") or code

    if lang == "tamil":
        if is_family:
            msg = (
                f"👋 வணக்கம் {pat_name}!\n\n"
                f"🏥 Dr. Kumar Child Care Clinic-ல் நீங்கள் பதிவு செய்யப்பட்டீர்கள்.\n"
                f"🪪 உங்கள் Patient ID: *{pat_code}*\n\n"
                f"சந்திப்பு பதிவு செய்ய MENU என்று reply பண்ணுங்கள்."
            )
        else:
            msg = (
                f"👋 வணக்கம் {pat_name}!\n\n"
                f"🏥 Dr. Kumar Child Care Clinic-ல் உங்களை வரவேற்கிறோம்.\n"
                f"🪪 உங்கள் Patient ID: *{pat_code}*\n\n"
                f"இந்த ID-ஐ ஒவ்வொரு வருகையிலும் பயன்படுத்துங்கள்.\n"
                f"சந்திப்பு பதிவு செய்ய MENU என்று reply பண்ணுங்கள்."
            )
    else:
        if is_family:
            msg = (
                f"👋 Hello {pat_name}!\n\n"
                f"🏥 You have been registered at Dr. Kumar Child Care Clinic.\n"
                f"🪪 Your Patient ID: *{pat_code}*\n\n"
                f"Reply MENU to book an appointment."
            )
        else:
            msg = (
                f"👋 Welcome to Dr. Kumar Child Care Clinic, {pat_name}!\n\n"
                f"🪪 Your Patient ID: *{pat_code}*\n\n"
                f"Please save this ID — you'll need it at every visit.\n"
                f"Reply MENU to book an appointment."
            )

    wa_sent = False
    send_to = mobile_raw or (fhm.lstrip("+") if fhm else "")
    if send_to:
        try:
            from scheduler import send_whatsapp as _wa
            _wa(send_to, msg)
            wa_sent = True
        except Exception as e:
            print(f"❌ WhatsApp welcome failed: {e}")

    patient["whatsapp_sent"] = wa_sent
    return patient


@app.get("/patients/search")
async def search_patient_by_mobile(mobile: str):
    from database import supabase
    normalized = mobile.lstrip("+")
    if normalized.startswith("91") and len(normalized) == 12:
        normalized = normalized[2:]
    with_prefix = "91" + normalized

    all_patients = supabase.table("patients").select("id, name, age, mobile, patient_code, gender").execute()
    matches = []
    for p in (all_patients.data or []):
        pm = (p.get("mobile") or "").lstrip("+")
        pm_short = pm[2:] if (pm.startswith("91") and len(pm) == 12) else pm
        if pm_short == normalized or pm == with_prefix or pm == normalized:
            matches.append({
                "patient_id": p["id"],
                "name": p["name"],
                "age": p.get("age"),
                "gender": p.get("gender"),
                "mobile": p["mobile"],
                "patient_code": p.get("patient_code"),
                "last_visit_date": None,
            })

    if not matches:
        raise HTTPException(status_code=404, detail="Patient not found")
    return matches


@app.get("/patients/{patient_id}")
async def get_patient(patient_id: str):
    from database import supabase
    result = supabase.table("patients").select("*").eq("id", patient_id).single().execute()
    return result.data


@app.patch("/patients/{patient_id}")
async def update_patient(patient_id: str, request: Request):
    from database import supabase
    import datetime as dt
    body = await request.json()
    # Only update columns that actually exist in the schema
    allowed = {"name", "mobile", "age", "gender", "language", "date_of_birth",
               "email", "address", "family_head_mobile"}
    update_data = {k: v for k, v in body.items() if k in allowed and v is not None}
    update_data["updated_at"] = dt.datetime.utcnow().isoformat()
    result = supabase.table("patients").update(update_data).eq("id", patient_id).execute()
    return result.data[0] if result.data else {}


@app.get("/patients/{patient_id}/visits")
async def get_patient_visits(patient_id: str):
    from database import supabase
    result = supabase.table("visits") \
        .select("*, appointments(appointment_date, token_number)") \
        .eq("patient_id", patient_id) \
        .order("created_at", desc=True) \
        .limit(5) \
        .execute()
    return result.data or []


# ── APPOINTMENTS ──────────────────────────────────────────
@app.get("/appointments/today")
async def today_appointments(doctor_id: str):
    from database import supabase
    today = date.today().isoformat()
    result = supabase.table("appointments").select("*, patients(*)").eq("doctor_id", doctor_id).eq("appointment_date", today).order("token_number", desc=False).execute()
    return result.data or []


@app.get("/appointments")
async def list_appointments(doctor_id: str, date: str = "", date_from: str = "", date_to: str = "", patient_id: str = ""):
    from database import supabase
    q = supabase.table("appointments").select("*, patients(*)").eq("doctor_id", doctor_id)
    if patient_id:
        q = q.eq("patient_id", patient_id)
    if date:
        q = q.eq("appointment_date", date)
    elif date_from and date_to:
        q = q.gte("appointment_date", date_from).lte("appointment_date", date_to)
    result = q.order("appointment_date", desc=True).order("token_number", desc=False).limit(50).execute()
    return result.data or []


@app.patch("/appointments/{appointment_id}/status")
async def update_appointment_status(appointment_id: str, request: Request):
    from database import supabase
    body = await request.json()
    result = supabase.table("appointments").update({"status": body["status"]}).eq("id", appointment_id).execute()
    return result.data[0] if result.data else {}


# ── QUEUE ─────────────────────────────────────────────────
@app.get("/queue/status")
async def queue_status(doctor_id: str, date: str = ""):
    from database import supabase
    import datetime as dt
    d = date if date else dt.date.today().isoformat()
    # tokens uses queue_date
    token_row = supabase.table("tokens").select("current_token").eq("doctor_id", doctor_id).eq("queue_date", d).execute()
    current = token_row.data[0]["current_token"] if token_row.data else 0

    appts = supabase.table("appointments").select("*, patients(*)").eq("doctor_id", doctor_id).eq("appointment_date", d).order("token_number", desc=False).execute()
    all_appts = appts.data or []
    confirmed = [a for a in all_appts if a.get("status") == "Confirmed"]
    # current_token = last token called; in-progress = current+1; done = ≤ current; waiting = > current+1
    seen    = [a for a in confirmed if (a.get("token_number") or 0) <= current]
    waiting = [a for a in confirmed if (a.get("token_number") or 0) > current + 1]

    return {
        "current_token": current,
        "total_today": len(all_appts),
        "waiting": len(waiting),
        "completed": len(seen),
        "appointments": all_appts,
    }


@app.post("/queue/next")
async def queue_next(request: Request):
    from database import supabase
    import datetime as dt
    body = await request.json()
    doctor_id = body["doctor_id"]
    today = dt.date.today().isoformat()
    token_row = supabase.table("tokens").select("current_token").eq("doctor_id", doctor_id).eq("queue_date", today).execute()
    new_token = (token_row.data[0]["current_token"] if token_row.data else 0) + 1
    if token_row.data:
        supabase.table("tokens").update({"current_token": new_token}).eq("doctor_id", doctor_id).eq("queue_date", today).execute()
    else:
        supabase.table("tokens").insert({"doctor_id": doctor_id, "queue_date": today, "current_token": new_token}).execute()
    return {"token": new_token}


@app.post("/queue/prev")
async def queue_prev(request: Request):
    from database import supabase
    import datetime as dt
    body = await request.json()
    doctor_id = body["doctor_id"]
    today = dt.date.today().isoformat()
    token_row = supabase.table("tokens").select("current_token").eq("doctor_id", doctor_id).eq("queue_date", today).execute()
    current = token_row.data[0]["current_token"] if token_row.data else 1
    new_token = max(1, current - 1)
    if token_row.data:
        supabase.table("tokens").update({"current_token": new_token}).eq("doctor_id", doctor_id).eq("queue_date", today).execute()
    else:
        supabase.table("tokens").insert({"doctor_id": doctor_id, "queue_date": today, "current_token": new_token}).execute()
    return {"token": new_token}


@app.post("/queue/set-token")
async def queue_set_token(request: Request):
    from database import supabase
    import datetime as dt
    body = await request.json()
    doctor_id = body["doctor_id"]
    token = body["token"]
    today = dt.date.today().isoformat()
    token_row = supabase.table("tokens").select("current_token").eq("doctor_id", doctor_id).eq("queue_date", today).execute()
    if token_row.data:
        supabase.table("tokens").update({"current_token": token}).eq("doctor_id", doctor_id).eq("queue_date", today).execute()
    else:
        supabase.table("tokens").insert({"doctor_id": doctor_id, "queue_date": today, "current_token": token}).execute()
    return {"token": token}


# ── PRESCRIPTIONS ─────────────────────────────────────────
@app.get("/prescriptions/{prescription_id}/detail")
async def get_prescription_detail(prescription_id: str):
    from database import supabase
    result = supabase.table("prescriptions").select(
        "*, patients(id, name, mobile, patient_code, age, gender, language), prescription_medicines(*), visits(id, chief_complaint, diagnosis, notes)"
    ).eq("id", prescription_id).execute()
    if not result.data:
        return {}
    row = _decode_walkin(result.data[0])
    # For walk-in: inject complaint/diagnosis as a synthetic visits object so frontend can load them
    if not row.get("patient_id") and not row.get("visits") and row.get("walkin_complaint"):
        row["visits"] = {
            "id": None,
            "chief_complaint": row.get("walkin_complaint", ""),
            "diagnosis":       row.get("walkin_diagnosis", ""),
            "notes":           row.get("general_notes", ""),
        }
    return row


@app.put("/prescriptions/{prescription_id}")
async def update_prescription(prescription_id: str, request: Request):
    from database import supabase
    body = await request.json()

    # 1. Update prescription fields
    supabase.table("prescriptions").update({
        "dietary_instructions": body.get("dietary_instructions", ""),
        "precautions":          body.get("precautions", ""),
        "general_notes":        body.get("notes", ""),
    }).eq("id", prescription_id).execute()

    # 2. Update visit (chief_complaint + diagnosis) if visit_id present
    visit_id = body.get("visit_id")
    if visit_id:
        supabase.table("visits").update({
            "chief_complaint": body.get("chief_complaint", ""),
            "diagnosis":       body.get("diagnosis", ""),
            "notes":           body.get("notes", ""),
        }).eq("id", visit_id).execute()

    # 3. Replace medicines: delete all, re-insert
    supabase.table("prescription_medicines").delete().eq("prescription_id", prescription_id).execute()
    medicines = body.get("medicines", [])
    med_rows = []
    for i, m in enumerate(medicines):
        if not m.get("medicine_name", "").strip():
            continue
        med_rows.append({
            "prescription_id": prescription_id,
            "medicine_name":   m["medicine_name"],
            "dosage":          m.get("dosage", ""),
            "morning":         m.get("morning", False),
            "afternoon":       m.get("afternoon", False),
            "evening":         m.get("evening", False),
            "night":           m.get("night", False),
            "before_food":     m.get("before_food", False),
            "duration_days":   m.get("duration_days", 5),
            "instructions":    m.get("instructions", ""),
            "sort_order":      m.get("sort_order", i + 1),
        })
    if med_rows:
        supabase.table("prescription_medicines").insert(med_rows).execute()

    # 4. Fetch patient info and send WhatsApp with updated prescription
    whatsapp_sent = False
    try:
        import datetime as dt, pytz
        pat_res = supabase.table("patients").select("name, mobile, patient_code, language").eq("id", body.get("patient_id", "")).execute()
        patient = pat_res.data[0] if pat_res.data else {}
        mobile   = patient.get("mobile", "")
        pname    = patient.get("name", "Patient")
        pcode    = patient.get("patient_code", "")
        language = patient.get("language", "english")
        diagnosis = body.get("diagnosis", "")
        dietary   = body.get("dietary_instructions", "")
        precautions = body.get("precautions", "")

        IST = pytz.timezone("Asia/Kolkata")
        now_ist = dt.datetime.now(IST)

        timing_icons = {"morning": "🌅", "afternoon": "☀️", "evening": "🌆", "night": "🌙"}
        timing_labels_en = {"morning": "Morning", "afternoon": "Afternoon", "evening": "Evening", "night": "Night"}
        timing_labels_ta = {"morning": "காலை", "afternoon": "மதியம்", "evening": "மாலை", "night": "இரவு"}

        def med_line(m, lang, idx):
            timings_keys = [k for k in ["morning", "afternoon", "evening", "night"] if m.get(k)]
            icons = " + ".join(timing_icons[k] for k in timings_keys)
            if lang == "tamil":
                labels = " + ".join(timing_labels_ta[k] for k in timings_keys)
                food = "சாப்பிடுவதற்கு முன்" if m.get("before_food") else "சாப்பிட்ட பின்"
                dur = f"{m.get('duration_days', 5)} நாட்கள்"
            else:
                labels = " + ".join(timing_labels_en[k] for k in timings_keys)
                food = "Before food" if m.get("before_food") else "After food"
                dur = f"{m.get('duration_days', 5)} days"
            inst = f"\n   {m['instructions']}" if m.get("instructions") else ""
            return f"{idx}. {m['medicine_name']} — {m.get('dosage','')}\n   {icons} {labels} | {food} | {dur}{inst}"

        valid_meds = [m for m in medicines if m.get("medicine_name", "").strip()]
        med_lines = "\n\n".join(med_line(m, language, i+1) for i, m in enumerate(valid_meds))

        if language == "tamil":
            msg = (
                f"💊 *மருந்துச் சீட்டு (புதுப்பிக்கப்பட்டது)*\n"
                f"🏥 Dr. Kumar Child Care Clinic\n\n"
                f"நோயாளி: {pname}" + (f" ({pcode})" if pcode else "") + f"\n"
                f"தேதி: {now_ist.strftime('%d %b %Y')}\n"
                f"நோய்: {diagnosis}\n\n"
                f"மருந்துகள்:\n{med_lines}"
            )
            if dietary:   msg += f"\n\n🥗 உணவு: {dietary}"
            if precautions: msg += f"\n⚠️ எச்சரிக்கை: {precautions}"
            msg += f"\n\nFollow-up: 3 நாட்களில் வரவும்."
        else:
            msg = (
                f"💊 *Updated Prescription*\n"
                f"🏥 Dr. Kumar Child Care Clinic\n\n"
                f"Patient: {pname}" + (f" ({pcode})" if pcode else "") + f"\n"
                f"Date: {now_ist.strftime('%d %b %Y')}\n"
                f"Diagnosis: {diagnosis}\n\n"
                f"Medicines:\n{med_lines}"
            )
            if dietary:     msg += f"\n\n🥗 Diet: {dietary}"
            if precautions: msg += f"\n⚠️ Precautions: {precautions}"
            msg += f"\n\nFollow-up in 3 days if not improving.\nReply MENU for any help."

        if mobile:
            from scheduler import send_whatsapp as _wa
            _wa(mobile, msg)
            whatsapp_sent = True

    except Exception as we:
        print(f"⚠️ WhatsApp send error on update: {we}")

    return {"ok": True, "prescription_id": prescription_id, "whatsapp_sent": whatsapp_sent}


@app.get("/prescriptions/active")
async def active_prescriptions(doctor_id: str):
    from database import supabase
    # prescriptions joins via visit_id → visits; filter by doctor_id directly on prescriptions
    result = supabase.table("prescriptions").select("*, patients(name, mobile, patient_code), prescription_medicines(*)").eq("doctor_id", doctor_id).order("created_at", desc=True).execute()
    return result.data or []


def _decode_walkin(row: dict) -> dict:
    """If general_notes has a WALKIN:: prefix, decode and inject walkin fields."""
    import json as _json, re as _re
    notes = row.get("general_notes") or ""
    m = _re.match(r"^WALKIN::(.+?)::END\n?(.*)", notes, _re.DOTALL)
    if m:
        try:
            meta = _json.loads(m.group(1))
            row["walkin_name"] = meta.get("name") or ""
            row["walkin_age"]  = meta.get("age")
            row["walkin_complaint"] = meta.get("complaint") or ""
            row["walkin_diagnosis"] = meta.get("diagnosis") or ""
            row["general_notes"] = m.group(2)  # strip prefix from notes
        except Exception:
            pass
    return row


@app.get("/prescriptions")
async def list_prescriptions(doctor_id: str, patient_id: str = ""):
    from database import supabase
    q = supabase.table("prescriptions").select("*, patients(name, mobile, patient_code), prescription_medicines(*)").eq("doctor_id", doctor_id)
    if patient_id:
        q = q.eq("patient_id", patient_id)
    result = q.order("created_at", desc=True).execute()
    return [_decode_walkin(r) for r in (result.data or [])]


@app.post("/prescriptions")
async def create_prescription_v2(request: Request):
    from database import supabase as db
    import datetime as dt
    import pytz

    body = await request.json()
    doctor_id_req    = body.get("doctor_id", "8c33abe0-5d2e-4613-9437-c7c375e8d162")
    patient_id       = body.get("patient_id") or None
    visit_id_req     = body.get("visit_id") or None
    appointment_id   = body.get("appointment_id") or None
    is_walkin        = body.get("is_walkin", False)
    walkin_name      = body.get("walkin_name") or None
    walkin_age       = body.get("walkin_age") or None
    chief_complaint  = body.get("chief_complaint", "")
    dietary          = body.get("dietary_instructions", "")
    precautions      = body.get("precautions", "")
    notes            = body.get("general_notes", "")
    medicines_input  = body.get("medicines", [])

    IST = pytz.timezone("Asia/Kolkata")
    now_ist = dt.datetime.now(IST)
    today_str = now_ist.date().isoformat()

    import json as _json
    diagnosis = body.get("diagnosis", "")

    # For walk-in: encode name/age/complaint/diagnosis into general_notes since no patient row
    if is_walkin:
        meta = _json.dumps({"__walkin": True, "name": walkin_name or "", "age": walkin_age, "complaint": chief_complaint, "diagnosis": diagnosis}, ensure_ascii=False)
        notes = f"WALKIN::{meta}::END\n{notes}" if notes else f"WALKIN::{meta}::END"

    # Auto-create visit if patient linked and no visit provided
    visit_id = visit_id_req
    if patient_id and not visit_id:
        visit_res = db.table("visits").insert({
            "patient_id":      patient_id,
            "doctor_id":       doctor_id_req,
            "appointment_id":  appointment_id,
            "chief_complaint": chief_complaint,
            "diagnosis":       diagnosis,
            "visit_status":    "Completed",
            "visit_date":      today_str,
            "created_at":      now_ist.isoformat(),
        }).execute()
        visit_id = visit_res.data[0]["id"] if visit_res.data else None

    pres_data = {
        "doctor_id":            doctor_id_req,
        "patient_id":           patient_id,
        "visit_id":             visit_id,
        "prescription_date":    today_str,
        "dietary_instructions": dietary,
        "precautions":          precautions,
        "general_notes":        notes,
        "whatsapp_sent":        False,
        "created_at":           now_ist.isoformat(),
    }
    pres_res = db.table("prescriptions").insert(pres_data).execute()
    pres = pres_res.data[0] if pres_res.data else {}
    pres_id = pres.get("id", "")

    med_rows = []
    for i, m in enumerate(medicines_input):
        if not m.get("medicine_name", "").strip():
            continue
        med_rows.append({
            "prescription_id": pres_id,
            "medicine_name":   m["medicine_name"],
            "dosage":          m.get("dosage", ""),
            "morning":         m.get("morning", False),
            "afternoon":       m.get("afternoon", False),
            "evening":         m.get("evening", False),
            "night":           m.get("night", False),
            "before_food":     m.get("before_food", False),
            "duration_days":   m.get("duration_days", 5),
            "instructions":    m.get("instructions", ""),
            "sort_order":      m.get("sort_order", i + 1),
        })
    if med_rows:
        db.table("prescription_medicines").insert(med_rows).execute()

    # Auto followup if patient linked
    if patient_id:
        followup_date = (now_ist.date() + dt.timedelta(days=7)).isoformat()
        try:
            db.table("followups").insert({
                "patient_id":     patient_id,
                "doctor_id":      doctor_id_req,
                "visit_id":       visit_id,
                "scheduled_date": followup_date,
                "channel":        "whatsapp",
                "call_status":    "Pending",
                "followup_day":   7,
            }).execute()
        except Exception:
            pass

    return {"prescription_id": pres_id}


@app.post("/prescriptions/{prescription_id}/send-whatsapp")
async def send_prescription_whatsapp(prescription_id: str):
    from database import supabase as db
    import datetime as dt
    import pytz

    try:
        pres_res = db.table("prescriptions").select("*").eq("id", prescription_id).execute()
        if not pres_res.data:
            raise HTTPException(status_code=404, detail="Prescription not found")
        pres = pres_res.data[0]

        patient_id = pres.get("patient_id")
        if not patient_id:
            raise HTTPException(status_code=400, detail="No patient linked to this prescription")

        pat_res = db.table("patients").select("name, mobile, patient_code, language").eq("id", patient_id).execute()
        if not pat_res.data:
            raise HTTPException(status_code=404, detail="Patient not found")
        pat = pat_res.data[0]

        name = pat.get("name", "")
        mobile = (pat.get("mobile") or "").lstrip("+")
        pcode = pat.get("patient_code", "")
        language = (pat.get("language") or "english").lower()

        meds_res = db.table("prescription_medicines").select("*").eq("prescription_id", prescription_id).execute()
        medicines = meds_res.data or []

        dietary = pres.get("dietary_instructions", "")
        precautions_text = pres.get("precautions", "")

        IST = pytz.timezone("Asia/Kolkata")
        now_ist = dt.datetime.now(IST)
        pdate = pres.get("prescription_date", now_ist.strftime("%Y-%m-%d"))
        try:
            pdate_fmt = dt.datetime.strptime(pdate, "%Y-%m-%d").strftime("%d %b %Y")
        except Exception:
            pdate_fmt = pdate

        timing_icons = {"morning": "🌅", "afternoon": "☀️", "evening": "🌆", "night": "🌙"}
        timing_labels_en = {"morning": "Morning", "afternoon": "Afternoon", "evening": "Evening", "night": "Night"}
        timing_labels_ta = {"morning": "காலை", "afternoon": "மதியம்", "evening": "மாலை", "night": "இரவு"}

        def med_line(m, lang, idx):
            timings_keys = [k for k in ["morning", "afternoon", "evening", "night"] if m.get(k)]
            icons = " + ".join(timing_icons[k] for k in timings_keys)
            if lang == "tamil":
                labels = " + ".join(timing_labels_ta[k] for k in timings_keys)
                food = "சாப்பிடுவதற்கு முன்" if m.get("before_food") else "சாப்பிட்ட பின்"
                dur = f"{m.get('duration_days', 5)} நாட்கள்"
            else:
                labels = " + ".join(timing_labels_en[k] for k in timings_keys)
                food = "Before food" if m.get("before_food") else "After food"
                dur = f"{m.get('duration_days', 5)} days"
            inst = f"\n   {m['instructions']}" if m.get("instructions") else ""
            return f"{idx}. {m['medicine_name']} — {m.get('dosage', '')}\n   {icons} {labels} | {food} | {dur}{inst}"

        med_lines = "\n\n".join(
            med_line(m, language, i + 1)
            for i, m in enumerate(medicines)
            if m.get("medicine_name", "").strip()
        )

        if language == "tamil":
            msg = (
                f"💊 *மருந்துச் சீட்டு*\n"
                f"🏥 Dr. Kumar Child Care Clinic\n\n"
                f"நோயாளி: {name}" + (f" ({pcode})" if pcode else "") + f"\n"
                f"தேதி: {pdate_fmt}\n\n"
                f"மருந்துகள்:\n{med_lines}"
            )
            if dietary:
                msg += f"\n\n🥗 உணவு: {dietary}"
            if precautions_text:
                msg += f"\n⚠️ எச்சரிக்கை: {precautions_text}"
            msg += f"\n\nFollow-up: 7 நாட்களில் வரவும்.\nகேள்விகளுக்கு MENU என்று reply பண்ணுங்கள்."
        else:
            msg = (
                f"💊 *Your Prescription*\n"
                f"🏥 Dr. Kumar Child Care Clinic\n\n"
                f"Patient: {name}" + (f" ({pcode})" if pcode else "") + f"\n"
                f"Date: {pdate_fmt}\n\n"
                f"Medicines:\n{med_lines}"
            )
            if dietary:
                msg += f"\n\n🥗 Diet: {dietary}"
            if precautions_text:
                msg += f"\n⚠️ Precautions: {precautions_text}"
            msg += f"\n\nFollow-up in 7 days.\nReply MENU for any help."

        from scheduler import send_whatsapp as _wa
        _wa(mobile, msg)

        db.table("prescriptions").update({"whatsapp_sent": True}).eq("id", prescription_id).execute()

        return {"sent": True, "mobile": mobile}

    except HTTPException:
        raise
    except Exception as e:
        return {"sent": False, "error": str(e)}


# ── FOLLOW-UPS ────────────────────────────────────────────
@app.get("/followups/pending")
async def pending_followups(doctor_id: str):
    from database import supabase
    # followups table (no underscore); pending = completed_at is null
    result = supabase.table("followups").select("*, patients(name, mobile, language)").eq("doctor_id", doctor_id).is_("completed_at", "null").order("created_at", desc=True).execute()
    return result.data or []


@app.get("/followups")
async def list_followups(doctor_id: str):
    from database import supabase
    result = supabase.table("followups").select("*, patients(name, mobile, language)").eq("doctor_id", doctor_id).order("created_at", desc=True).execute()
    return result.data or []


# ── QUERIES ───────────────────────────────────────────────
@app.get("/queries/pending")
async def pending_queries(doctor_id: str):
    from database import supabase
    # table is "queries", not "patient_queries"
    result = supabase.table("queries").select("*, patients(name, mobile, patient_code, age, gender, language, created_at)").eq("doctor_id", doctor_id).eq("status", "Pending").order("created_at", desc=True).execute()
    return result.data or []


@app.get("/queries")
async def list_queries(doctor_id: str, patient_id: str = ""):
    from database import supabase
    q = supabase.table("queries").select("*, patients(name, mobile, patient_code, age, gender, language, created_at)").eq("doctor_id", doctor_id)
    if patient_id:
        q = q.eq("patient_id", patient_id)
    result = q.order("created_at", desc=True).execute()
    return result.data or []


@app.patch("/queries/{query_id}/answer")
async def answer_query(query_id: str, request: Request):
    from database import supabase
    import datetime as dt
    body = await request.json()
    reply_text = body["answer"]
    # replied_by is a UUID column — fetch doctor_id, patient_id and original question
    q_row = supabase.table("queries").select("doctor_id, patient_id, question").eq("id", query_id).execute()
    doctor_id = q_row.data[0]["doctor_id"] if q_row.data else None
    patient_id = q_row.data[0]["patient_id"] if q_row.data else None
    question_text = q_row.data[0]["question"] if q_row.data else ""
    update = {
        "reply": reply_text,
        "status": "Closed",
        "replied_at": dt.datetime.utcnow().isoformat(),
    }
    if doctor_id:
        update["replied_by"] = doctor_id
    result = supabase.table("queries").update(update).eq("id", query_id).execute()

    # Send WhatsApp notification to patient (non-blocking — DB save already succeeded)
    try:
        if patient_id:
            pat = supabase.table("patients").select("mobile, patient_code").eq("id", patient_id).execute()
            mobile = pat.data[0]["mobile"] if pat.data else None
            patient_code = pat.data[0].get("patient_code", "") if pat.data else ""
            if mobile:
                msg = (
                    f"👨‍⚕️ *Dr. Kumar Child Care Clinic*\n\n"
                    f"Patient: *{patient_code}*\n\n"
                    f"Dr. Kumar has replied to your question:\n\n"
                    f"*Your question:* _{question_text}_\n"
                    f"*Dr. Kumar's reply:* _{reply_text}_\n\n"
                    f"Reply MENU for main menu."
                )
                send_whatsapp(mobile, msg)
                print(f"✅ WhatsApp reply sent for query {query_id} to {mobile}")
            else:
                print(f"⚠️ No mobile found for patient {patient_id}, skipping WhatsApp")
    except Exception as e:
        print(f"❌ WhatsApp reply failed for query {query_id}: {e}")

    return result.data[0] if result.data else {}


# ── REVIEWS ───────────────────────────────────────────────
@app.get("/reviews")
async def list_reviews(doctor_id: str):
    from database import supabase
    # table is "reviews" not "review_requests", sort by created_at
    result = supabase.table("reviews").select("*, patients(name, mobile)").eq("doctor_id", doctor_id).order("created_at", desc=True).execute()
    return result.data or []


# ── DOCTOR ────────────────────────────────────────────────
@app.get("/doctor/{doctor_id}")
async def get_doctor(doctor_id: str):
    from database import supabase
    result = supabase.table("doctors").select("*").eq("id", doctor_id).single().execute()
    return result.data


@app.patch("/doctor/{doctor_id}")
async def update_doctor(doctor_id: str, request: Request):
    from database import supabase
    body = await request.json()
    result = supabase.table("doctors").update(body).eq("id", doctor_id).execute()
    return result.data[0] if result.data else {}


# ── VOICE WEBHOOKS ────────────────────────────────────
@app.get("/webhook/voice/followup")
@app.post("/webhook/voice/followup")
async def voice_followup(request: Request):
    """Twilio voice webhook - plays follow-up audio"""
    from followup import handle_voice_followup_webhook
    return await handle_voice_followup_webhook(request)


@app.post("/webhook/voice/followup-response")
async def voice_followup_response(request: Request):
    """Twilio voice webhook - handles keypress"""
    from followup import handle_voice_followup_response
    return await handle_voice_followup_response(request)


# ── FOLLOW-UP TRIGGER ENDPOINTS ───────────────────────
@app.post("/trigger/followup-whatsapp")
async def trigger_followup_whatsapp():
    from followup import send_followup_whatsapp_job
    await send_followup_whatsapp_job()
    return {"status": "Follow-up WhatsApp sent"}


@app.post("/trigger/followup-calls")
async def trigger_followup_calls():
    from followup import make_followup_calls_job
    await make_followup_calls_job()
    return {"status": "Follow-up calls initiated"}



# ── APPOINTMENT SLOTS ─────────────────────────────────────

@app.get("/appointments/slots")
async def get_appointment_slots(doctor_id: str, date: str):
    """Return time slots with booking counts for a given date."""
    from database import supabase
    from datetime import datetime, timedelta

    # Load clinic config
    cfg_res = supabase.table("clinic_config")\
        .select("config_key,config_value")\
        .eq("doctor_id", doctor_id)\
        .in_("config_key", [
            "clinic.slot_start_morning", "clinic.slot_end_morning",
            "clinic.slot_start_evening", "clinic.slot_end_evening",
            "clinic.slot_duration_minutes", "clinic.max_per_slot",
        ]).execute()
    cfg = {r["config_key"]: r["config_value"] for r in (cfg_res.data or [])}

    start_m  = cfg.get("clinic.slot_start_morning",   "09:00")
    end_m    = cfg.get("clinic.slot_end_morning",     "13:00")
    start_e  = cfg.get("clinic.slot_start_evening",   "17:00")
    end_e    = cfg.get("clinic.slot_end_evening",     "20:00")
    dur      = int(cfg.get("clinic.slot_duration_minutes", "15"))
    max_slot = int(cfg.get("clinic.max_per_slot",          "3"))

    def parse_t(s):
        h, m_ = map(int, s.split(":"))
        return datetime(2000, 1, 1, h, m_)

    def gen_slots(start_str, end_str):
        slots = []
        t = parse_t(start_str)
        end = parse_t(end_str)
        while t < end:
            slots.append(t.strftime("%H:%M"))
            t += timedelta(minutes=dur)
        return slots

    all_slots = gen_slots(start_m, end_m) + gen_slots(start_e, end_e)

    # Count bookings for each slot
    appts_res = supabase.table("appointments")\
        .select("appointment_time")\
        .eq("doctor_id", doctor_id)\
        .eq("appointment_date", date)\
        .eq("status", "Confirmed")\
        .execute()
    booked_counts: dict[str, int] = {}
    for a in (appts_res.data or []):
        t = (a.get("appointment_time") or "")[:5]
        booked_counts[t] = booked_counts.get(t, 0) + 1

    def display_time(t_str):
        h, m_ = map(int, t_str.split(":"))
        suffix = "AM" if h < 12 else "PM"
        h12 = h if h <= 12 else h - 12
        if h12 == 0: h12 = 12
        return f"{h12}:{m_:02d} {suffix}"

    return [
        {
            "time":         slot,
            "display":      display_time(slot),
            "booked_count": booked_counts.get(slot, 0),
            "max":          max_slot,
            "available":    True,  # always allow (soft limit)
        }
        for slot in all_slots
    ]


@app.get("/appointments/next-token")
async def get_next_token(doctor_id: str, date: str):
    """Return the next token number for a given date."""
    from database import supabase
    res = supabase.table("appointments")\
        .select("token_number")\
        .eq("doctor_id", doctor_id)\
        .eq("appointment_date", date)\
        .execute()
    max_tok = max((r.get("token_number") or 0 for r in (res.data or [])), default=0)
    return {"token": max_tok + 1}


@app.post("/appointments/book")
async def book_appointment(request: Request):
    """Book an appointment, assign token, send WhatsApp confirmation."""
    from database import supabase
    import datetime as dt

    body          = await request.json()
    patient_id    = body["patient_id"]
    doctor_id     = body["doctor_id"]
    appt_date     = body["appointment_date"]
    appt_time     = body.get("appointment_time") or ""
    visit_type    = body.get("visit_type") or "New Visit"

    # Next token
    tok_res = supabase.table("appointments")\
        .select("token_number")\
        .eq("doctor_id", doctor_id)\
        .eq("appointment_date", appt_date)\
        .execute()
    token = max((r.get("token_number") or 0 for r in (tok_res.data or [])), default=0) + 1

    # Insert appointment
    appt_row = {
        "patient_id":        patient_id,
        "doctor_id":         doctor_id,
        "appointment_date":  appt_date,
        "appointment_time":  appt_time,
        "token_number":      token,
        "status":            "Confirmed",
    }
    ins = supabase.table("appointments").insert(appt_row).execute()
    if not ins.data:
        from fastapi import HTTPException
        raise HTTPException(status_code=500, detail="Appointment insert failed")
    appt_id = ins.data[0]["id"]

    # Get patient info
    pat_res = supabase.table("patients").select("name,mobile,patient_code,language").eq("id", patient_id).single().execute()
    patient = pat_res.data or {}
    pat_name  = patient.get("name") or "Patient"
    pat_code  = patient.get("patient_code") or ""
    pat_mob   = patient.get("mobile") or ""
    language  = (patient.get("language") or "English").lower()

    # Format date nicely
    try:
        d = dt.date.fromisoformat(appt_date)
        date_display = d.strftime("%d %b %Y")
    except Exception:
        date_display = appt_date

    # Format time nicely
    def fmt_time(t_str):
        if not t_str: return t_str
        try:
            h, m_ = map(int, t_str[:5].split(":"))
            suffix = "AM" if h < 12 else "PM"
            h12 = h if h <= 12 else h - 12
            if h12 == 0: h12 = 12
            return f"{h12}:{m_:02d} {suffix}"
        except Exception:
            return t_str

    time_display = fmt_time(appt_time)

    if language == "tamil":
        msg = (
            f"✅ சந்திப்பு உறுதிப்படுத்தப்பட்டது!\n\n"
            f"🏥 Dr. Kumar Child Care Clinic\n"
            f"👤 {pat_name} ({pat_code})\n"
            f"📅 {date_display}\n"
            f"⏰ {time_display} | Token #{token}\n\n"
            f"வரும்போது இந்த token number சொல்லுங்கள்.\n"
            f"ரத்து செய்ய CANCEL என்று reply பண்ணுங்கள்.\n"
            f"MENU — முகப்பு பக்கம்."
        )
    else:
        msg = (
            f"✅ Appointment Confirmed!\n\n"
            f"🏥 Dr. Kumar Child Care Clinic\n"
            f"👤 {pat_name} ({pat_code})\n"
            f"📅 {date_display}\n"
            f"⏰ {time_display} | Token #{token}\n\n"
            f"Please mention your token number when you arrive.\n"
            f"Reply CANCEL to cancel. Reply MENU for help."
        )

    # Send WhatsApp
    wa_sent = False
    if pat_mob:
        try:
            from scheduler import send_whatsapp as _wa
            _wa(pat_mob, msg)
            wa_sent = True
        except Exception as e:
            print(f"❌ WhatsApp booking confirmation failed: {e}")

    return {
        "appointment_id": appt_id,
        "token_number":   token,
        "patient_name":   pat_name,
        "whatsapp_sent":  wa_sent,
    }

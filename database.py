from supabase import create_client, Client
from dotenv import load_dotenv
import os

load_dotenv()

supabase: Client = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_SERVICE_KEY")
)


def get_doctor_by_whatsapp(whatsapp_number: str):
    """Get doctor by WhatsApp number - handles both +14155238886 and 14155238886"""
    clean = whatsapp_number.replace('+', '')
    _fields = "id, name, clinic_name, clinic_timings, clinic_address, mobile, online_consultation_enabled, online_consultation_hours"
    result = supabase.table("doctors").select(_fields).eq("whatsapp_number", clean).execute()
    if result.data:
        return result.data[0]
    result = supabase.table("doctors").select(_fields).eq("whatsapp_number", whatsapp_number).execute()
    return result.data[0] if result.data else None


def assign_online_token(doctor_id: str, appointment_date: str) -> int:
    """Return next O-token number for online appointments on a given date."""
    res = supabase.table("appointments").select("id")\
        .eq("doctor_id", doctor_id)\
        .eq("appointment_date", appointment_date)\
        .eq("consultation_type", "online")\
        .neq("status", "Cancelled")\
        .execute()
    return len(res.data or []) + 1


def get_patient_by_mobile(mobile: str):
    """Get primary patient by mobile number"""
    result = supabase.table("patients").select(
        "id, name, mobile, age, gender, date_of_birth"
    ).eq("mobile", mobile).eq("family_head_mobile", mobile).execute()
    return result.data[0] if result.data else None


def get_conversation_state(mobile: str):
    """Get conversation state for a patient"""
    result = supabase.table("conversation_state").select(
        "state, temp_data"
    ).eq("mobile", mobile).execute()
    if result.data:
        return result.data[0]["state"], result.data[0]["temp_data"] or {}
    return "idle", {}


def save_conversation_state(mobile: str, state: str, temp_data: dict):
    """Upsert conversation state"""
    supabase.rpc("upsert_conversation_state", {"p_mobile": mobile}).execute()
    supabase.table("conversation_state").update({
        "state": state,
        "temp_data": temp_data
    }).eq("mobile", mobile).execute()


def get_queue_status(doctor_id: str):
    """Get today's queue status"""
    from datetime import date
    today = date.today().isoformat()
    result = supabase.table("tokens").select(
        "current_token, total_tokens, avg_minutes_per_patient"
    ).eq("doctor_id", doctor_id).eq("queue_date", today).eq("is_active", True).execute()
    return result.data[0] if result.data else None


def get_patient_token_today(patient_id: str, doctor_id: str):
    """Get patient's token number for today (single patient, legacy)"""
    from datetime import date
    today = date.today().isoformat()
    result = supabase.table("appointments").select(
        "token_number, appointment_time"
    ).eq("patient_id", patient_id).eq("doctor_id", doctor_id).eq(
        "appointment_date", today
    ).eq("status", "Confirmed").neq("consultation_type", "online").execute()
    return result.data[0] if result.data else None


def get_family_tokens_today(mobile: str, doctor_id: str, in_progress_token: int):
    """
    Get all appointment tokens today for all patients sharing a mobile number.
    Returns list of {token_number, name, queue_status} sorted by token_number.
    queue_status: 'Waiting' | 'In Progress' | 'Done'
    """
    from datetime import date
    today = date.today().isoformat()

    # Find all patient IDs linked to this mobile (self + family members)
    own = supabase.table("patients").select("id, name").eq("mobile", mobile).execute()
    family = supabase.table("patients").select("id, name").eq("family_head_mobile", mobile).execute()
    all_patients = {p["id"]: p["name"] for p in (own.data or []) + (family.data or [])}

    if not all_patients:
        return []

    # Fetch today's confirmed appointments for all those patients
    appts = supabase.table("appointments").select(
        "patient_id, token_number"
    ).eq("doctor_id", doctor_id).eq("appointment_date", today).eq(
        "status", "Confirmed"
    ).neq("consultation_type", "online").in_("patient_id", list(all_patients.keys())).order("token_number").execute()

    tokens = []
    for a in (appts.data or []):
        t = a["token_number"]
        if t is None:
            continue
        if t < in_progress_token:
            status = "Done"
        elif t == in_progress_token:
            status = "In Progress"
        else:
            status = "Waiting"
        tokens.append({
            "token_number": t,
            "name": all_patients.get(a["patient_id"], "Patient"),
            "queue_status": status,
        })
    return tokens


def check_holiday(doctor_id: str, date_str: str):
    """Check if a date is a holiday"""
    result = supabase.table("doctor_holidays").select(
        "reason"
    ).eq("doctor_id", doctor_id).eq("holiday_date", date_str).execute()
    return result.data[0] if result.data else None


def get_booked_slots(doctor_id: str, date_str: str):
    """Get booked appointment times for a date"""
    result = supabase.table("appointments").select(
        "appointment_time"
    ).eq("doctor_id", doctor_id).eq("appointment_date", date_str).eq(
        "status", "Confirmed"
    ).neq("consultation_type", "online").execute()
    return [r["appointment_time"][:5] for r in result.data]


def get_next_token(doctor_id: str, date_str: str):
    """Get next token number for the day"""
    result = supabase.table("appointments").select(
        "token_number"
    ).eq("doctor_id", doctor_id).eq(
        "appointment_date", date_str
    ).eq("status", "Confirmed").order(
        "token_number", desc=True
    ).limit(1).execute()

    if result.data:
        return (result.data[0]["token_number"] or 0) + 1
    return 1


def _time_str(t) -> str:
    """Normalize appointment_time (str or datetime.time) to 'HH:MM:SS'."""
    if not t:
        return ""
    if isinstance(t, str):
        return t + ":00" if len(t) == 5 else t
    return t.strftime("%H:%M:%S")


_slot_cfg_cache = {"data": None, "ts": 0.0}


def get_slot_config() -> dict:
    """Clinic slot grid config (session starts + minutes per patient),
    cached for 5 minutes. Single-clinic deployment — no doctor filter."""
    import time as _time_mod
    if _slot_cfg_cache["data"] and _time_mod.time() - _slot_cfg_cache["ts"] < 300:
        return _slot_cfg_cache["data"]
    try:
        res = supabase.table("clinic_config").select("config_key, config_value").in_(
            "config_key", [
                "clinic.slot_start_morning",
                "clinic.slot_start_evening",
                "clinic.slot_duration_minutes",
            ]).execute()
        cfg = {r["config_key"]: r["config_value"] for r in (res.data or [])}
    except Exception:
        cfg = {}
    data = {
        "morning_start": cfg.get("clinic.slot_start_morning", "09:00"),
        "evening_start": cfg.get("clinic.slot_start_evening", "17:00"),
        "duration": int(cfg.get("clinic.slot_duration_minutes") or 15),
    }
    _slot_cfg_cache["data"] = data
    _slot_cfg_cache["ts"] = _time_mod.time()
    return data


def get_display_token(token_number, appointment_time, all_day_appointments=None) -> str:
    """Slot-position display token: every time slot maps to a FIXED token in its
    session — morning start → M1, next slot → M2 …, evening start → E1, etc.
    Position = (slot time − session start) / slot duration, so changing the
    per-patient minutes in clinic config re-maps tokens automatically.
    DB token_number stays an integer; this is display-only, never stored."""
    t = _time_str(appointment_time)
    if not t:
        return f"#{token_number}" if token_number else "?"

    is_evening = t >= "13:00:00"
    cfg = get_slot_config()
    start = cfg["evening_start"] if is_evening else cfg["morning_start"]
    prefix = "E" if is_evening else "M"

    def _mins(x):
        return int(x[:2]) * 60 + int(x[3:5])

    delta = _mins(t) - _mins(start)
    if delta < 0:
        return f"{prefix}?"
    return f"{prefix}{delta // cfg['duration'] + 1}"


def is_slot_available(doctor_id: str, appointment_date: str, appointment_time) -> bool:
    """A slot is free unless a Confirmed/In Progress/Completed appointment
    occupies it — only Cancelled frees the slot (a Completed slot reopening
    would mint duplicate display tokens)."""
    result = supabase.table("appointments").select("id").eq(
        "doctor_id", doctor_id
    ).eq("appointment_date", appointment_date).eq(
        "appointment_time", _time_str(appointment_time)
    ).in_("status", ["Confirmed", "In Progress", "Completed"]).neq("consultation_type", "online").execute()
    return len(result.data or []) == 0


def assign_token_for_slot(doctor_id: str, appointment_date: str, appointment_time) -> int:
    """Token to assign for this slot: reuse a cancelled appointment's token at the
    exact same slot, otherwise MAX(token_number)+1 among non-cancelled for the day."""
    cancelled = supabase.table("appointments").select("token_number").eq(
        "doctor_id", doctor_id
    ).eq("appointment_date", appointment_date).eq(
        "appointment_time", _time_str(appointment_time)
    ).eq("status", "Cancelled").limit(1).execute()
    if cancelled.data and cancelled.data[0].get("token_number"):
        return cancelled.data[0]["token_number"]

    # Include cancelled rows so new tokens never collide with existing token numbers
    existing = supabase.table("appointments").select("token_number").eq(
        "doctor_id", doctor_id
    ).eq("appointment_date", appointment_date).order(
        "token_number", desc=True
    ).limit(1).execute()
    if existing.data:
        return (existing.data[0].get("token_number") or 0) + 1
    return 1


def get_active_appointment(patient_id: str, doctor_id: str, date_str: str):
    """The patient's existing Confirmed/In Progress appointment on a date, if any.
    Used to enforce one active appointment per patient per day."""
    result = supabase.table("appointments").select(
        "id, appointment_time, token_number, status"
    ).eq("patient_id", patient_id).eq("doctor_id", doctor_id).eq(
        "appointment_date", date_str
    ).in_("status", ["Confirmed", "In Progress"]).limit(1).execute()
    return result.data[0] if result.data else None


def ensure_queue_session(doctor_id: str, queue_date: str):
    """Create the day's tokens session row on first booking, bump total_tokens after.
    Emulates: INSERT ... ON CONFLICT (doctor_id, queue_date) DO UPDATE total_tokens+1"""
    try:
        existing = supabase.table("tokens").select("total_tokens").eq(
            "doctor_id", doctor_id).eq("queue_date", queue_date).execute()
        if existing.data:
            supabase.table("tokens").update({
                "total_tokens": (existing.data[0].get("total_tokens") or 0) + 1,
                "is_active": True
            }).eq("doctor_id", doctor_id).eq("queue_date", queue_date).execute()
        else:
            supabase.table("tokens").insert({
                "doctor_id": doctor_id,
                "queue_date": queue_date,
                "current_token": 0,
                "total_tokens": 1,
                "is_active": True
            }).execute()
    except Exception as e:
        print(f"⚠️ ensure_queue_session failed for {queue_date}: {e}")


def create_appointment(patient_id: str, doctor_id: str, date_str: str,
                       time_str: str, token: int, booking_source: str = "whatsapp"):
    """Create a new appointment. If a cancelled row occupies this exact slot,
    reuse it via UPDATE (recycles its token, avoids unique-constraint clashes)."""
    t = _time_str(time_str)

    if t:
        cancelled = supabase.table("appointments").select("id").eq(
            "doctor_id", doctor_id
        ).eq("appointment_date", date_str).eq(
            "appointment_time", t
        ).eq("status", "Cancelled").limit(1).execute()
        if cancelled.data:
            result = supabase.table("appointments").update({
                "patient_id": patient_id,
                "status": "Confirmed",
                "booking_source": booking_source,
                "cancellation_reason": None,
            }).eq("id", cancelled.data[0]["id"]).execute()
            return result.data[0] if result.data else None

    result = supabase.table("appointments").insert({
        "patient_id": patient_id,
        "doctor_id": doctor_id,
        "appointment_date": date_str,
        "appointment_time": t,
        "token_number": token,
        "status": "Confirmed",
        "booking_source": booking_source
    }).execute()
    ensure_queue_session(doctor_id, date_str)
    return result.data[0] if result.data else None


def create_online_appointment(patient_id: str, doctor_id: str, date_str: str,
                              time_str: str, booking_source: str = "whatsapp"):
    """Create an online consultation appointment. token_number is NULL to avoid
    the unique_token_per_doctor_date constraint (O-tokens are computed at display time)."""
    t = _time_str(time_str)
    result = supabase.table("appointments").insert({
        "patient_id": patient_id,
        "doctor_id": doctor_id,
        "appointment_date": date_str,
        "appointment_time": t,
        "token_number": None,
        "consultation_type": "online",
        "status": "Confirmed",
        "booking_source": booking_source,
    }).execute()
    ensure_queue_session(doctor_id, date_str)
    return result.data[0] if result.data else None


def get_upcoming_appointments(patient_id: str, doctor_id: str):
    """Get upcoming appointments for a single patient (legacy)"""
    from datetime import date
    today = date.today().isoformat()
    result = supabase.table("appointments").select(
        "id, appointment_date, appointment_time, token_number, status"
    ).eq("patient_id", patient_id).eq("doctor_id", doctor_id).eq(
        "status", "Confirmed"
    ).gte("appointment_date", today).order("appointment_date").limit(5).execute()
    return result.data


def get_family_upcoming_appointments(mobile: str, doctor_id: str):
    """
    Get cancellable appointments for every patient linked to this mobile.
    - Today: only Waiting (token > in_progress). Done/In Progress are already seen.
    - Future dates: all Confirmed appointments.
    Returns list with 'patient_name' key, sorted by date/token.
    """
    from datetime import date
    today = date.today().isoformat()

    own    = supabase.table("patients").select("id, name").eq("mobile", mobile).execute()
    family = supabase.table("patients").select("id, name").eq("family_head_mobile", mobile).execute()
    all_patients = {p["id"]: p["name"] for p in (own.data or []) + (family.data or [])}

    if not all_patients:
        return []

    result = supabase.table("appointments").select(
        "id, patient_id, appointment_date, appointment_time, token_number, status"
    ).eq("doctor_id", doctor_id).eq("status", "Confirmed").gte(
        "appointment_date", today
    ).in_("patient_id", list(all_patients.keys())).order("appointment_date").order(
        "appointment_time"
    ).limit(10).execute()

    # Today's serving slot time — appointments at or before it can't be cancelled
    token_row = supabase.table("tokens").select("current_token").eq(
        "doctor_id", doctor_id).eq("queue_date", today).execute()
    current = token_row.data[0]["current_token"] if token_row.data else 0
    serving_time = ""
    if current:
        srow = supabase.table("appointments").select("appointment_time").eq(
            "doctor_id", doctor_id).eq("appointment_date", today).eq(
            "token_number", current).limit(1).execute()
        serving_time = _time_str(srow.data[0]["appointment_time"]) if srow.data else ""

    appts = []
    for a in (result.data or []):
        # For today: skip slots already seen or being seen
        if a["appointment_date"] == today and serving_time:
            if _time_str(a.get("appointment_time")) <= serving_time:
                continue
        a["patient_name"] = all_patients.get(a["patient_id"], "Patient")
        appts.append(a)
    return appts


def cancel_appointment(appointment_id: str):
    """Cancel an appointment and its linked online consultation if any."""
    supabase.table("appointments").update({
        "status": "Cancelled"
    }).eq("id", appointment_id).execute()
    # Cancel linked consultation (online bookings auto-create a consultations row)
    supabase.table("consultations").update({
        "status": "cancelled"
    }).eq("appointment_id", appointment_id).in_("status", ["scheduled", "waiting"]).execute()


def _next_patient_counter(doctor_id: str) -> int:
    """Return next sequential patient number for this clinic."""
    res = supabase.table("patients").select("id", count="exact")\
        .eq("doctor_id", doctor_id).execute()
    return (res.count or 0) + 1


def create_patient(mobile: str, name: str, dob: str, gender: str,
                   family_head_mobile: str = None, language: str = "english",
                   city: str = "", doctor_id: str = ""):
    """Create a new patient with auto-calculated age and patient_code (NAME3-YEAR-COUNTER)."""
    from datetime import date, datetime

    age = None
    dob_iso = None
    birth_year = "0000"

    try:
        dob_date = datetime.strptime(dob, "%d %B %Y").date()
        today = date.today()
        age = today.year - dob_date.year - (
            (today.month, today.day) < (dob_date.month, dob_date.day)
        )
        dob_iso = dob_date.isoformat()
        birth_year = str(dob_date.year)
    except Exception:
        pass

    name_part = name[:3].upper().replace(" ", "")
    counter = _next_patient_counter(doctor_id) if doctor_id else 1
    patient_code = f"{name_part}-{birth_year}-{counter}"

    fhm = family_head_mobile if family_head_mobile else mobile

    row = {
        "mobile": mobile,
        "whatsapp_number": mobile,
        "name": name,
        "date_of_birth": dob_iso,
        "age": age,
        "gender": gender,
        "patient_code": patient_code,
        "family_head_mobile": fhm,
        "language": language,
        "city": city,
        "registration_source": "whatsapp",
    }
    if doctor_id:
        row["doctor_id"] = doctor_id

    result = supabase.table("patients").insert(row).execute()
    return result.data[0] if result.data else None

def get_patient_by_mobile(mobile: str):
    """Get primary patient by mobile number"""
    result = supabase.table("patients").select(
        "id, name, mobile, age, gender, date_of_birth"
    ).eq("mobile", mobile).eq("family_head_mobile", mobile).execute()
    
    if result.data:
        return result.data[0]
    
    # Fallback: find by mobile only (for patients without family_head_mobile set)
    result2 = supabase.table("patients").select(
        "id, name, mobile, age, gender, date_of_birth"
    ).eq("mobile", mobile).is_("family_head_mobile", "null").execute()
    
    return result2.data[0] if result2.data else None

def get_family_members(mobile: str):
    """Get all patients under this mobile number"""
    result = supabase.table("patients").select(
        "id, name, age, gender"
    ).or_(
        f"mobile.eq.{mobile},family_head_mobile.eq.{mobile}"
    ).execute()
    return result.data if result.data else []

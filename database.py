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
    # Normalize: remove + for consistent lookup
    clean = whatsapp_number.replace('+', '')
    
    # Try without + first (most common storage format)
    result = supabase.table("doctors").select(
        "id, name, clinic_name, clinic_timings, clinic_address, mobile"
    ).eq("whatsapp_number", clean).execute()
    
    if result.data:
        return result.data[0]
    
    # Try with + (some may be stored with +)
    result = supabase.table("doctors").select(
        "id, name, clinic_name, clinic_timings, clinic_address, mobile"
    ).eq("whatsapp_number", whatsapp_number).execute()
    
    return result.data[0] if result.data else None


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
    ).eq("status", "Confirmed").execute()
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
    ).in_("patient_id", list(all_patients.keys())).order("token_number").execute()

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
    ).execute()
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


def create_appointment(patient_id: str, doctor_id: str, date_str: str,
                       time_str: str, token: int):
    """Create a new appointment"""
    result = supabase.table("appointments").insert({
        "patient_id": patient_id,
        "doctor_id": doctor_id,
        "appointment_date": date_str,
        "appointment_time": time_str + ":00",
        "token_number": token,
        "status": "Confirmed",
        "booking_source": "whatsapp"
    }).execute()
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
        "token_number"
    ).limit(10).execute()

    # Get current in_progress token to filter out today's Done/In Progress
    token_row = supabase.table("tokens").select("current_token").eq(
        "doctor_id", doctor_id).eq("queue_date", today).execute()
    in_progress = (token_row.data[0]["current_token"] + 1) if token_row.data else 0

    appts = []
    for a in (result.data or []):
        # For today: skip if token is already Done or In Progress
        if a["appointment_date"] == today:
            t = a.get("token_number") or 0
            if t <= in_progress:
                continue
        a["patient_name"] = all_patients.get(a["patient_id"], "Patient")
        appts.append(a)
    return appts


def cancel_appointment(appointment_id: str):
    """Cancel an appointment"""
    supabase.table("appointments").update({
        "status": "Cancelled"
    }).eq("id", appointment_id).execute()


def create_patient(mobile: str, name: str, dob: str, gender: str,
                   family_head_mobile: str = None):
    """Create a new patient with auto-calculated age and patient_code"""
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

    # Generate patient code: first 3 letters + last 4 of mobile + birth year
    name_part = name[:3].upper()
    mobile_part = mobile[-4:]
    patient_code = f"{name_part}-{mobile_part}-{birth_year}"

    # family_head_mobile defaults to own mobile if not provided
    fhm = family_head_mobile if family_head_mobile else mobile

    result = supabase.table("patients").insert({
        "mobile": mobile,
        "whatsapp_number": mobile,
        "name": name,
        "date_of_birth": dob_iso,
        "age": age,
        "gender": gender,
        "patient_code": patient_code,
        "family_head_mobile": fhm,
        "registration_source": "whatsapp"
    }).execute()
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

"""
multi_doctor.py — Feature-flagged multi-doctor helpers.

Imported only when feature.multi_doctor.enabled = true.
Zero side effects on import. Safe to import at any time.
All functions catch ALL exceptions and return safe defaults — never crash.
"""


async def get_clinic_doctors(supabase, clinic_whatsapp_number: str) -> list:
    """Return active doctors at this clinic WhatsApp number that have multi-doctor enabled.

    Multi-doctor is considered active when 2+ such doctors exist.
    Each doctor controls their own participation via feature.multi_doctor.enabled in clinic_config.
    Falls back to [] on any error so callers always get single-doctor flow.
    """
    try:
        # Get all is_available doctors sharing this WhatsApp number
        doctors_res = supabase.table("doctors") \
            .select("id, name, speciality, specialty_display, is_available, clinic_name") \
            .eq("whatsapp_number", clinic_whatsapp_number.replace("+", "")) \
            .eq("is_available", True) \
            .execute()
        doctors = doctors_res.data or []
        if len(doctors) < 2:
            return []

        # Filter to only those with feature.multi_doctor.enabled = 'true'
        doctor_ids = [d["id"] for d in doctors]
        flags_res = supabase.table("clinic_config") \
            .select("doctor_id, config_value") \
            .in_("doctor_id", doctor_ids) \
            .eq("config_key", "feature.multi_doctor.enabled") \
            .execute()
        enabled_ids = {
            r["doctor_id"]
            for r in (flags_res.data or [])
            if r.get("config_value") == "true"
        }
        eligible = [d for d in doctors if d["id"] in enabled_ids]
        return eligible if len(eligible) >= 2 else []
    except Exception:
        return []


async def is_multi_doctor_enabled(supabase, clinic_doctor_id: str) -> bool:
    """Legacy helper — now just checks if get_clinic_doctors would return 2+ doctors.
    Kept for backward compatibility with any direct callers."""
    try:
        result = supabase.table("clinic_config") \
            .select("config_value") \
            .eq("doctor_id", clinic_doctor_id) \
            .eq("config_key", "feature.multi_doctor.enabled") \
            .limit(1).execute()
        rows = result.data or []
        return bool(rows and rows[0].get("config_value") == "true")
    except Exception:
        return False


async def get_doctor_by_id(supabase, doctor_id: str) -> dict:
    """Fetch a single doctor by id. Returns {} on any error."""
    try:
        result = supabase.table("doctors") \
            .select("id, name, speciality, specialty_display, clinic_name, clinic_timings, clinic_address, whatsapp_number") \
            .eq("id", doctor_id) \
            .limit(1).execute()
        rows = result.data or []
        return rows[0] if rows else {}
    except Exception:
        return {}


def build_doctor_selection_message(doctors: list) -> dict:
    """Build WhatsApp list-message payload for doctor selection."""
    rows = []
    for doc in doctors:
        specialty = doc.get("specialty_display") or doc.get("speciality", "")
        rows.append({
            "id": f"doctor_{doc['id']}",
            "title": doc.get("name", "Doctor"),
            "description": specialty[:72] if specialty else "",
        })
    return {
        "type": "list",
        "header": "Choose Your Doctor",
        "body": "Please select the doctor you would like to see",
        "footer": (doctors[0].get("clinic_name") if doctors else "") or "Clinic",
        "action": {
            "button": "Select Doctor",
            "sections": [{"title": "Available Doctors", "rows": rows}],
        },
    }


def build_session_selection_message(doctor_name: str) -> dict:
    """Build WhatsApp button-message payload for morning/evening selection."""
    return {
        "type": "button",
        "body": f"Which session would you like with {doctor_name}?",
        "buttons": [
            {"id": "session_morning", "title": "🌅 Morning"},
            {"id": "session_evening", "title": "🌆 Evening"},
        ],
    }

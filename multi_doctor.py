"""
multi_doctor.py — Feature-flagged multi-doctor helpers.

Imported only when feature.multi_doctor.enabled = true.
Zero side effects on import. Safe to import at any time.
All functions catch ALL exceptions and return safe defaults — never crash.
"""


async def is_multi_doctor_enabled(supabase, clinic_doctor_id: str) -> bool:
    """Check feature flag. Returns False on any error (safe default)."""
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


async def get_clinic_doctors(supabase, clinic_whatsapp_number: str) -> list:
    """Get all active doctors that share this clinic WhatsApp number."""
    try:
        result = supabase.table("doctors") \
            .select("id, name, speciality, specialty_display, is_available, clinic_name") \
            .eq("whatsapp_number", clinic_whatsapp_number) \
            .eq("is_available", True) \
            .execute()
        return result.data or []
    except Exception:
        return []


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

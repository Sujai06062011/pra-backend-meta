"""
consultation_helpers.py

Shared helpers for online consultation logic.
Imported by both main.py and whatsapp_handler.py — no circular deps.
"""

import os
import uuid as uuid_lib
import httpx
import pytz
from datetime import datetime

IST = pytz.timezone("Asia/Kolkata")

JAAS_APP_ID = os.getenv("JAAS_APP_ID", "")

META_API_VERSION    = os.getenv("META_API_VERSION", "v18.0")
META_PHONE_NUMBER_ID = os.getenv("META_PHONE_NUMBER_ID")
META_ACCESS_TOKEN   = os.getenv("META_ACCESS_TOKEN")
META_BASE_URL = (
    f"https://graph.facebook.com/{META_API_VERSION}"
    f"/{META_PHONE_NUMBER_ID}/messages"
)


# ── JaaS room helpers ──────────────────────────────────────────────────────────

def generate_room_id(doctor_name: str, scheduled_at: datetime) -> str:
    clean_name = (
        doctor_name.lower()
        .replace(" ", "-")
        .replace(".", "")
        .replace("dr-", "dr")[:15]
    )
    time_str = scheduled_at.strftime("%Y%m%d-%H%M%S")
    suffix = str(uuid_lib.uuid4())[:6]
    return f"{clean_name}-{time_str}-{suffix}"


def get_patient_join_url(room_id: str) -> str:
    return f"https://8x8.vc/{JAAS_APP_ID}/{room_id}"


# ── WhatsApp sender (standalone, no circular import) ──────────────────────────

async def send_meta_list(
    to_number: str,
    body_text: str,
    button_label: str,
    sections: list,
    header_text: str = None,
    footer_text: str = None,
) -> dict:
    """Send a WhatsApp interactive LIST message via Meta API."""
    url = (
        f"https://graph.facebook.com/{META_API_VERSION}"
        f"/{META_PHONE_NUMBER_ID}/messages"
    )
    interactive = {
        "type": "list",
        "body": {"text": body_text},
        "action": {"button": button_label, "sections": sections},
    }
    if header_text:
        interactive["header"] = {"type": "text", "text": header_text}
    if footer_text:
        interactive["footer"] = {"text": footer_text}
    payload = {
        "messaging_product": "whatsapp",
        "to": to_number.lstrip("+"),
        "type": "interactive",
        "interactive": interactive,
    }
    headers = {
        "Authorization": f"Bearer {META_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient() as client:
        r = await client.post(url, json=payload, headers=headers)
        print(f"[Meta LIST] to={to_number} status={r.status_code} body={r.text[:200]}")
        return r.json()


async def send_whatsapp_text(to_number: str, message: str):
    headers = {
        "Authorization": f"Bearer {META_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to_number.lstrip("+"),
        "type": "text",
        "text": {"body": message},
    }
    async with httpx.AsyncClient() as client:
        r = await client.post(META_BASE_URL, json=payload, headers=headers)
        print(f"[consultation_helpers] WhatsApp to={to_number} status={r.status_code}")
        return r.json()


# ── Online slot check ──────────────────────────────────────────────────────────

async def is_online_consultation_slot(
    supabase,
    doctor_id: str,
    appointment_date: str,   # "2026-06-17"
    appointment_time: str,   # "22:00" or "22:00:00"
) -> bool:
    try:
        doctor = (
            supabase.table("doctors")
            .select("online_consultation_enabled, online_consultation_hours")
            .eq("id", doctor_id)
            .single()
            .execute()
        )
        if not doctor.data:
            return False
        if not doctor.data.get("online_consultation_enabled"):
            return False
        hours = doctor.data.get("online_consultation_hours") or []
        if not hours:
            return False

        from datetime import date as date_type
        day_name  = date_type.fromisoformat(appointment_date).strftime("%A").lower()
        appt_time = appointment_time[:5]  # "22:00"

        for entry in hours:
            if entry.get("day", "").lower() != day_name:
                continue
            # New format: {day, morning:{enabled,start,end}, evening:{enabled,start,end}}
            if "morning" in entry or "evening" in entry:
                for sess_key in ["morning", "evening"]:
                    sess = entry.get(sess_key) or {}
                    if sess.get("enabled") and sess.get("start", "")[:5] <= appt_time <= sess.get("end", "")[:5]:
                        return True
            else:
                # Legacy format: {day, start, end}
                if entry.get("start", "")[:5] <= appt_time <= entry.get("end", "")[:5]:
                    return True
        return False
    except Exception as e:
        print(f"[is_online_consultation_slot error] {e}")
        return False


# ── Create video room for an appointment ──────────────────────────────────────

async def create_consultation_for_appointment(
    supabase,
    doctor_id: str,
    patient_id: str,
    appointment_id: str,
    appointment_date: str,
    appointment_time: str,
    chief_complaint: str = "",
) -> dict | None:
    try:
        doctor = (
            supabase.table("doctors")
            .select("name, clinic_name")
            .eq("id", doctor_id)
            .single()
            .execute()
        )
        scheduled_str = f"{appointment_date}T{appointment_time}"
        try:
            scheduled_at = IST.localize(
                datetime.strptime(scheduled_str, "%Y-%m-%dT%H:%M:%S")
            )
        except ValueError:
            scheduled_at = IST.localize(
                datetime.strptime(scheduled_str, "%Y-%m-%dT%H:%M")
            )

        room_id  = generate_room_id(doctor.data["name"], scheduled_at)
        room_url = get_patient_join_url(room_id)

        row = supabase.table("consultations").insert({
            "appointment_id":    appointment_id,
            "patient_id":        patient_id,
            "doctor_id":         doctor_id,
            "room_id":           room_id,
            "room_url":          room_url,
            "scheduled_at":      scheduled_at.isoformat(),
            "status":            "scheduled",
            "consultation_type": "online",
            "chief_complaint":   chief_complaint,
            "patient_link_sent": False,
        }).execute()

        supabase.table("appointments").update({"consultation_type": "online"}).eq(
            "id", appointment_id
        ).execute()

        print(f"[create_consultation] room={room_id} appt={appointment_id}")
        return {
            "consultation_id": row.data[0]["id"],
            "room_id":  room_id,
            "room_url": room_url,
        }
    except Exception as e:
        print(f"[create_consultation_for_appointment error] {e}")
        return None


# ── Build + send video link WhatsApp message ──────────────────────────────────

async def send_video_link_to_patient(
    mobile: str,
    room_url: str,
    appointment_time: str,   # "22:00" or "22:00:00"
    appointment_date: str = "",  # "2026-06-17"
    language: str = "english",
):
    try:
        time_obj = datetime.strptime(appointment_time[:5], "%H:%M")
        formatted_time = time_obj.strftime("%I:%M %p")
    except Exception:
        formatted_time = appointment_time[:5]

    try:
        from datetime import date as _date
        formatted_date = _date.fromisoformat(appointment_date).strftime("%d %b %Y")
    except Exception:
        formatted_date = appointment_date

    lang = (language or "english").lower()

    if lang == "tamil":
        msg = (
            f"🎥 இது ஆன்லைன் கன்சல்டேஷன்!\n\n"
            f"📅 {formatted_date} | ⏰ {formatted_time}\n\n"
            f"கீழே உள்ள லிங்கை கிளிக் செய்து join ஆகுங்கள்:\n\n"
            f"{room_url}\n\n"
            f"✅ Download தேவையில்லை\n"
            f"✅ Login தேவையில்லை\n"
            f"லிங்கை கிளிக் செய்தால் போதும்!"
        )
    elif lang == "hindi":
        msg = (
            f"🎥 यह ऑनलाइन कंसल्टेशन है!\n\n"
            f"📅 {formatted_date} | ⏰ {formatted_time}\n\n"
            f"नीचे दिए लिंक पर क्लिक करें:\n\n"
            f"{room_url}\n\n"
            f"✅ कोई डाउनलोड नहीं\n"
            f"✅ कोई लॉगिन नहीं\n"
            f"बस लिंक पर क्लिक करें!"
        )
    else:
        msg = (
            f"🎥 This is an Online Consultation!\n\n"
            f"📅 {formatted_date} | ⏰ {formatted_time}\n\n"
            f"Click the link below to join:\n\n"
            f"{room_url}\n\n"
            f"✅ No download needed\n"
            f"✅ No login required\n"
            f"✅ Just click the link at appointment time!"
        )

    await send_whatsapp_text(mobile, msg)
    print(f"[send_video_link_to_patient] sent to {mobile}")

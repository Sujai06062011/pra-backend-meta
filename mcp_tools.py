"""
mcp_tools.py — Shared MCP tool definitions for Parro Connect clinic.

Used by:
  parro_mcp.py  → stdio transport (local Claude Desktop)
  main.py       → HTTP transport (Railway / remote AI)
"""

import json
import uuid
from datetime import date, datetime, timedelta

import pytz
from mcp.server import Server
from mcp.types import Tool, TextContent

IST = pytz.timezone("Asia/Kolkata")
DEFAULT_DOCTOR_ID = "8c33abe0-5d2e-4613-9437-c7c375e8d162"


def normalize_mobile(mobile: str) -> str:
    """Normalize mobile — prepend 91 for bare 10-digit Indian numbers."""
    m = mobile.lstrip("+").replace(" ", "").replace("-", "")
    if len(m) == 10 and m.isdigit():
        return "91" + m
    return m


def compute_display_token(token_number, appointment_time: str) -> str:
    """Compute M/E display token from token_number and appointment time."""
    t = str(appointment_time)[:5]
    prefix = "E" if t >= "13:00" else "M"
    return f"{prefix}{token_number}" if token_number else "?"


def create_parro_mcp_server(supabase_client) -> Server:
    """
    Factory — creates a fully configured MCP Server with all
    Parro Connect clinic tools. Pass the Supabase client so
    the same instance is reused across transports.
    """
    server = Server("parro-connect-clinic")
    supabase = supabase_client

    # ── Helpers ───────────────────────────────────────────────

    def ok(data: dict) -> list[TextContent]:
        return [TextContent(type="text", text=json.dumps(data, ensure_ascii=False, default=str))]

    def err(message: str) -> list[TextContent]:
        return [TextContent(type="text", text=json.dumps({"error": message, "success": False}))]

    def calculate_age(dob_str: str) -> int:
        try:
            dob = date.fromisoformat(str(dob_str)[:10])
            today = date.today()
            return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
        except Exception:
            return 0

    def format_display_date(date_str: str) -> str:
        try:
            d = date.fromisoformat(str(date_str)[:10])
            months = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
            days = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]
            return f"{days[d.weekday()]} {d.day} {months[d.month-1]} {d.year}"
        except Exception:
            return date_str

    def format_time_display(time_str: str) -> str:
        try:
            t = str(time_str)[:5]
            h, m = map(int, t.split(":"))
            period = "AM" if h < 12 else "PM"
            h12 = h if h <= 12 else h - 12
            if h12 == 0:
                h12 = 12
            return f"{h12}:{m:02d} {period}"
        except Exception:
            return time_str

    def generate_slots(start: str, end: str, interval: int, booked: set) -> list[dict]:
        slots = []
        try:
            current = datetime.strptime(start, "%H:%M")
            end_time = datetime.strptime(end, "%H:%M")
            while current < end_time:
                slot_str = current.strftime("%H:%M")
                if slot_str not in booked:
                    slots.append({"time": slot_str, "display": format_time_display(slot_str)})
                current += timedelta(minutes=interval)
        except Exception as e:
            print(f"Slot generation error: {e}")
        return slots

    def get_slots_from_clinic_config(date_str: str, booked: set):
        """Read schedule from clinic_config keys and return morning/evening slots."""
        check_date = date.fromisoformat(date_str)
        day_name = check_date.strftime("%A").lower()

        cfg_rows = supabase.table("clinic_config")\
            .select("config_key, config_value")\
            .eq("doctor_id", DEFAULT_DOCTOR_ID)\
            .execute()
        all_cfg = {r["config_key"]: r["config_value"] for r in (cfg_rows.data or [])}

        morning_start_def = (all_cfg.get("clinic.slot_start_morning") or "10:00")[:5]
        morning_end_def   = (all_cfg.get("clinic.slot_end_morning")   or "13:30")[:5]
        evening_start_def = (all_cfg.get("clinic.slot_start_evening") or "17:00")[:5]
        evening_end_def   = (all_cfg.get("clinic.slot_end_evening")   or "20:30")[:5]
        slot_duration     = int(all_cfg.get("clinic.slot_duration_minutes") or 10)

        p = f"clinic.schedule.{day_name}"
        if f"{p}.enabled" not in all_cfg:
            return {
                "is_closed": False,
                "morning_enabled": True, "morning_start": morning_start_def, "morning_end": morning_end_def,
                "evening_enabled": True, "evening_start": evening_start_def, "evening_end": evening_end_def,
                "slot_duration": slot_duration,
            }

        return {
            "is_closed": all_cfg[f"{p}.enabled"].lower() != "true",
            "morning_enabled": all_cfg.get(f"{p}.morning_enabled", "true").lower() == "true",
            "morning_start":   (all_cfg.get(f"{p}.morning_start") or morning_start_def)[:5],
            "morning_end":     (all_cfg.get(f"{p}.morning_end")   or morning_end_def)[:5],
            "evening_enabled": all_cfg.get(f"{p}.evening_enabled", "true").lower() == "true",
            "evening_start":   (all_cfg.get(f"{p}.evening_start") or evening_start_def)[:5],
            "evening_end":     (all_cfg.get(f"{p}.evening_end")   or evening_end_def)[:5],
            "slot_duration":   int(all_cfg.get(f"{p}.slot_duration_minutes") or slot_duration),
        }

    # ── Tool definitions ──────────────────────────────────────

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return [
            Tool(
                name="get_clinic_info",
                description=(
                    "Get clinic information: name, doctor, speciality, location, "
                    "consultation hours, fees, and languages. "
                    "Always call this first when a new conversation starts."
                ),
                inputSchema={"type": "object", "properties": {}, "required": []}
            ),
            Tool(
                name="get_patient",
                description=(
                    "Find patient(s) registered with a mobile number. "
                    "Returns all family members linked to this mobile. "
                    "Call before booking, cancelling, or checking queue."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "mobile": {"type": "string", "description": "Mobile with country code e.g. 919047099959"}
                    },
                    "required": ["mobile"]
                }
            ),
            Tool(
                name="register_patient",
                description=(
                    "Register a new patient. Call when get_patient returns found=false. "
                    "Collect name, date of birth, gender and language first. DOB: YYYY-MM-DD."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "mobile": {"type": "string"},
                        "name": {"type": "string"},
                        "dob": {"type": "string", "description": "YYYY-MM-DD e.g. 1990-05-15"},
                        "gender": {"type": "string", "enum": ["Male", "Female", "Other"]},
                        "language": {"type": "string", "enum": ["tamil", "english", "hindi"]},
                        "city": {"type": "string"}
                    },
                    "required": ["mobile", "name", "dob", "gender", "language"]
                }
            ),
            Tool(
                name="get_available_slots",
                description=(
                    "Get available appointment slots for a date. "
                    "Returns morning and evening slots separately. "
                    "Default date is today. Always show both sessions."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "date": {"type": "string", "description": "YYYY-MM-DD. Default: today"},
                        "visit_type": {"type": "string", "enum": ["in_clinic", "online"], "description": "Default: in_clinic"}
                    },
                    "required": []
                }
            ),
            Tool(
                name="book_appointment",
                description=(
                    "Book an appointment for a patient. "
                    "ALWAYS confirm slot with patient before calling. "
                    "For online visits also returns video link."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "patient_id": {"type": "string"},
                        "mobile": {"type": "string"},
                        "date": {"type": "string"},
                        "time": {"type": "string", "description": "HH:MM e.g. 10:15"},
                        "visit_type": {"type": "string", "enum": ["in_clinic", "online"], "description": "Default: in_clinic"}
                    },
                    "required": ["patient_id", "mobile", "date", "time"]
                }
            ),
            Tool(
                name="get_queue_status",
                description=(
                    "Get today's queue status and token position. "
                    "Call when patient asks about wait time or token number."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {"mobile": {"type": "string"}},
                    "required": ["mobile"]
                }
            ),
            Tool(
                name="get_upcoming_appointments",
                description=(
                    "Get all upcoming confirmed appointments for a mobile number. "
                    "Use before cancellation or to show patient their bookings."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {"mobile": {"type": "string"}},
                    "required": ["mobile"]
                }
            ),
            Tool(
                name="cancel_appointment",
                description=(
                    "Cancel a specific appointment by ID. "
                    "ALWAYS confirm with patient before cancelling. "
                    "Get appointment_id from get_upcoming_appointments."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "appointment_id": {"type": "string"},
                        "patient_id": {"type": "string"}
                    },
                    "required": ["appointment_id", "patient_id"]
                }
            ),
            Tool(
                name="add_family_member",
                description=(
                    "Add a family member to an existing patient account. "
                    "Call when patient wants to book for someone not in their list."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "primary_mobile": {"type": "string"},
                        "name": {"type": "string"},
                        "dob": {"type": "string"},
                        "gender": {"type": "string", "enum": ["Male", "Female", "Other"]},
                        "relationship": {"type": "string"}
                    },
                    "required": ["primary_mobile", "name", "dob", "gender"]
                }
            ),
        ]

    # ── Tool implementations ──────────────────────────────────

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent]:
        try:

            # ── get_clinic_info ──────────────────────────────
            if name == "get_clinic_info":
                result = supabase.table("doctors")\
                    .select("name, clinic_name, clinic_address, online_consultation_enabled, clinic_timings, online_consultation_hours")\
                    .eq("id", DEFAULT_DOCTOR_ID).single().execute()

                if not result.data:
                    return err("Could not fetch clinic info")
                d = result.data
                return ok({
                    "clinic_name": d.get("clinic_name", ""),
                    "doctor_name": d.get("name", ""),
                    "clinic_address": d.get("clinic_address", ""),
                    "speciality": "Paediatrics",
                    "whatsapp": "+91 84380 55569",
                    "languages": ["Tamil", "English", "Hindi"],
                    "online_consultation": d.get("online_consultation_enabled", False),
                    "clinic_timings": d.get("clinic_timings", {}),
                    "online_hours": d.get("online_consultation_hours", [])
                })

            # ── get_patient ──────────────────────────────────
            elif name == "get_patient":
                mobile = normalize_mobile(arguments["mobile"])

                result = supabase.table("patients")\
                    .select("id, name, patient_code, date_of_birth, gender, language")\
                    .or_(f"mobile.eq.{mobile},family_head_mobile.eq.{mobile}")\
                    .eq("doctor_id", DEFAULT_DOCTOR_ID).execute()

                if not result.data:
                    return ok({"found": False, "patients": [], "message": "No patient found. Please register."})

                patients = [{
                    "id": p["id"],
                    "name": p["name"],
                    "patient_code": p.get("patient_code", ""),
                    "age": calculate_age(p.get("date_of_birth", "")),
                    "gender": p.get("gender", ""),
                    "language": p.get("language", "english")
                } for p in result.data]

                return ok({"found": True, "patients": patients, "total": len(patients), "primary": patients[0]})

            # ── register_patient ─────────────────────────────
            elif name == "register_patient":
                mobile = normalize_mobile(arguments["mobile"])
                name_val = arguments["name"].strip()
                dob = arguments["dob"]
                gender = arguments["gender"]
                language = arguments.get("language", "english")
                city = arguments.get("city", "")

                dob_iso = dob
                age = 0
                birth_year = "0000"
                try:
                    dob_date = date.fromisoformat(dob[:10])
                    today = date.today()
                    age = today.year - dob_date.year - ((today.month, today.day) < (dob_date.month, dob_date.day))
                    dob_iso = dob_date.isoformat()
                    birth_year = str(dob_date.year)
                except Exception:
                    pass

                name_part = name_val[:3].upper().replace(" ", "")
                counter_res = supabase.table("patients").select("id", count="exact").eq("doctor_id", DEFAULT_DOCTOR_ID).execute()
                counter = (counter_res.count or 0) + 1
                patient_code = f"{name_part}-{birth_year}-{counter}"
                new_id = str(uuid.uuid4())

                supabase.table("patients").insert({
                    "id": new_id,
                    "mobile": mobile,
                    "whatsapp_number": mobile,
                    "name": name_val,
                    "date_of_birth": dob_iso,
                    "age": age,
                    "gender": gender,
                    "language": language,
                    "city": city,
                    "patient_code": patient_code,
                    "family_head_mobile": mobile,
                    "doctor_id": DEFAULT_DOCTOR_ID,
                    "registration_source": "mcp",
                    "created_at": datetime.now(IST).isoformat()
                }).execute()

                return ok({
                    "success": True,
                    "patient_id": new_id,
                    "patient_code": patient_code,
                    "name": name_val,
                    "message": f"Welcome {name_val.split()[0]}! Registration complete. Patient code: {patient_code}"
                })

            # ── get_available_slots ──────────────────────────
            elif name == "get_available_slots":
                date_str = arguments.get("date", date.today().isoformat())
                visit_type = arguments.get("visit_type", "in_clinic")
                check_date = date.fromisoformat(date_str)
                day_name = check_date.strftime("%A").lower()
                display_date = format_display_date(date_str)

                booked_result = supabase.table("appointments")\
                    .select("appointment_time")\
                    .eq("doctor_id", DEFAULT_DOCTOR_ID)\
                    .eq("appointment_date", date_str)\
                    .in_("status", ["Confirmed", "In Progress"])\
                    .execute()
                booked = {str(a["appointment_time"])[:5] for a in (booked_result.data or [])}

                # Online consultation
                if visit_type == "online":
                    doc = supabase.table("doctors")\
                        .select("online_consultation_enabled, online_consultation_hours")\
                        .eq("id", DEFAULT_DOCTOR_ID).single().execute()

                    if not doc.data or not doc.data.get("online_consultation_enabled"):
                        return ok({"has_slots": False, "visit_type": "online", "message": "Online consultation not available."})

                    online_hours = doc.data.get("online_consultation_hours", [])
                    day_entry = next((h for h in online_hours if h.get("day", "").lower() == day_name), None)
                    online_slots = []
                    if day_entry:
                        slot_duration = 10
                        if "morning" in day_entry or "evening" in day_entry:
                            m = day_entry.get("morning") or {}
                            e = day_entry.get("evening") or {}
                            if m.get("enabled"):
                                online_slots.extend(generate_slots(m["start"], m["end"], slot_duration, booked))
                            if e.get("enabled"):
                                online_slots.extend(generate_slots(e["start"], e["end"], slot_duration, booked))
                        else:
                            online_slots.extend(generate_slots(day_entry.get("start", "09:00"), day_entry.get("end", "17:00"), slot_duration, booked))

                    return ok({
                        "date": date_str, "display_date": display_date, "visit_type": "online",
                        "slots": online_slots[:10], "has_slots": len(online_slots) > 0, "total": len(online_slots)
                    })

                # In-clinic — read from clinic_config
                sched = get_slots_from_clinic_config(date_str, booked)
                if sched["is_closed"]:
                    return ok({"date": date_str, "display_date": display_date, "has_slots": False,
                               "message": f"Clinic is closed on {check_date.strftime('%A')}."})

                dur = sched["slot_duration"]
                morning_slots = generate_slots(sched["morning_start"], sched["morning_end"], dur, booked) if sched["morning_enabled"] else []
                evening_slots = generate_slots(sched["evening_start"], sched["evening_end"], dur, booked) if sched["evening_enabled"] else []

                return ok({
                    "date": date_str, "display_date": display_date, "visit_type": "in_clinic",
                    "morning_slots": morning_slots[:8], "evening_slots": evening_slots[:8],
                    "morning_count": len(morning_slots), "evening_count": len(evening_slots),
                    "has_slots": bool(morning_slots or evening_slots),
                    "tip": "Show morning and evening options. Ask patient which session they prefer."
                })

            # ── book_appointment ─────────────────────────────
            elif name == "book_appointment":
                patient_id = arguments["patient_id"]
                mobile = normalize_mobile(arguments["mobile"])
                appt_date = arguments["date"]
                appt_time = arguments["time"]
                visit_type = arguments.get("visit_type", "in_clinic")

                existing = supabase.table("appointments")\
                    .select("id").eq("doctor_id", DEFAULT_DOCTOR_ID)\
                    .eq("appointment_date", appt_date).eq("appointment_time", appt_time)\
                    .in_("status", ["Confirmed", "In Progress"]).execute()

                if existing.data:
                    return ok({"success": False, "message": "Slot just taken. Please choose another."})

                hour = int(appt_time.split(":")[0])
                prefix = "M" if hour < 13 else "E"
                session_start = "13:00" if prefix == "E" else "00:00"
                session_end   = "23:59" if prefix == "E" else "12:59"

                existing_tokens = supabase.table("appointments")\
                    .select("token_number").eq("doctor_id", DEFAULT_DOCTOR_ID)\
                    .eq("appointment_date", appt_date)\
                    .gte("appointment_time", session_start).lte("appointment_time", session_end)\
                    .neq("status", "Cancelled").execute()

                token_num = len(existing_tokens.data or []) + 1
                display_token = f"{prefix}{token_num}"
                appt_id = str(uuid.uuid4())

                supabase.table("appointments").insert({
                    "id": appt_id,
                    "patient_id": patient_id,
                    "doctor_id": DEFAULT_DOCTOR_ID,
                    "appointment_date": appt_date,
                    "appointment_time": appt_time,
                    "token_number": token_num,
                    "status": "Confirmed",
                    "consultation_type": visit_type,
                    "created_at": datetime.now(IST).isoformat()
                }).execute()

                patient = supabase.table("patients").select("name").eq("id", patient_id).single().execute()
                patient_name = patient.data.get("name", "") if patient.data else ""

                response = {
                    "success": True,
                    "appointment_id": appt_id,
                    "token": display_token,
                    "patient_name": patient_name,
                    "date": format_display_date(appt_date),
                    "time": format_time_display(appt_time),
                    "visit_type": visit_type,
                    "confirmation_message": (
                        f"Appointment confirmed! Token {display_token} for {patient_name} "
                        f"on {format_display_date(appt_date)} at {format_time_display(appt_time)}."
                    )
                }

                if visit_type == "online":
                    try:
                        from consultation_helpers import generate_room_id, get_patient_join_url
                        scheduled_dt = datetime.strptime(f"{appt_date}T{appt_time}", "%Y-%m-%dT%H:%M")
                        room_id = generate_room_id("drkumar", scheduled_dt)
                        room_url = get_patient_join_url(room_id)
                        supabase.table("consultations").insert({
                            "id": str(uuid.uuid4()),
                            "appointment_id": appt_id, "patient_id": patient_id,
                            "doctor_id": DEFAULT_DOCTOR_ID, "room_id": room_id, "room_url": room_url,
                            "scheduled_at": datetime.now(IST).isoformat(),
                            "status": "scheduled", "consultation_type": "online", "patient_link_sent": False
                        }).execute()
                        response["video_link"] = room_url
                        response["video_message"] = f"Video link: {room_url} — No download needed."
                    except Exception as e:
                        print(f"Video room error: {e}")

                return ok(response)

            # ── get_queue_status ─────────────────────────────
            elif name == "get_queue_status":
                mobile = normalize_mobile(arguments["mobile"])
                today = date.today().isoformat()

                patients = supabase.table("patients")\
                    .select("id, name")\
                    .or_(f"mobile.eq.{mobile},family_head_mobile.eq.{mobile}")\
                    .eq("doctor_id", DEFAULT_DOCTOR_ID).execute()

                if not patients.data:
                    return ok({"has_appointment": False, "message": "No appointments found for today."})

                patient_ids = [p["id"] for p in patients.data]
                patient_names = {p["id"]: p["name"] for p in patients.data}

                appointments = supabase.table("appointments")\
                    .select("id, appointment_time, token_number, status, patient_id")\
                    .eq("appointment_date", today).eq("doctor_id", DEFAULT_DOCTOR_ID)\
                    .in_("patient_id", patient_ids).in_("status", ["Confirmed", "In Progress"]).execute()

                if not appointments.data:
                    return ok({"has_appointment": False, "message": "No appointments found for today."})

                serving = supabase.table("appointments")\
                    .select("token_number, appointment_time")\
                    .eq("appointment_date", today).eq("doctor_id", DEFAULT_DOCTOR_ID)\
                    .eq("status", "In Progress").execute()

                now_serving = None
                if serving.data:
                    s = serving.data[0]
                    now_serving = compute_display_token(s.get("token_number"), str(s.get("appointment_time", "")))

                appt_list = []
                for a in appointments.data:
                    appt_time_str = str(a.get("appointment_time", ""))[:5]
                    token_num = a.get("token_number") or 0
                    token = compute_display_token(token_num, appt_time_str)
                    is_evening = appt_time_str >= "13:00"
                    session_start = "13:00" if is_evening else "00:00"
                    session_end   = "23:59" if is_evening else "12:59"

                    ahead_result = supabase.table("appointments")\
                        .select("token_number")\
                        .eq("appointment_date", today).eq("doctor_id", DEFAULT_DOCTOR_ID)\
                        .gte("appointment_time", session_start).lte("appointment_time", session_end)\
                        .eq("status", "Confirmed").lt("token_number", token_num).execute()

                    patients_ahead = len(ahead_result.data or [])
                    appt_list.append({
                        "patient": patient_names.get(a["patient_id"], ""),
                        "token": token,
                        "time": format_time_display(appt_time_str),
                        "status": a.get("status", ""),
                        "patients_ahead": patients_ahead,
                        "est_wait_mins": patients_ahead * 10
                    })

                status_parts = [f"Now serving: {now_serving}"] if now_serving else []
                for a in appt_list:
                    if a["patients_ahead"] == 0:
                        status_parts.append(f"{a['patient']} (Token {a['token']}): You are next!")
                    else:
                        status_parts.append(f"{a['patient']} (Token {a['token']}): {a['patients_ahead']} patient(s) ahead, ~{a['est_wait_mins']} mins wait")

                return ok({"has_appointment": True, "now_serving": now_serving, "your_appointments": appt_list, "status_message": " | ".join(status_parts)})

            # ── get_upcoming_appointments ────────────────────
            elif name == "get_upcoming_appointments":
                mobile = normalize_mobile(arguments["mobile"])
                today = date.today().isoformat()

                patients = supabase.table("patients")\
                    .select("id, name")\
                    .or_(f"mobile.eq.{mobile},family_head_mobile.eq.{mobile}")\
                    .eq("doctor_id", DEFAULT_DOCTOR_ID).execute()

                if not patients.data:
                    return ok({"appointments": [], "total": 0})

                patient_ids = [p["id"] for p in patients.data]
                patient_names = {p["id"]: p["name"] for p in patients.data}

                appts = supabase.table("appointments")\
                    .select("id, appointment_date, appointment_time, token_number, consultation_type, status, patient_id")\
                    .in_("patient_id", patient_ids).eq("doctor_id", DEFAULT_DOCTOR_ID)\
                    .eq("status", "Confirmed").gte("appointment_date", today)\
                    .order("appointment_date").execute()

                result = []
                for a in (appts.data or []):
                    appt_time_str = str(a.get("appointment_time", ""))[:5]
                    result.append({
                        "id": a["id"],
                        "patient": patient_names.get(a["patient_id"], ""),
                        "patient_id": a["patient_id"],
                        "token": compute_display_token(a.get("token_number"), appt_time_str),
                        "date": format_display_date(str(a["appointment_date"])[:10]),
                        "time": format_time_display(appt_time_str),
                        "visit_type": a.get("consultation_type", "in_clinic"),
                        "status": a.get("status", "")
                    })

                return ok({"appointments": result, "total": len(result)})

            # ── cancel_appointment ───────────────────────────
            elif name == "cancel_appointment":
                appointment_id = arguments["appointment_id"]
                patient_id = arguments["patient_id"]

                appt = supabase.table("appointments")\
                    .select("id, patient_id, appointment_date, appointment_time, token_number, status")\
                    .eq("id", appointment_id).eq("patient_id", patient_id).single().execute()

                if not appt.data:
                    return ok({"success": False, "message": "Appointment not found or access denied."})
                if appt.data.get("status") == "Cancelled":
                    return ok({"success": False, "message": "This appointment is already cancelled."})

                supabase.table("appointments").update({"status": "Cancelled"}).eq("id", appointment_id).execute()

                appt_time_str = str(appt.data.get("appointment_time", ""))[:5]
                token = compute_display_token(appt.data.get("token_number"), appt_time_str)
                return ok({
                    "success": True,
                    "token": token,
                    "date": format_display_date(str(appt.data.get("appointment_date", ""))[:10]),
                    "time": format_time_display(appt_time_str),
                    "message": f"Appointment {token} cancelled successfully."
                })

            # ── add_family_member ────────────────────────────
            elif name == "add_family_member":
                primary_mobile = normalize_mobile(arguments["primary_mobile"])
                name_val = arguments["name"].strip()
                dob = arguments["dob"]
                gender = arguments["gender"]
                relationship = arguments.get("relationship", "Family")

                primary = supabase.table("patients").select("language")\
                    .eq("mobile", primary_mobile).eq("doctor_id", DEFAULT_DOCTOR_ID).limit(1).execute()
                language = primary.data[0].get("language", "english") if primary.data else "english"

                dob_iso = dob
                age = 0
                birth_year = "0000"
                try:
                    dob_date = date.fromisoformat(dob[:10])
                    today = date.today()
                    age = today.year - dob_date.year - ((today.month, today.day) < (dob_date.month, dob_date.day))
                    dob_iso = dob_date.isoformat()
                    birth_year = str(dob_date.year)
                except Exception:
                    pass

                name_part = name_val[:3].upper().replace(" ", "")
                counter_res = supabase.table("patients").select("id", count="exact").eq("doctor_id", DEFAULT_DOCTOR_ID).execute()
                counter = (counter_res.count or 0) + 1
                patient_code = f"{name_part}-{birth_year}-{counter}"
                new_id = str(uuid.uuid4())

                supabase.table("patients").insert({
                    "id": new_id,
                    "mobile": primary_mobile,
                    "whatsapp_number": primary_mobile,
                    "name": name_val,
                    "date_of_birth": dob_iso,
                    "age": age,
                    "gender": gender,
                    "language": language,
                    "patient_code": patient_code,
                    "family_head_mobile": primary_mobile,
                    "doctor_id": DEFAULT_DOCTOR_ID,
                    "relationship": relationship,
                    "registration_source": "mcp",
                    "created_at": datetime.now(IST).isoformat()
                }).execute()

                return ok({
                    "success": True,
                    "patient_id": new_id,
                    "patient_code": patient_code,
                    "name": name_val,
                    "relationship": relationship,
                    "message": f"{name_val} has been added to your family."
                })

            else:
                return err(f"Unknown tool: {name}")

        except Exception as e:
            import traceback
            traceback.print_exc()
            return err(f"Tool execution failed: {str(e)}")

    server._direct_call_tool = call_tool
    return server

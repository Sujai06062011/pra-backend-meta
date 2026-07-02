"""
agent.py — Claude Agent for Parro Connect clinic WhatsApp bot.

Handles free-form Tamil/English messages autonomously using tool calls.
Called from whatsapp_handler.py only when current_state is "idle" or "agent".
Falls back to None on any error so the state machine remains the safety net.

CONSTRAINTS (non-negotiable):
- get_patient filters by family_head_mobile ONLY — never by doctor_id
- get_upcoming_appointments queries across ALL doctors, no doctor_id filter
- Agent never answers medical questions — defers to ask-doctor flow
- Agent never books without explicit patient confirmation first
"""

import os
import json
from datetime import datetime, timedelta, date
import pytz

IST = pytz.timezone("Asia/Kolkata")
DEFAULT_DOCTOR_ID = "8c33abe0-5d2e-4613-9437-c7c375e8d162"
MAX_HISTORY_TURNS = 10
AGENT_TIMEOUT_MINS = 30


TOOLS = [
    {
        "name": "get_patient",
        "description": (
            "Find all patients registered under a mobile number — self + all family members. "
            "Filters by family_head_mobile ONLY. Never filters by doctor. "
            "Always call this first before any booking, queue check, or cancellation."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "mobile": {
                    "type": "string",
                    "description": "Mobile with country code e.g. 919047099959",
                }
            },
            "required": ["mobile"],
        },
    },
    {
        "name": "get_clinic_doctors",
        "description": (
            "Get all active doctors at this clinic. "
            "Use this to resolve which doctor the patient wants when they don't specify, "
            "or when the clinic has multiple doctors."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "clinic_name": {
                    "type": "string",
                    "description": "Clinic name to scope results to this clinic's doctors only.",
                }
            },
            "required": [],
        },
    },
    {
        "name": "get_available_slots",
        "description": (
            "Get available in-clinic appointment slots for a specific doctor on a date. "
            "Returns morning and evening slots separately. "
            "If doctor_id not known yet, call get_clinic_doctors first."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": "Date in YYYY-MM-DD format",
                },
                "doctor_id": {
                    "type": "string",
                    "description": "Doctor UUID. Omit to use the default/primary doctor.",
                },
            },
            "required": ["date"],
        },
    },
    {
        "name": "book_appointment",
        "description": (
            "Book an in-clinic appointment. "
            "ONLY call after presenting available slots and receiving explicit confirmation "
            "('yes', 'confirm', 'ok', 'சரி', etc.) from the patient in the conversation."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "patient_id": {"type": "string"},
                "date": {"type": "string", "description": "YYYY-MM-DD"},
                "time": {"type": "string", "description": "HH:MM e.g. 10:15"},
                "doctor_id": {
                    "type": "string",
                    "description": "Doctor UUID. Omit to use default doctor.",
                },
            },
            "required": ["patient_id", "date", "time"],
        },
    },
    {
        "name": "get_upcoming_appointments",
        "description": (
            "Get all upcoming confirmed appointments for a mobile number across ALL doctors. "
            "Use before cancellation to show the patient what they have."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"mobile": {"type": "string"}},
            "required": ["mobile"],
        },
    },
    {
        "name": "get_queue_status",
        "description": "Get today's live queue status and estimated wait for a mobile number.",
        "input_schema": {
            "type": "object",
            "properties": {
                "mobile": {"type": "string"},
                "doctor_id": {
                    "type": "string",
                    "description": "Doctor UUID. Omit to use default doctor.",
                },
            },
            "required": ["mobile"],
        },
    },
    {
        "name": "cancel_appointment",
        "description": (
            "Cancel a specific appointment by ID. "
            "Always call get_upcoming_appointments first, list them to the patient, "
            "and confirm which one to cancel before calling this."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"appointment_id": {"type": "string"}},
            "required": ["appointment_id"],
        },
    },
    {
        "name": "register_patient",
        "description": (
            "Register a brand-new patient. Only call when get_patient returns found=False. "
            "Collect name, date of birth (YYYY-MM-DD), gender, and language first."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "mobile": {"type": "string"},
                "name": {"type": "string"},
                "dob": {"type": "string", "description": "YYYY-MM-DD e.g. 1990-05-15"},
                "gender": {"type": "string", "enum": ["Male", "Female", "Other"]},
                "language": {"type": "string", "enum": ["tamil", "english", "hindi"]},
                "doctor_id": {"type": "string"},
            },
            "required": ["mobile", "name", "dob", "gender", "language"],
        },
    },
    {
        "name": "add_family_member",
        "description": (
            "Add a new family member to an existing patient account. "
            "Call when patient wants to book for someone not yet in their list. "
            "Ask for name, date of birth, gender. Do NOT assume or invent details."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "primary_mobile": {"type": "string"},
                "name": {"type": "string"},
                "dob": {"type": "string", "description": "YYYY-MM-DD"},
                "gender": {"type": "string", "enum": ["Male", "Female", "Other"]},
                "relationship": {"type": "string", "description": "e.g. Son, Daughter, Spouse"},
                "doctor_id": {"type": "string"},
            },
            "required": ["primary_mobile", "name", "dob", "gender"],
        },
    },
]


# ── Tool implementations ───────────────────────────────────────────────────────

def _tool_get_patient(mobile: str) -> dict:
    """Constraint: filter by family_head_mobile ONLY — never by doctor_id."""
    from database import supabase as supa
    result = (
        supa.table("patients")
        .select("id, name, patient_code, date_of_birth, gender, age, language")
        .or_(f"mobile.eq.{mobile},family_head_mobile.eq.{mobile}")
        .order("created_at", desc=False)
        .execute()
    )
    patients = result.data or []
    return {"found": bool(patients), "patients": patients}


def _tool_get_clinic_doctors(clinic_name: str = "") -> dict:
    from database import supabase as supa
    query = (
        supa.table("doctors")
        .select("id, name, speciality, specialty_display, clinic_name")
        .eq("is_available", True)
    )
    if clinic_name:
        query = query.eq("clinic_name", clinic_name)
    result = query.execute()
    doctors = [
        {
            "id": d["id"],
            "name": d["name"],
            "specialty": d.get("specialty_display") or d.get("speciality", ""),
            "clinic": d.get("clinic_name", ""),
        }
        for d in (result.data or [])
    ]
    return {"doctors": doctors, "count": len(doctors)}


def _tool_get_available_slots(date_str: str, doctor_id: str) -> dict:
    from routers.availability import get_availability_for_date, generate_slots_for_date
    from database import get_booked_slots

    av = get_availability_for_date(doctor_id, date_str)
    if av.get("is_holiday"):
        return {"available": False, "reason": f"Holiday: {av.get('holiday_name', 'closed')}"}
    if not av.get("morning", {}).get("enabled") and not av.get("evening", {}).get("enabled"):
        return {"available": False, "reason": "Clinic closed on this day"}

    now_ist = datetime.now(pytz.timezone("Asia/Kolkata"))
    cutoff = now_ist.strftime("%H:%M") if date_str == now_ist.date().isoformat() else ""
    booked = set(get_booked_slots(doctor_id, date_str))
    all_slots = generate_slots_for_date(doctor_id, date_str)

    available = [
        s for s in all_slots
        if s["time"] not in booked and (not cutoff or s["time"] > cutoff)
    ]
    morning = [s["time"] for s in available if s["session"] == "morning"]
    evening = [s["time"] for s in available if s["session"] == "evening"]

    return {
        "available": bool(available),
        "date": date_str,
        "morning_slots": morning,
        "evening_slots": evening,
        "total": len(available),
    }


def _tool_book_appointment(patient_id: str, date_str: str, time_str: str, doctor_id: str) -> dict:
    from database import (
        assign_token_for_slot, create_appointment, get_display_token,
        is_slot_available, get_active_appointment, supabase as supa,
    )
    from whatsapp_handler import format_time

    existing = get_active_appointment(patient_id, doctor_id, date_str)
    if existing:
        ex_time = format_time(str(existing.get("appointment_time", ""))[:5])
        return {"success": False, "reason": f"Already has an appointment at {ex_time} on {date_str}"}

    if not is_slot_available(doctor_id, date_str, time_str):
        return {"success": False, "reason": "Slot is already taken"}

    token = assign_token_for_slot(doctor_id, date_str, time_str)
    appt = create_appointment(patient_id, doctor_id, date_str, time_str, token)
    if not appt:
        return {"success": False, "reason": "Booking failed — please try again"}

    display_tok = get_display_token(token, time_str, doctor_id=doctor_id, date_str=date_str)
    pat_res = supa.table("patients").select("name, patient_code").eq("id", patient_id).single().execute()
    pat_name = (pat_res.data or {}).get("name", "Patient")
    pat_code = (pat_res.data or {}).get("patient_code", "")

    return {
        "success": True,
        "patient_name": pat_name,
        "patient_code": pat_code,
        "date": date_str,
        "time": time_str,
        "token": display_tok,
        "appointment_id": appt.get("id", ""),
    }


def _tool_get_upcoming_appointments(mobile: str) -> dict:
    """Constraint: queries across ALL doctors — no doctor_id filter."""
    from database import supabase as supa, get_display_token, _time_str

    own = supa.table("patients").select("id").eq("mobile", mobile).execute()
    fam = supa.table("patients").select("id").eq("family_head_mobile", mobile).execute()
    my_ids = list({p["id"] for p in (own.data or []) + (fam.data or [])})

    if not my_ids:
        return {"appointments": [], "count": 0}

    today = date.today().isoformat()
    res = (
        supa.table("appointments")
        .select(
            "id, patient_id, doctor_id, appointment_date, appointment_time, "
            "token_number, status, patients(name), doctors(name)"
        )
        .in_("patient_id", my_ids)
        .eq("status", "Confirmed")
        .gte("appointment_date", today)
        .order("appointment_date")
        .order("appointment_time")
        .limit(10)
        .execute()
    )

    simplified = []
    for a in (res.data or []):
        doc_name = (a.get("doctors") or {}).get("name", "")
        d_id = a.get("doctor_id", "")
        d_str = str(a.get("appointment_date", ""))[:10]
        simplified.append({
            "id": a.get("id"),
            "patient_name": (a.get("patients") or {}).get("name", ""),
            "doctor_name": doc_name,
            "doctor_id": d_id,
            "date": d_str,
            "time": str(a.get("appointment_time", ""))[:5],
            "token": get_display_token(
                a.get("token_number"), a.get("appointment_time"),
                doctor_id=d_id, date_str=d_str
            ),
            "status": a.get("status", ""),
        })

    return {"appointments": simplified, "count": len(simplified)}


def _tool_get_queue_status(mobile: str, doctor_id: str) -> dict:
    from database import supabase as supa, get_display_token, _time_str, get_slot_config

    now = datetime.now(pytz.timezone("Asia/Kolkata"))
    today = now.date().isoformat()

    tok_res = (
        supa.table("tokens")
        .select("current_token")
        .eq("doctor_id", doctor_id)
        .eq("queue_date", today)
        .execute()
    )
    current_token = tok_res.data[0]["current_token"] if tok_res.data else 0

    own = supa.table("patients").select("id").eq("mobile", mobile).execute()
    fam = supa.table("patients").select("id").eq("family_head_mobile", mobile).execute()
    my_ids = list({p["id"] for p in (own.data or []) + (fam.data or [])})
    if not my_ids:
        return {"has_appointment": False}

    appts = (
        supa.table("appointments")
        .select("token_number, appointment_time, status, patients(name)")
        .eq("doctor_id", doctor_id)
        .eq("appointment_date", today)
        .neq("status", "Cancelled")
        .neq("consultation_type", "online")
        .in_("patient_id", my_ids)
        .execute()
    )
    if not (appts.data or []):
        return {"has_appointment": False}

    all_today = (
        supa.table("appointments")
        .select("token_number, appointment_time, status")
        .eq("doctor_id", doctor_id)
        .eq("appointment_date", today)
        .neq("consultation_type", "online")
        .execute()
        .data or []
    )
    slot_min = get_slot_config().get("duration", 10)

    results = []
    for a in (appts.data or []):
        t = _time_str(a.get("appointment_time"))
        display_tok = get_display_token(
            a.get("token_number"), a.get("appointment_time"),
            doctor_id=doctor_id, date_str=today
        )
        pat_name = (a.get("patients") or {}).get("name", "Patient")
        if a.get("status") == "In Progress":
            status_msg = "Now being seen"
        else:
            is_eve = t >= "13:00:00"
            ahead = len([
                x for x in all_today
                if x.get("status") == "Confirmed"
                and (_time_str(x.get("appointment_time")) >= "13:00:00") == is_eve
                and _time_str(x.get("appointment_time")) < t
                and (x.get("token_number") or 0) > current_token
            ])
            wait = ahead * slot_min
            status_msg = f"~{wait} mins wait" if wait > 0 else "Next in line"
        results.append({"patient": pat_name, "token": display_tok, "time": t[:5], "status": status_msg})

    return {"has_appointment": True, "current_token": current_token, "queue": results}


def _tool_cancel_appointment(appointment_id: str) -> dict:
    from database import cancel_appointment, supabase as supa
    try:
        appt_res = (
            supa.table("appointments")
            .select("appointment_date, appointment_time, patients(name)")
            .eq("id", appointment_id)
            .single()
            .execute()
        )
        cancel_appointment(appointment_id)
        a = appt_res.data or {}
        return {
            "success": True,
            "patient": (a.get("patients") or {}).get("name", "Patient"),
            "date": str(a.get("appointment_date", ""))[:10],
            "time": str(a.get("appointment_time", ""))[:5],
        }
    except Exception as e:
        return {"success": False, "reason": str(e)}


def _tool_register_patient(mobile: str, name: str, dob: str, gender: str,
                            language: str, doctor_id: str) -> dict:
    from database import create_patient
    try:
        new_patient = create_patient(
            mobile, name, dob, gender,
            family_head_mobile=mobile,
            language=language, city="",
            doctor_id=doctor_id,
        )
        if not new_patient:
            return {"success": False, "reason": "Registration failed"}
        return {
            "success": True,
            "patient_id": new_patient.get("id", ""),
            "patient_code": new_patient.get("patient_code", ""),
            "name": name,
        }
    except Exception as e:
        return {"success": False, "reason": str(e)}


def _tool_add_family_member(primary_mobile: str, name: str, dob: str,
                             gender: str, doctor_id: str, relationship: str = "") -> dict:
    from database import supabase as supa, _next_patient_counter
    import re as _re
    from whatsapp_handler import MONTHS

    dob_iso = None
    birth_year = "0000"
    age = None
    try:
        _dob = dob.strip()
        dob_date = None
        nm = _re.match(r"^(\d{1,2})[/\-\s](\d{1,2})[/\-\s](\d{4})$", _dob)
        if nm:
            dob_date = date(int(nm.group(3)), int(nm.group(2)), int(nm.group(1)))
        if not dob_date:
            dm = _re.search(r"(\d{1,2})[\s\-/]+([a-zA-Z]+)[\s\-/]+(\d{4})", _dob)
            if dm:
                m_num = MONTHS.get(dm.group(2).lower()[:3])
                if m_num:
                    dob_date = date(int(dm.group(3)), int(m_num), int(dm.group(1)))
        if not dob_date and len(_dob) == 10 and _dob[4] == "-":
            dob_date = date.fromisoformat(_dob)
        if dob_date:
            today_d = date.today()
            age = today_d.year - dob_date.year - (
                (today_d.month, today_d.day) < (dob_date.month, dob_date.day)
            )
            dob_iso = dob_date.isoformat()
            birth_year = str(dob_date.year)
    except Exception:
        pass

    name_part = name[:3].upper().replace(" ", "")
    counter = _next_patient_counter(doctor_id)
    patient_code = f"{name_part}-{birth_year}-{counter}"
    gender_clean = "Male" if gender.lower().startswith("m") else (
        "Female" if gender.lower().startswith("f") else "Other")

    try:
        ins = supa.table("patients").insert({
            "mobile": primary_mobile,
            "whatsapp_number": primary_mobile,
            "name": name,
            "date_of_birth": dob_iso,
            "age": age,
            "gender": gender_clean,
            "language": "tamil",
            "patient_code": patient_code,
            "family_head_mobile": primary_mobile,
            "registration_source": "whatsapp",
            "doctor_id": doctor_id,
        }).execute()
        new_id = ins.data[0]["id"] if ins.data else ""
        return {
            "success": True,
            "patient_id": new_id,
            "patient_code": patient_code,
            "name": name,
            "age": age,
        }
    except Exception as e:
        return {"success": False, "reason": str(e)}


# ── Tool dispatcher ────────────────────────────────────────────────────────────

def _dispatch_tool(name: str, inputs: dict, doctor_id: str = DEFAULT_DOCTOR_ID, clinic_name: str = "") -> str:
    """Maps Claude's tool calls to actual database functions.
    doctor_id and clinic_name are threaded from run_clinic_agent for correct scoping."""
    try:
        if name == "get_patient":
            result = _tool_get_patient(inputs["mobile"])
        elif name == "get_clinic_doctors":
            result = _tool_get_clinic_doctors(clinic_name=clinic_name)
        elif name == "get_available_slots":
            result = _tool_get_available_slots(
                inputs["date"],
                inputs.get("doctor_id") or doctor_id,
            )
        elif name == "book_appointment":
            result = _tool_book_appointment(
                inputs["patient_id"], inputs["date"], inputs["time"],
                inputs.get("doctor_id") or doctor_id,
            )
        elif name == "get_upcoming_appointments":
            result = _tool_get_upcoming_appointments(inputs["mobile"])
        elif name == "get_queue_status":
            result = _tool_get_queue_status(
                inputs["mobile"],
                inputs.get("doctor_id") or doctor_id,
            )
        elif name == "cancel_appointment":
            result = _tool_cancel_appointment(inputs["appointment_id"])
        elif name == "register_patient":
            result = _tool_register_patient(
                inputs["mobile"], inputs["name"], inputs["dob"],
                inputs["gender"], inputs["language"],
                inputs.get("doctor_id") or doctor_id,
            )
        elif name == "add_family_member":
            result = _tool_add_family_member(
                inputs["primary_mobile"], inputs["name"], inputs["dob"],
                inputs["gender"], inputs.get("doctor_id") or doctor_id,
                inputs.get("relationship", ""),
            )
        else:
            result = {"error": f"Unknown tool: {name}"}
    except Exception as e:
        result = {"error": str(e)}
    return json.dumps(result, ensure_ascii=False, default=str)


# ── History helpers ────────────────────────────────────────────────────────────

def _validate_history(history: list) -> list:
    """Remove orphaned tool_result blocks whose tool_use is no longer in history."""
    if not history:
        return history

    tool_use_ids = set()
    for msg in history:
        if msg.get("role") == "assistant":
            for block in (msg.get("content") or []):
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tool_use_ids.add(block["id"])

    clean = []
    for msg in history:
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, list):
                filtered = [
                    b for b in content
                    if not (isinstance(b, dict) and b.get("type") == "tool_result"
                            and b.get("tool_use_id") not in tool_use_ids)
                ]
                if not filtered:
                    continue
                msg = {**msg, "content": filtered}
        clean.append(msg)
    return clean


def _serialize_content(content) -> list:
    """Convert Anthropic SDK content blocks to JSON-safe dicts for persistence."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    out = []
    for block in content:
        if hasattr(block, "type"):
            if block.type == "text":
                out.append({"type": "text", "text": block.text})
            elif block.type == "tool_use":
                out.append({"type": "tool_use", "id": block.id,
                            "name": block.name, "input": block.input})
        elif isinstance(block, dict):
            out.append(block)
    return out


# ── Main entry point ───────────────────────────────────────────────────────────

async def run_clinic_agent(mobile: str, text: str, doctor: dict) -> str | None:
    """
    Run Claude agent for a free-form message.
    Returns a WhatsApp-ready reply string, or None to fall through to state machine.
    Persists multi-turn conversation history in conversation_state table.

    Voice and text inputs both arrive here as plain text — routing is identical.
    doctor dict carries the clinic context from the incoming WhatsApp number.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("[AGENT] ANTHROPIC_API_KEY not set — falling through to state machine")
        return None

    import anthropic
    from database import get_conversation_state, save_conversation_state

    # Use the doctor from the incoming WhatsApp number context
    doctor_id = doctor.get("id") or DEFAULT_DOCTOR_ID
    clinic_name = doctor.get("clinic_name", "TrueCare Family Clinic")
    doctor_name = doctor.get("name", "Doctor")
    clinic_timings = doctor.get("clinic_timings", "Mon-Sat: 9AM-1PM, 5PM-8PM")
    clinic_address = doctor.get("clinic_address", "Chennai")

    # Load persisted history
    _, temp_data = get_conversation_state(mobile)
    history = temp_data.get("agent_history", []) if temp_data else []
    last_ts = temp_data.get("agent_ts", "") if temp_data else ""

    # Expire after inactivity
    if last_ts:
        try:
            last_dt = datetime.fromisoformat(last_ts)
            if (datetime.now(IST) - last_dt).total_seconds() > AGENT_TIMEOUT_MINS * 60:
                history = []
        except Exception:
            history = []

    history = _validate_history(history)

    now_ist = datetime.now(IST)
    today_str = now_ist.date().isoformat()
    tomorrow_str = (now_ist.date() + timedelta(days=1)).isoformat()

    system_prompt = f"""You are *Parro*, the WhatsApp assistant for {clinic_name}.
If asked who you are or what your name is, say: "I'm Parro, your clinic assistant 🤝"
Doctor: {doctor_name}
Timings: {clinic_timings}
Address: {clinic_address}

Today: {today_str} | Tomorrow: {tomorrow_str}
Resolve relative dates: "naalaikku"/"tomorrow" = {tomorrow_str}, "indru"/"today" = {today_str},
"next week" = {(now_ist.date() + timedelta(days=7)).isoformat()}.

RULES:
1. Reply in the same language the patient used (Tamil or English). Never mix unless patient does.
2. Keep replies short and WhatsApp-friendly — 2-4 lines max. No markdown headers or ** bold **.
3. The patient's WhatsApp number is {mobile} — use it directly, NEVER ask for it.
4. Call get_patient(mobile="{mobile}") IMMEDIATELY without asking — you already have the number.
5. If get_patient returns found=False: collect name, DOB, gender, language then call register_patient.
6. If patient wants to book for a name NOT in their family list:
   - Do NOT guess or search other families
   - Ask if they want to add this person as a new family member
   - If yes, collect name+DOB+gender then call add_family_member
7. If patient specifies a doctor by name: call get_clinic_doctors(clinic_name="{clinic_name}") to get their ID, then use it. If patient doesn't specify a doctor and there are multiple, ask which doctor they want.
8. For booking flow:
   a. call get_available_slots(date, doctor_id)
   b. present options clearly
   c. wait for patient to pick a time
   d. confirm: "Book for [Name] on [date] at [time] with [Doctor]?"
   e. ONLY after patient says yes/சரி/ok — call book_appointment
9. For medical questions (symptoms, dosage, treatment): do NOT answer yourself.
   Tell patient: "I'll pass your question to {doctor_name}. You'll get a reply on WhatsApp soon."
   Then guide them to the "Ask Doctor" option (Reply 6 or say "ask doctor").
10. After a successful booking, reply EXACTLY in this format:
✅ Appointment Confirmed!

🏥 {{clinic_name}}
👤 {{patient_name}} ({{patient_code}})
📅 {{DD Mon YYYY}}
⏰ {{H:MM AM/PM}} | Token {{token}}

Please mention your token when you arrive.
Reply CANCEL to cancel. Reply MENU for help.

11. Never make up information. If a tool fails, tell patient honestly and offer to help via another way.
12. If patient sends "MENU", "BYE", or a number 1-6 — do NOT handle it. Return empty string so state machine takes over."""

    history.append({"role": "user", "content": text})

    client = anthropic.AsyncAnthropic(api_key=api_key)

    try:
        for _ in range(10):  # max 10 tool-call rounds per message
            response = await client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1024,
                system=system_prompt,
                tools=TOOLS,
                messages=history,
            )

            if response.stop_reason == "end_turn":
                reply_text = ""
                for block in response.content:
                    if hasattr(block, "type") and block.type == "text":
                        reply_text = block.text.strip()
                        break

                history.append({"role": "assistant", "content": _serialize_content(response.content)})

                if len(history) > MAX_HISTORY_TURNS * 2:
                    history = _validate_history(history[-(MAX_HISTORY_TURNS * 2):])

                save_conversation_state(mobile, "agent", {
                    "agent_history": history,
                    "agent_ts": now_ist.isoformat(),
                })
                print(f"[AGENT] Reply to {mobile}: {reply_text[:100]}")
                return reply_text or None

            if response.stop_reason != "tool_use":
                break

            tool_results = []
            for block in response.content:
                if hasattr(block, "type") and block.type == "tool_use":
                    print(f"[AGENT] Tool: {block.name}({json.dumps(block.input)[:200]})")
                    result_str = _dispatch_tool(block.name, block.input, doctor_id, clinic_name)
                    print(f"[AGENT] Result: {result_str[:200]}")
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result_str,
                    })

            history.append({"role": "assistant", "content": _serialize_content(response.content)})
            history.append({"role": "user", "content": tool_results})

    except Exception as e:
        print(f"[AGENT ERROR] {e}")
        return None

    return None

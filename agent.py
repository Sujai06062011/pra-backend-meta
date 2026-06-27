"""
agent.py — Claude Agent for Parro Connect clinic WhatsApp bot.

Handles free-form Tamil/English messages autonomously using tool calls.
Called from whatsapp_handler.py only when current_state is "idle" or "agent".
Falls back to None on any error so the state machine remains the safety net.
"""

import os
import json
from datetime import datetime, timedelta
import pytz

IST = pytz.timezone("Asia/Kolkata")
DEFAULT_DOCTOR_ID = "8c33abe0-5d2e-4613-9437-c7c375e8d162"
MAX_HISTORY_TURNS = 10   # keep last 10 message pairs to avoid context blowup
AGENT_TIMEOUT_MINS = 30  # reset agent history after 30 min of inactivity

TOOLS = [
    {
        "name": "get_patient",
        "description": (
            "Find patient(s) registered with a mobile number. "
            "Returns all family members linked to this number. "
            "Always call this first before booking or checking queue."
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
        "name": "get_available_slots",
        "description": (
            "Get available in-clinic appointment slots for a date. "
            "Returns morning and evening slots separately."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": "Date in YYYY-MM-DD format",
                }
            },
            "required": ["date"],
        },
    },
    {
        "name": "book_appointment",
        "description": (
            "Book an in-clinic appointment. "
            "Always show available slots and confirm date+time before calling this."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "patient_id": {"type": "string"},
                "date": {"type": "string", "description": "YYYY-MM-DD"},
                "time": {"type": "string", "description": "HH:MM e.g. 10:15"},
            },
            "required": ["patient_id", "date", "time"],
        },
    },
    {
        "name": "get_upcoming_appointments",
        "description": "Get all upcoming confirmed appointments for a mobile number.",
        "input_schema": {
            "type": "object",
            "properties": {"mobile": {"type": "string"}},
            "required": ["mobile"],
        },
    },
    {
        "name": "get_queue_status",
        "description": "Get today's live queue status and token position for a mobile number.",
        "input_schema": {
            "type": "object",
            "properties": {"mobile": {"type": "string"}},
            "required": ["mobile"],
        },
    },
    {
        "name": "cancel_appointment",
        "description": (
            "Cancel a specific appointment by ID. "
            "Always list upcoming appointments first and confirm with the patient before calling this."
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
            "Register a new patient. Call when get_patient returns found=False. "
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
            },
            "required": ["mobile", "name", "dob", "gender", "language"],
        },
    },
    {
        "name": "add_family_member",
        "description": (
            "Add a new family member to an existing patient account. "
            "Call when patient wants to book for someone not yet in their list. "
            "Collect name, date of birth, gender."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "primary_mobile": {"type": "string", "description": "Mobile of the account holder"},
                "name": {"type": "string"},
                "dob": {"type": "string", "description": "YYYY-MM-DD"},
                "gender": {"type": "string", "enum": ["Male", "Female", "Other"]},
                "relationship": {"type": "string", "description": "e.g. Son, Daughter, Spouse"},
            },
            "required": ["primary_mobile", "name", "dob", "gender"],
        },
    },
]


# ── Tool implementations (call Supabase directly, no MCP HTTP) ────────────────

def _tool_get_patient(mobile: str) -> dict:
    from database import supabase as supa
    result = (
        supa.table("patients")
        .select("id, name, patient_code, date_of_birth, gender, age, language")
        .or_(f"mobile.eq.{mobile},family_head_mobile.eq.{mobile}")
        .eq("doctor_id", DEFAULT_DOCTOR_ID)
        .execute()
    )
    patients = result.data or []
    return {"found": bool(patients), "patients": patients}


def _tool_get_available_slots(date_str: str) -> dict:
    from routers.availability import get_availability_for_date, generate_slots_for_date
    from database import get_booked_slots
    import pytz as _pytz

    av = get_availability_for_date(DEFAULT_DOCTOR_ID, date_str)
    if av.get("is_holiday"):
        return {"available": False, "reason": f"Holiday: {av.get('holiday_name', 'closed')}"}
    if not av.get("morning", {}).get("enabled") and not av.get("evening", {}).get("enabled"):
        return {"available": False, "reason": "Clinic closed on this day"}

    now_ist = datetime.now(_pytz.timezone("Asia/Kolkata"))
    cutoff = now_ist.strftime("%H:%M") if date_str == now_ist.date().isoformat() else ""
    booked = set(get_booked_slots(DEFAULT_DOCTOR_ID, date_str))
    all_slots = generate_slots_for_date(DEFAULT_DOCTOR_ID, date_str)

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


def _tool_book_appointment(patient_id: str, date_str: str, time_str: str) -> dict:
    from database import (
        assign_token_for_slot, create_appointment, get_display_token,
        is_slot_available, get_active_appointment, supabase as supa,
    )
    from whatsapp_handler import format_time

    existing = get_active_appointment(patient_id, DEFAULT_DOCTOR_ID, date_str)
    if existing:
        ex_time = format_time(str(existing.get("appointment_time", ""))[:5])
        return {"success": False, "reason": f"Already has an appointment at {ex_time} on {date_str}"}

    if not is_slot_available(DEFAULT_DOCTOR_ID, date_str, time_str):
        return {"success": False, "reason": "Slot is already taken"}

    token = assign_token_for_slot(DEFAULT_DOCTOR_ID, date_str, time_str)
    appt = create_appointment(patient_id, DEFAULT_DOCTOR_ID, date_str, time_str, token)
    if not appt:
        return {"success": False, "reason": "Booking failed — please try again"}

    display_tok = get_display_token(token, time_str)
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
    from database import get_family_upcoming_appointments, get_display_token
    appts = get_family_upcoming_appointments(mobile, DEFAULT_DOCTOR_ID)
    simplified = [
        {
            "id": a.get("id"),
            "patient_name": a.get("patient_name") or a.get("name", ""),
            "date": str(a.get("appointment_date", ""))[:10],
            "time": str(a.get("appointment_time", ""))[:5],
            "token": get_display_token(a.get("token_number"), a.get("appointment_time")),
            "status": a.get("status", ""),
        }
        for a in appts
    ]
    return {"appointments": simplified, "count": len(simplified)}


def _tool_get_queue_status(mobile: str) -> dict:
    from database import supabase as supa, get_display_token, _time_str, get_slot_config
    import pytz as _pytz

    now = datetime.now(_pytz.timezone("Asia/Kolkata"))
    today = now.date().isoformat()

    tok_res = (
        supa.table("tokens")
        .select("current_token")
        .eq("doctor_id", DEFAULT_DOCTOR_ID)
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
        .eq("doctor_id", DEFAULT_DOCTOR_ID)
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
        .eq("doctor_id", DEFAULT_DOCTOR_ID)
        .eq("appointment_date", today)
        .neq("consultation_type", "online")
        .execute()
        .data or []
    )
    slot_min = get_slot_config().get("duration", 10)

    results = []
    for a in (appts.data or []):
        t = _time_str(a.get("appointment_time"))
        display_tok = get_display_token(a.get("token_number"), a.get("appointment_time"))
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


def _tool_register_patient(mobile: str, name: str, dob: str, gender: str, language: str) -> dict:
    from database import create_patient, supabase as supa
    try:
        new_patient = create_patient(
            mobile, name, dob, gender,
            family_head_mobile=mobile,
            language=language, city="",
            doctor_id=DEFAULT_DOCTOR_ID,
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


def _tool_add_family_member(primary_mobile: str, name: str, dob: str, gender: str, relationship: str = "") -> dict:
    from database import supabase as supa, _next_patient_counter
    import re as _re
    from datetime import date as _date
    from whatsapp_handler import MONTHS

    # Parse DOB → ISO
    dob_iso = None
    birth_year = "0000"
    age = None
    try:
        _dob = dob.strip()
        dob_date = None
        nm = _re.match(r"^(\d{1,2})[/\-\s](\d{1,2})[/\-\s](\d{4})$", _dob)
        if nm:
            dob_date = _date(int(nm.group(3)), int(nm.group(2)), int(nm.group(1)))
        if not dob_date:
            dm = _re.search(r"(\d{1,2})[\s\-/]+([a-zA-Z]+)[\s\-/]+(\d{4})", _dob)
            if dm:
                m_num = MONTHS.get(dm.group(2).lower()[:3])
                if m_num:
                    dob_date = _date(int(dm.group(3)), int(m_num), int(dm.group(1)))
        if not dob_date and len(_dob) == 10 and _dob[4] == "-":
            dob_date = _date.fromisoformat(_dob)
        if dob_date:
            today_d = _date.today()
            age = today_d.year - dob_date.year - (
                (today_d.month, today_d.day) < (dob_date.month, dob_date.day)
            )
            dob_iso = dob_date.isoformat()
            birth_year = str(dob_date.year)
    except Exception:
        pass

    name_part = name[:3].upper().replace(" ", "")
    counter = _next_patient_counter(DEFAULT_DOCTOR_ID)
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
            "doctor_id": DEFAULT_DOCTOR_ID,
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


def _dispatch_tool(name: str, inputs: dict) -> str:
    try:
        if name == "get_patient":
            result = _tool_get_patient(inputs["mobile"])
        elif name == "get_available_slots":
            result = _tool_get_available_slots(inputs["date"])
        elif name == "book_appointment":
            result = _tool_book_appointment(inputs["patient_id"], inputs["date"], inputs["time"])
        elif name == "get_upcoming_appointments":
            result = _tool_get_upcoming_appointments(inputs["mobile"])
        elif name == "get_queue_status":
            result = _tool_get_queue_status(inputs["mobile"])
        elif name == "cancel_appointment":
            result = _tool_cancel_appointment(inputs["appointment_id"])
        elif name == "register_patient":
            result = _tool_register_patient(
                inputs["mobile"], inputs["name"], inputs["dob"],
                inputs["gender"], inputs["language"],
            )
        elif name == "add_family_member":
            result = _tool_add_family_member(
                inputs["primary_mobile"], inputs["name"], inputs["dob"],
                inputs["gender"], inputs.get("relationship", ""),
            )
        else:
            result = {"error": f"Unknown tool: {name}"}
    except Exception as e:
        result = {"error": str(e)}
    return json.dumps(result, ensure_ascii=False, default=str)


# ── History validation ────────────────────────────────────────────────────────

def _validate_history(history: list) -> list:
    """
    Remove any trailing tool_result messages whose tool_use blocks are missing.
    Also drops orphaned tool_use blocks with no following tool_result.
    Returns a clean history safe to send to the Anthropic API.
    """
    if not history:
        return history

    # Collect all tool_use IDs present in assistant messages
    tool_use_ids = set()
    for msg in history:
        if msg.get("role") == "assistant":
            for block in (msg.get("content") or []):
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tool_use_ids.add(block["id"])

    # Remove user messages that contain tool_results referencing unknown tool_use IDs
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
                    continue  # drop entire message if all blocks were orphaned results
                msg = {**msg, "content": filtered}
        clean.append(msg)

    return clean


# ── Content block serialization (for history persistence) ─────────────────────

def _serialize_content(content) -> list:
    """Convert Anthropic SDK content blocks to JSON-safe dicts."""
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


# ── Main agent entry point ────────────────────────────────────────────────────

async def run_clinic_agent(mobile: str, text: str, doctor: dict) -> str | None:
    """
    Run Claude agent for a free-form message.
    Returns a WhatsApp-ready reply string, or None to fall through to state machine.
    Persists conversation history in conversation_state table for multi-turn support.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("[AGENT] ANTHROPIC_API_KEY not set — falling through to state machine")
        return None

    import anthropic
    from database import get_conversation_state, save_conversation_state

    # Load history from DB (persisted across messages)
    _, temp_data = get_conversation_state(mobile)
    history = temp_data.get("agent_history", [])
    last_ts = temp_data.get("agent_ts", "")

    # Expire history after AGENT_TIMEOUT_MINS of inactivity
    if last_ts:
        try:
            last_dt = datetime.fromisoformat(last_ts)
            if (datetime.now(IST) - last_dt).total_seconds() > AGENT_TIMEOUT_MINS * 60:
                history = []
        except Exception:
            history = []

    # Validate: remove orphaned tool_result blocks that have no matching tool_use
    history = _validate_history(history)

    now_ist = datetime.now(IST)
    today_str = now_ist.date().isoformat()
    tomorrow_str = (now_ist.date() + timedelta(days=1)).isoformat()

    clinic_name = doctor.get("clinic_name", "Dr. Kumar Child Care Clinic")
    doctor_name = doctor.get("name", "Dr. Kumar")
    clinic_timings = doctor.get("clinic_timings", "Mon-Sat: 9AM-1PM, 5PM-8PM")
    clinic_address = doctor.get("clinic_address", "Chennai")

    system_prompt = f"""You are a WhatsApp assistant for {clinic_name}, Chennai.
Doctor: {doctor_name} | Speciality: Paediatrics
Timings: {clinic_timings}
Address: {clinic_address}

Today: {today_str} | Tomorrow: {tomorrow_str}
Resolve relative dates: "naalaikku"/"tomorrow" = {tomorrow_str}, "indru"/"today" = {today_str},
"next week" = {(now_ist.date() + timedelta(days=7)).isoformat()}.

RULES:
- Reply in the same language the patient used (Tamil or English — do not mix)
- Keep replies short and WhatsApp-friendly (no markdown bold ** or # headers)
- Always call get_patient(mobile="{mobile}") first to get patient_id before booking
- If get_patient returns found=False: collect name, DOB, gender, language then call register_patient
- If patient wants to book for someone not in their list: collect name+DOB+gender then call add_family_member
- For booking: call get_available_slots first, present options, confirm with patient, then book
- If patient has multiple family members, ask which one the appointment is for
- If intent is unclear, ask exactly ONE clarifying question
- After completing a task, end with a brief next-action hint"""

    # Append current user message to history
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
                # Extract final text reply
                reply_text = ""
                for block in response.content:
                    if hasattr(block, "type") and block.type == "text":
                        reply_text = block.text.strip()
                        break

                # Save assistant turn to history
                history.append({"role": "assistant", "content": _serialize_content(response.content)})

                # Trim to MAX_HISTORY_TURNS message pairs, then re-validate
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

            # Process tool calls, collect results
            tool_results = []
            for block in response.content:
                if hasattr(block, "type") and block.type == "tool_use":
                    print(f"[AGENT] Tool call: {block.name}({json.dumps(block.input)[:200]})")
                    result_str = _dispatch_tool(block.name, block.input)
                    print(f"[AGENT] Tool result: {result_str[:200]}")
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result_str,
                    })

            # Add assistant + tool results to history for next round
            history.append({"role": "assistant", "content": _serialize_content(response.content)})
            history.append({"role": "user", "content": tool_results})

    except Exception as e:
        print(f"[AGENT ERROR] {e}")
        return None

    return None

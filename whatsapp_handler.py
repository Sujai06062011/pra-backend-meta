from datetime import datetime, date, timedelta
import re
from consultation_helpers import (
    is_online_consultation_slot,
    create_consultation_for_appointment,
    send_video_link_to_patient,
    send_meta_list,
    send_meta_buttons,
    send_whatsapp_text,
)
from database import (
    get_doctor_by_whatsapp, get_patient_by_mobile, get_conversation_state,
    save_conversation_state, get_queue_status, get_patient_token_today, get_family_tokens_today,
    check_holiday, get_booked_slots, get_next_token, create_appointment, create_online_appointment,
    get_upcoming_appointments, get_family_upcoming_appointments, cancel_appointment, create_patient,
    get_display_token, assign_token_for_slot, assign_online_token, is_slot_available, _time_str,
    get_slot_config, get_active_appointment,
)
from database import supabase as _supa
from routers.availability import (
    get_availability_for_date, get_next_open_date, generate_slots_for_date,
    fmt12, get_full_clinic_config,
)

MENU_HINT = "\n\nReply MENU for main menu or BYE to end conversation."

# States that must stay in the state machine (active multi-step flows)
_STATE_MACHINE_STATES = {
    "awaiting_name", "awaiting_dob", "awaiting_gender", "awaiting_language", "awaiting_city",
    "awaiting_booking_patient_select", "awaiting_new_member_name", "awaiting_new_member_dob",
    "awaiting_new_member_gender", "awaiting_new_member_language", "awaiting_new_member_city",
    "awaiting_booking_date", "awaiting_date", "awaiting_consult_type",
    "awaiting_slot", "awaiting_session_selection", "awaiting_slot_selection",
    "awaiting_slot_confirmation", "awaiting_cancel_choice", "awaiting_cancel_selection",
    "awaiting_query_patient_select", "awaiting_query_doctor_select", "awaiting_query",
    # multi-doctor states (dormant until feature flag = true)
    "awaiting_doctor_select",
}

# Messages that should always stay in the state machine
_SM_KEYWORDS = {
    "menu", "main menu", "back", "home", "hi", "hello", "hey",
    "start", "help", "bye", "goodbye", "exit", "end", "quit",
    "1", "2", "3", "4", "5", "6",
}


def _should_use_agent(text: str, current_state: str, is_existing: bool) -> bool:
    """Return True when the Claude agent should handle this message."""
    if current_state in _STATE_MACHINE_STATES:
        return False  # Active state machine flow — never interrupt
    if current_state not in ("idle", "agent"):
        return False  # Unknown state — let state machine handle it
    if text.lower().strip() in _SM_KEYWORDS:
        return False  # Menu shortcuts → state machine
    # New patients with simple digit/keyword → state machine registration flow
    if not is_existing and text.lower().strip() in {"1", "2", "3", "4", "5", "6",
                                                     "hi", "hello", "hey", "start"}:
        return False
    return True

MONTHS = {
    "jan": "01", "feb": "02", "mar": "03", "apr": "04",
    "may": "05", "jun": "06", "jul": "07", "aug": "08",
    "sep": "09", "oct": "10", "nov": "11", "dec": "12",
    "january": "01", "february": "02", "march": "03", "april": "04",
    "june": "06", "july": "07", "august": "08", "september": "09",
    "october": "10", "november": "11", "december": "12"
}

ALL_SLOTS = [
    "09:00", "09:15", "09:30", "09:45",
    "10:00", "10:15", "10:30", "10:45",
    "11:00", "11:30", "12:00", "12:30",
    "17:00", "17:30", "18:00", "18:30",
    "19:00", "19:30"
]


def format_time(t: str) -> str:
    h, m = t.split(":")
    hour = int(h)
    ampm = "PM" if hour >= 12 else "AM"
    display = hour - 12 if hour > 12 else hour
    return f"{display}:{m} {ampm}"


def generate_online_slots(doctor: dict, date_str: str, duration: int = 15) -> list:
    """Generate online consultation time slots for a date from the doctor's online hours.
    Returns list of (time_str, session_label) tuples."""
    from datetime import datetime as _dt, timedelta as _td
    hours = doctor.get("online_consultation_hours") or []
    day_name = date.fromisoformat(date_str).strftime("%A").lower()
    day_entry = next((h for h in hours if h.get("day", "").lower() == day_name), None)
    if not day_entry:
        return []
    slots = []

    def _gen(start_str, end_str, session_label):
        sh, sm = map(int, start_str.split(":"))
        eh, em = map(int, end_str.split(":"))
        cur = _dt(2000, 1, 1, sh, sm)
        end_t = _dt(2000, 1, 1, eh, em)
        while cur < end_t:
            slots.append((cur.strftime("%H:%M"), session_label))
            cur += _td(minutes=duration)

    # New format: {day, morning:{enabled,start,end}, evening:{enabled,start,end}}
    if "morning" in day_entry or "evening" in day_entry:
        morning = day_entry.get("morning") or {}
        evening = day_entry.get("evening") or {}
        if morning.get("enabled"):
            _gen(morning["start"], morning["end"], "morning")
        if evening.get("enabled"):
            _gen(evening["start"], evening["end"], "evening")
    else:
        # Legacy format: {day, start, end}
        _gen(day_entry["start"], day_entry["end"], "online")
    return slots


def build_date_options(name_label: str = "") -> tuple:
    """Return (reply_text, date_options_list, date_labels_list, new_state)."""
    today     = date.today()
    tomorrow  = date.fromordinal(today.toordinal() + 1)
    day_after = date.fromordinal(today.toordinal() + 2)
    fmt = "%d %B %Y"
    header = f"📅 Booking for *{name_label}*\n\n" if name_label else ""
    reply = (
        f"{header}Which date?\n\n"
        f"1. Today ({today.strftime(fmt)})\n"
        f"2. Tomorrow ({tomorrow.strftime(fmt)})\n"
        f"3. Day after ({day_after.strftime(fmt)})\n"
        f"4. Other date (reply with date e.g. 15 June 2026)"
        + MENU_HINT
    )
    return (
        reply,
        [today.strftime("%Y-%m-%d"), tomorrow.strftime("%Y-%m-%d"), day_after.strftime("%Y-%m-%d")],
        [today.strftime(fmt), tomorrow.strftime(fmt), day_after.strftime(fmt)],
    )


async def send_date_selection_interactive(
    from_number: str,
    name_label: str,
    date_opts: list,
) -> None:
    """Send date selection as 3 reply buttons: Today, Tomorrow, Other Date."""
    today     = date.today()
    tomorrow  = date.fromordinal(today.toordinal() + 1)
    months    = ["Jan","Feb","Mar","Apr","May","Jun",
                 "Jul","Aug","Sep","Oct","Nov","Dec"]
    first_name = name_label.split()[0] if name_label else "you"

    today_title    = f"Today {today.day} {months[today.month-1]}"
    tomorrow_title = f"Tomorrow {tomorrow.day} {months[tomorrow.month-1]}"

    await send_meta_buttons(
        to_number=from_number,
        body_text=f"📅 Booking for {first_name}\n\nWhich date?",
        buttons=[
            {"id": f"date_today_{date_opts[0]}",    "title": today_title[:20]},
            {"id": f"date_tomorrow_{date_opts[1]}", "title": tomorrow_title[:20]},
            {"id": "date_other",                     "title": "Other Date"},
        ],
        footer_text="Tap Other Date to choose a different day",
    )


def parse_date(text: str):
    lower = text.lower()
    day = None
    month = None
    year = date.today().year

    day_match = re.search(r"(\d{1,2})(st|nd|rd|th)?", lower)
    if day_match:
        day = day_match.group(1).zfill(2)

    for name, num in MONTHS.items():
        if name in lower:
            month = num
            break

    year_match = re.search(r"202[5-9]", lower)
    if year_match:
        year = year_match.group(0)

    if not day or not month:
        return None, "Could not understand date. Please try again.\n\nExample: 10 June 2026"

    return f"{year}-{month}-{day}", None


def format_display_date(date_str: str) -> str:
    """Convert '2026-06-20' → 'Sat 20 Jun'."""
    try:
        d = date.fromisoformat(date_str)
        days   = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]
        months = ["Jan","Feb","Mar","Apr","May","Jun",
                  "Jul","Aug","Sep","Oct","Nov","Dec"]
        return f"{days[d.weekday()]} {d.day} {months[d.month-1]}"
    except Exception:
        return date_str


def _format_appt_button_title(appt: dict) -> str:
    """'Subramaniam M5' — max 20 chars for reply button title."""
    name = appt.get("patient_name", appt.get("name", "Patient"))
    first = name.split()[0]
    token = appt.get("display_token", appt.get("token_number", ""))
    doc_name = appt.get("doctor_name", "")
    doc_short = f" {doc_name.split()[-1]}" if doc_name else ""  # last word e.g. "Kumar"/"Deepa"
    return f"{first} {token}{doc_short}"[:20]


def _format_appt_time_title(appt: dict) -> str:
    """'Subramaniam 10AM' — max 20 chars, used when token unknown."""
    name = appt.get("patient_name", appt.get("name", "Patient"))
    first = name.split()[0]
    time_str = str(appt.get("appointment_time", ""))[:5]
    try:
        hour = int(time_str.split(":")[0])
        period = "AM" if hour < 12 else "PM"
        h = hour if hour <= 12 else hour - 12
        if h == 0:
            h = 12
        display_time = f"{h}{period}"
    except Exception:
        display_time = time_str
    doc_name = appt.get("doctor_name", "")
    doc_short = f" {doc_name.split()[-1]}" if doc_name else ""
    return f"{first} {display_time}{doc_short}"[:20]


def _format_appt_row_description(appt: dict) -> str:
    """'18 Jun · 10:15 AM · Token M5' — max 72 chars for list row."""
    from database import get_display_token
    months = ["Jan","Feb","Mar","Apr","May","Jun",
              "Jul","Aug","Sep","Oct","Nov","Dec"]
    date_str = str(appt.get("appointment_date", ""))[:10]
    time_str = str(appt.get("appointment_time", ""))[:5]
    token_num = appt.get("token_number", "")
    parts = []
    try:
        d = date.fromisoformat(date_str)
        parts.append(f"{d.day} {months[d.month-1]}")
    except Exception:
        if date_str:
            parts.append(date_str)
    if time_str:
        parts.append(format_time(time_str))
    if token_num:
        d_tok = get_display_token(token_num, appt.get("appointment_time", ""),
                                   doctor_id=appt.get("doctor_id"), date_str=date_str)
        parts.append(f"Token {d_tok}")
    return " · ".join(parts)[:72]


async def send_cancel_appointment_list(
    from_number: str,
    appointments: list,
) -> None:
    """Send upcoming appointments as interactive buttons (≤3) or list (>3)."""
    from database import get_display_token
    total = len(appointments)
    if total <= 3:
        buttons = []
        body_lines = ["Your upcoming appointments:\n"]
        for i, appt in enumerate(appointments, 1):
            token_num = appt.get("token_number", "")
            d_tok = get_display_token(token_num, appt.get("appointment_time", ""),
                                       doctor_id=appt.get("doctor_id"),
                                       date_str=str(appt.get("appointment_date", ""))[:10]) if token_num else ""
            appt_with_tok = {**appt, "display_token": d_tok}
            buttons.append({
                "id": f"cancel_{appt['id']}",
                "title": _format_appt_button_title(appt_with_tok) if d_tok else _format_appt_time_title(appt),
            })
            desc = _format_appt_row_description(appt_with_tok)
            name = appt.get("patient_name", appt.get("name", ""))
            doc_name = appt.get("doctor_name", "")
            doc_suffix = f" · {doc_name}" if doc_name else ""
            body_lines.append(f"{i}. {name}{doc_suffix} — {desc}")
        body_lines.append("\nWhich would you like to cancel?")
        await send_meta_buttons(
            to_number=from_number,
            body_text="\n".join(body_lines),
            buttons=buttons,
            footer_text="Reply MENU to go back",
        )
    else:
        rows = []
        for appt in appointments[:9]:
            name = appt.get("patient_name", appt.get("name", "Patient"))
            doc_name = appt.get("doctor_name", "")
            title = (f"{name} · {doc_name}" if doc_name else name)[:24]
            rows.append({
                "id": f"cancel_{appt['id']}",
                "title": title,
                "description": _format_appt_row_description(appt),
            })
        await send_meta_list(
            to_number=from_number,
            body_text=(
                f"You have {total} upcoming appointments.\n"
                "Which would you like to cancel?"
            ),
            button_label="Select appointment",
            sections=[{"title": "Upcoming appointments", "rows": rows}],
            footer_text="Reply MENU to go back",
        )


async def send_slot_list(
    from_number: str,
    slots: list,
    selected_date: str,
    session: str,
    offset: int = 0,
    patient_name: str = "",
) -> None:
    """Send up to 5 slots as a WhatsApp interactive list. Adds 'See more' if needed."""
    PAGE_SIZE    = 5
    page_slots   = slots[offset: offset + PAGE_SIZE]
    remaining    = slots[offset + PAGE_SIZE:]
    session_icon  = "🌅" if session == "morning" else "🌙"
    session_label = "Morning" if session == "morning" else "Evening"
    first_name    = patient_name.split()[0] if patient_name else ""
    name_part     = f"for {first_name}" if first_name else ""

    rows = []
    for slot in page_slots:
        slot_time = str(slot)[:5]
        rows.append({
            "id":          f"slot_{selected_date}_{slot_time}",
            "title":       format_time(slot_time),
            "description": f"{session_label} · {format_display_date(selected_date)}",
        })

    if remaining:
        rows.append({
            "id":          f"slots_more_{selected_date}_{session}_{offset + PAGE_SIZE}",
            "title":       "See more slots →",
            "description": f"{len(remaining)} more available",
        })

    await send_meta_list(
        to_number=from_number,
        body_text=(
            f"{session_icon} {session_label} slots {name_part}\n"
            f"📅 {format_display_date(selected_date)}"
        ),
        button_label="Choose a slot",
        sections=[{"title": f"{session_label} slots", "rows": rows}],
        footer_text="Tap a slot to select it",
    )


async def send_session_or_slot_ui(
    from_number: str,
    morning_slots: list,
    evening_slots: list,
    parsed_date: str,
    booking_name: str,
    base_temp: dict,
) -> tuple:
    """
    Send session selection buttons (both sessions) or jump straight to slot list
    (single session). Returns (new_state, new_temp) for the caller to set.
    """
    has_morning = bool(morning_slots)
    has_evening = bool(evening_slots)

    if has_morning and has_evening:
        await send_meta_buttons(
            to_number=from_number,
            body_text=f"📅 {format_display_date(parsed_date)}\n\nWhich session?",
            buttons=[
                {"id": "session_morning", "title": "🌅 Morning"},
                {"id": "session_evening", "title": "🌙 Evening"},
            ],
            footer_text=(
                f"Morning: {len(morning_slots)} slots  "
                f"Evening: {len(evening_slots)} slots"
            ),
        )
        slot_temp = {
            **base_temp,
            "morning_slots": morning_slots,
            "evening_slots": evening_slots,
        }
        return "awaiting_session_selection", slot_temp

    session = "morning" if has_morning else "evening"
    slots   = morning_slots if has_morning else evening_slots
    slot_temp = {
        **base_temp,
        "morning_slots": morning_slots,
        "evening_slots": evening_slots,
        "session": session,
    }
    await send_slot_list(from_number, slots, parsed_date, session, 0, booking_name)
    return "awaiting_slot_selection", slot_temp


def build_main_menu(patient_name: str, clinic_name: str) -> str:
    # Kept as fallback for non-interactive contexts (e.g. concatenated into other messages)
    return (
        f"👋 Welcome to\n🏥 *{clinic_name}*\n\n"
        f"1️⃣ Book Appointment\n"
        f"2️⃣ Queue Status\n"
        f"3️⃣ Cancel Appointment\n"
        f"4️⃣ Clinic Timings\n"
        f"5️⃣ Speak to Receptionist\n"
        f"6️⃣ Ask Doctor a Question\n\n"
        f"Reply with a number.\n"
        f"Reply MENU for main menu or BYE to end."
    )


def get_main_menu_sections() -> list:
    return [
        {
            "title": "Clinic Services",
            "rows": [
                {"id": "menu_book_appointment",   "title": "Book Appointment",      "description": "Schedule an in-clinic or online visit"},
                {"id": "menu_queue_status",        "title": "Queue Status",           "description": "Check your position in today's queue"},
                {"id": "menu_cancel_appointment",  "title": "Cancel Appointment",     "description": "Cancel an existing booking"},
                {"id": "menu_clinic_timings",      "title": "Clinic Timings",         "description": "View our opening hours"},
                {"id": "menu_receptionist",        "title": "Speak to Receptionist",  "description": "Connect with our front desk"},
                {"id": "menu_ask_doctor",          "title": "Ask Doctor a Question",  "description": "Send a medical query to the doctor"},
            ],
        }
    ]


async def send_main_menu(from_number: str, clinic_name: str):
    """Send the main menu as a Meta interactive list message."""
    await send_meta_list(
        to_number=from_number,
        body_text=f"👋 Welcome to\n🏥 *{clinic_name}*\n\nHow can we help you today?",
        button_label="View options",
        sections=get_main_menu_sections(),
        footer_text="Reply BYE to end conversation",
    )


def get_all_linked_patients(mobile: str) -> list:
    """Return all patients whose mobile = {mobile} OR family_head_mobile = {mobile}, ordered by created_at ASC."""
    result = _supa.table("patients").select(
        "id, name, age, gender, patient_code, date_of_birth"
    ).or_(
        f"mobile.eq.{mobile},family_head_mobile.eq.{mobile}"
    ).order("created_at", desc=False).execute()
    return result.data or []


def get_active_prescription_doctors(patients: list) -> dict:
    """Return {patient_id: [{"id": doctor_id, "name": ..., "specialty": ...}]} for active prescriptions.

    A prescription is active when at least one medicine's course is still running today.
    Only patients with >= 1 active prescription are included in the result.
    """
    if not patients:
        return {}
    import pytz
    today = datetime.now(pytz.timezone("Asia/Kolkata")).date()
    ids = [p["id"] for p in patients]
    res = _supa.table("prescriptions").select(
        "patient_id, doctor_id, prescription_date, prescription_medicines(duration_days)"
    ).in_("patient_id", ids).execute()

    # patient_id → set of active doctor_ids
    active_map: dict[str, set] = {}
    for pres in (res.data or []):
        pres_date_str = pres.get("prescription_date") or ""
        if not pres_date_str:
            continue
        try:
            pres_date = datetime.strptime(pres_date_str, "%Y-%m-%d").date()
        except ValueError:
            continue
        for med in pres.get("prescription_medicines") or []:
            duration = med.get("duration_days") or 1
            if pres_date + timedelta(days=duration - 1) >= today:
                pid = pres["patient_id"]
                did = pres.get("doctor_id")
                if did:
                    active_map.setdefault(pid, set()).add(did)
                break

    if not active_map:
        return {}

    # Fetch doctor details for all active doctor IDs
    all_doctor_ids = list({did for dids in active_map.values() for did in dids})
    doc_res = _supa.table("doctors").select("id, name, specialty_display, speciality").in_("id", all_doctor_ids).execute()
    doctor_lookup = {d["id"]: d for d in (doc_res.data or [])}

    result = {}
    for pid, dids in active_map.items():
        result[pid] = [
            {
                "id": did,
                "name": doctor_lookup.get(did, {}).get("name", "Doctor"),
                "specialty": (doctor_lookup.get(did, {}).get("specialty_display") or
                              doctor_lookup.get(did, {}).get("speciality", "")),
            }
            for did in dids
            if did in doctor_lookup
        ]
    return result


def filter_patients_with_active_prescriptions(patients: list) -> list:
    """Legacy wrapper — returns patients list filtered to those with active prescriptions."""
    active_map = get_active_prescription_doctors(patients)
    return [p for p in patients if p["id"] in active_map]


def build_patient_select_msg(patients: list) -> str:
    lines = "📅 Book Appointment\n\nWho is this appointment for?\n\n"
    for i, p in enumerate(patients, 1):
        code = f" ({p['patient_code']})" if p.get("patient_code") else ""
        age  = f" · {p['age']} yrs" if p.get("age") else ""
        lines += f"{i}. {p['name']}{code}{age}\n"
    lines += f"{len(patients) + 1}. ➕ Add new family member\n"
    lines += "\nReply with a number.\nReply MENU for main menu."
    return lines



def _member_age(member: dict) -> int:
    dob = member.get("date_of_birth", "")
    if not dob:
        return member.get("age") or 0
    try:
        from datetime import date as _date
        dob_date = _date.fromisoformat(str(dob)[:10])
        today = _date.today()
        return today.year - dob_date.year - (
            (today.month, today.day) < (dob_date.month, dob_date.day)
        )
    except Exception:
        return member.get("age") or 0


def format_member_button_title(member: dict) -> str:
    first_name = member.get("name", "").split()[0]
    age = _member_age(member)
    gender = member.get("gender", "")
    gender_short = "M" if gender == "Male" else "F" if gender == "Female" else ""
    return f"{first_name} {age}{gender_short}".strip()[:20]


def format_member_list_description(member: dict) -> str:
    age = _member_age(member)
    code = member.get("patient_code", "")
    desc = f"{age} yrs · {code}" if age > 0 else code
    return desc[:72]


async def send_patient_select_interactive(from_number: str, all_patients: list):
    """Send patient selection as interactive buttons (≤2 members) or list (3-9) or plain text (10+)."""
    total_options = len(all_patients) + 1  # +1 for "Add new member"

    if total_options <= 3:
        buttons = [
            {"id": f"member_{p['id']}", "title": format_member_button_title(p)}
            for p in all_patients
        ]
        buttons.append({"id": "member_new", "title": "Add new member"})
        await send_meta_buttons(
            to_number=from_number,
            body_text="📅 Book Appointment\n\nWho is this appointment for?",
            buttons=buttons,
            footer_text="Reply MENU for main menu",
        )

    elif total_options <= 10:
        rows = [
            {
                "id": f"member_{p['id']}",
                "title": p.get("name", "")[:24],
                "description": format_member_list_description(p),
            }
            for p in all_patients
        ]
        rows.append({
            "id": "member_new",
            "title": "Add new family member",
            "description": "Register a new patient",
        })
        await send_meta_list(
            to_number=from_number,
            body_text="📅 Book Appointment\n\nWho is this appointment for?",
            button_label="Select patient",
            sections=[{"title": "Family members", "rows": rows}],
            footer_text="Reply MENU for main menu",
        )

    else:
        # 10+ members — plain text fallback
        return build_patient_select_msg(all_patients)

    return None  # message already sent interactively


async def handle_message(from_number: str, text: str, to_number: str, media_url: str = ""):
    text = text.strip()
    t = text.lower().strip()

    # Get doctor by WhatsApp number
    doctor = get_doctor_by_whatsapp(to_number)
    if not doctor:
        return "Sorry, this clinic is not registered. Please contact support."

    doctor_id     = doctor["id"]
    doctor_name   = doctor["name"]
    clinic_name   = doctor["clinic_name"]
    clinic_timings = doctor.get("clinic_timings", "Mon-Sat: 9AM-1PM, 5PM-8PM")
    clinic_address = doctor.get("clinic_address", "")

    # Get patient
    patient    = get_patient_by_mobile(from_number)
    is_existing = patient is not None
    patient_name = patient["name"] if patient else ""
    patient_id   = patient["id"]   if patient else ""

    # Get conversation state
    current_state, temp_data = get_conversation_state(from_number)

    # ── MULTI-DOCTOR: override doctor context if patient selected a specific doctor ──
    _sel_did = (temp_data or {}).get("selected_doctor_id")
    if _sel_did and _sel_did != doctor_id:
        try:
            from multi_doctor import get_doctor_by_id as _md_get_doctor
            _sel_doc = await _md_get_doctor(_supa, _sel_did)
            if _sel_doc:
                doctor = {**doctor, **{k: v for k, v in _sel_doc.items() if v is not None}}
                doctor_id = _sel_did
                doctor_name = _sel_doc.get("name", doctor_name)
                clinic_name = _sel_doc.get("clinic_name", clinic_name)
        except Exception as _mde:
            print(f"[MULTI_DOCTOR] doctor override failed: {_mde}")

    # ── GLOBAL COMMANDS (always reset state) ─────────────────
    if t in ["menu", "main menu", "back", "home",
             "hi", "hello", "hey", "start", "help"]:
        save_conversation_state(from_number, "idle", {})
        await send_main_menu(from_number, clinic_name)
        return None

    if t in ["bye", "goodbye", "exit", "end", "quit"]:
        save_conversation_state(from_number, "idle", {})
        return f"Thank you for contacting {clinic_name}. 🙏\n\nStay healthy! Reply Hi anytime to start again."

    # ── FOLLOWUP REPLY CAPTURE (idle + 1/2/3) ────────────────
    if current_state == "idle" and t in ["1", "2", "3"]:
        from followup import save_followup_reply, has_pending_followup
        if has_pending_followup(from_number):
            if t == "3":
                save_followup_reply(from_number, t)
                save_conversation_state(from_number, "idle", {})
                await send_whatsapp_text(from_number, "No problem! Let us book an appointment for you. 🏥")
                await send_main_menu(from_number, clinic_name)
                return None
            save_followup_reply(from_number, t)
            responses = {
                "1": f"Wonderful! We are glad you are feeling better. 😊\n\nStay healthy!\n- {clinic_name}",
                "2": f"We hope you feel better soon. 🙏\n\nPlease rest well and follow the diet instructions.\n- {clinic_name}",
            }
            save_conversation_state(from_number, "idle", {})
            return responses.get(t, "Thank you for your response!")

    # ── CLAUDE AGENT (free-form natural language, idle/agent state only) ─────
    if _should_use_agent(text, current_state, is_existing):
        try:
            from agent import run_clinic_agent
            agent_reply = await run_clinic_agent(from_number, text, doctor)
            if agent_reply:
                return agent_reply
            # None → fall through to state machine below
        except Exception as _ae:
            print(f"[AGENT FALLBACK] {_ae}")
    # ─────────────────────────────────────────────────────────

    # ── INTENT DETECTION ──────────────────────────────────────
    intent = "menu"

    if current_state == "awaiting_name":
        intent = "name_provided"
    elif current_state == "awaiting_dob":
        intent = "dob_provided"
    elif current_state == "awaiting_gender":
        intent = "gender_provided"
    elif current_state == "awaiting_language":
        intent = "language_provided"
    elif current_state == "awaiting_city":
        intent = "city_provided"
    elif current_state == "awaiting_booking_patient_select":
        intent = "booking_patient_selected"
    elif current_state == "awaiting_new_member_name":
        intent = "new_member_name_provided"
    elif current_state == "awaiting_new_member_dob":
        intent = "new_member_dob_provided"
    elif current_state == "awaiting_new_member_gender":
        intent = "new_member_gender_provided"
    elif current_state == "awaiting_new_member_language":
        intent = "new_member_language_provided"
    elif current_state == "awaiting_new_member_city":
        intent = "new_member_city_provided"
    elif current_state == "awaiting_booking_date":
        intent = "date_provided"
    elif current_state == "awaiting_date":
        intent = "date_provided"
    elif current_state == "awaiting_consult_type":
        intent = "consult_type_selected"
    elif current_state == "awaiting_slot":
        intent = "slot_selected"
    elif current_state == "awaiting_session_selection":
        intent = "session_text_input"
    elif current_state == "awaiting_slot_selection":
        intent = "slot_text_input"
    elif current_state == "awaiting_slot_confirmation":
        intent = "slot_confirm_text_input"
    elif current_state in ("awaiting_cancel_choice", "awaiting_cancel_selection"):
        intent = "cancel_choice"
    elif current_state == "awaiting_query_patient_select":
        intent = "query_patient_selected"
    elif current_state == "awaiting_query_doctor_select":
        intent = "query_doctor_selected"
    elif current_state == "awaiting_query":
        intent = "query_text_provided"
    elif current_state == "awaiting_doctor_select":
        intent = "doctor_selected"
    # Legacy family states — redirect to new booking flow
    elif current_state in ("awaiting_family_choice", "awaiting_family_name",
                           "awaiting_family_dob", "awaiting_family_gender"):
        intent = "book"
    elif media_url:
        intent = "media"
    elif t == "1" or any(k in t for k in ["book", "appointment"]):
        intent = "book"
    elif t == "2" or any(k in t for k in ["queue", "status", "token", "wait"]):
        intent = "queue"
    elif t == "3" or "cancel" in t:
        intent = "cancel"
    elif t == "4" or any(k in t for k in ["timing", "hour", "open", "close"]):
        intent = "timing"
    elif t == "5" or any(k in t for k in ["speak", "receptionist", "staff"]):
        intent = "speak"
    elif t == "6" or any(k in t for k in ["ask", "question", "query", "doctor"]):
        intent = "ask_question"

    # ── BUILD REPLY ───────────────────────────────────────────
    reply    = ""
    new_state = "idle"
    new_temp  = {}

    # ── NEW PATIENT ───────────────────────────────────────────
    if not is_existing and current_state == "idle":
        reply = (
            f"Welcome to {clinic_name}! 🙏\n\n"
            f"We noticed you are a new patient. Let us register you quickly.\n\n"
            f"Please reply with your Full Name."
        )
        new_state = "awaiting_name"

    # ── REGISTRATION FLOW ─────────────────────────────────────
    elif intent == "name_provided":
        reply = f"Thank you {text}! 😊\n\nPlease share your Date of Birth.\n\n(e.g. 15 Jun 1990)"
        new_state = "awaiting_dob"
        new_temp  = {"name": text}

    elif intent == "dob_provided":
        await send_meta_buttons(
            to_number=from_number,
            body_text="What is your gender?",
            buttons=[
                {"id": "gender_male",   "title": "Male"},
                {"id": "gender_female", "title": "Female"},
                {"id": "gender_other",  "title": "Other"},
            ],
        )
        new_state = "awaiting_gender"
        new_temp  = {**temp_data, "dob": text}

    elif intent == "gender_provided":
        if t in ["male", "m", "1"]:
            gender = "Male"
        elif t in ["female", "f", "2"]:
            gender = "Female"
        else:
            gender = "Other"
        await send_meta_buttons(
            to_number=from_number,
            body_text="What is your preferred language?",
            buttons=[
                {"id": "lang_tamil",   "title": "Tamil"},
                {"id": "lang_english", "title": "English"},
                {"id": "lang_hindi",   "title": "Hindi"},
            ],
        )
        new_state = "awaiting_language"
        new_temp  = {**temp_data, "gender": gender}

    elif intent == "language_provided":
        lang_map = {"1": "tamil", "2": "english", "3": "hindi",
                    "tamil": "tamil", "english": "english", "hindi": "hindi"}
        language = lang_map.get(t, "english")
        reply = "Which city are you from?\n\n(e.g. Chennai)"
        new_state = "awaiting_city"
        new_temp  = {**temp_data, "language": language}

    elif intent == "city_provided":
        city     = text.strip()
        name     = temp_data.get("name", "")
        dob      = temp_data.get("dob", "")
        gender   = temp_data.get("gender", "")
        language = temp_data.get("language", "english")
        new_patient = create_patient(from_number, name, dob, gender,
                                     family_head_mobile=from_number,
                                     language=language, city=city,
                                     doctor_id=doctor["id"] if doctor else "")
        patient_code = new_patient.get("patient_code", "") if new_patient else ""
        patient_id   = new_patient["id"] if new_patient else ""
        await send_whatsapp_text(from_number,
            f"✅ Welcome {name}! You are now registered.\n\n"
            f"🪪 Patient Code: *{patient_code}*\n\n"
            f"What would you like to do next?")
        await send_main_menu(from_number, clinic_name)
        new_state = "idle"

    # ── MAIN MENU ─────────────────────────────────────────────
    elif intent == "menu":
        await send_main_menu(from_number, clinic_name)
        new_state = "idle"

    # ── BOOK APPOINTMENT — unified flow ───────────────────────
    elif intent == "book":
        # NEW: multi-doctor branch (dormant until feature flag = true)
        try:
            from multi_doctor import get_clinic_doctors, build_doctor_selection_message
            _md_doctors = await get_clinic_doctors(_supa, to_number)
            print(f"[MULTI_DOCTOR] eligible doctors={[d.get('name') for d in _md_doctors]}")
            if len(_md_doctors) >= 2:
                    _msg = build_doctor_selection_message(_md_doctors)
                    await send_meta_list(
                        to_number=from_number,
                        header_text=_msg["header"],
                        body_text=_msg["body"],
                        footer_text=_msg["footer"],
                        button_label=_msg["action"]["button"],
                        sections=_msg["action"]["sections"],
                    )
                    new_state = "awaiting_doctor_select"
                    new_temp  = {}
                    save_conversation_state(from_number, new_state, new_temp)
                    return None
        except Exception as _mde:
            print(f"[MULTI_DOCTOR] book branch error (falling through): {_mde!r}")

        # EXISTING: single doctor flow — untouched
        all_patients = get_all_linked_patients(from_number)
        fallback_text = await send_patient_select_interactive(from_number, all_patients)
        reply     = fallback_text or ""
        new_state = "awaiting_booking_patient_select"
        new_temp  = {"booking_patients": all_patients}

    # ── DOCTOR SELECTED (multi-doctor flow) ───────────────────
    elif intent == "doctor_selected":
        try:
            from multi_doctor import get_doctor_by_id as _md_get_doc
            raw_id = text.strip()
            if raw_id.startswith("doctor_"):
                raw_id = raw_id[len("doctor_"):]
            _chosen_doc = await _md_get_doc(_supa, raw_id) if raw_id else {}
            if not _chosen_doc:
                reply     = "Sorry, that selection was not recognised. Please try again.\n\nReply MENU to start over."
                new_state = "idle"
            else:
                # Store selected doctor and continue to patient/date selection
                new_state = "awaiting_booking_patient_select"
                all_patients = get_all_linked_patients(from_number)
                fallback_text = await send_patient_select_interactive(from_number, all_patients)
                reply    = fallback_text or ""
                new_temp = {
                    "booking_patients": all_patients,
                    "selected_doctor_id": raw_id,
                    "selected_doctor_name": _chosen_doc.get("name", ""),
                }
        except Exception as _mde:
            print(f"[MULTI_DOCTOR] doctor_selected error: {_mde}")
            reply     = "Sorry, something went wrong. Please reply MENU to start over."
            new_state = "idle"

    # ── PATIENT SELECTED for booking ──────────────────────────
    elif intent == "booking_patient_selected":
        all_patients = temp_data.get("booking_patients", [])
        _md_carry = {"selected_doctor_id": temp_data["selected_doctor_id"]} if temp_data.get("selected_doctor_id") else {}
        try:
            choice = int(t) - 1
            if choice == len(all_patients):
                # Add new family member
                reply     = "Please enter the new family member's full name:"
                new_state = "awaiting_new_member_name"
                new_temp  = {**_md_carry}
            elif 0 <= choice < len(all_patients):
                p = all_patients[choice]
                base_temp = {"booking_for": p["id"], "booking_name": p["name"], **_md_carry}
                if doctor.get("online_consultation_enabled"):
                    await send_meta_buttons(
                        to_number=from_number,
                        body_text="Would you like to book:",
                        buttons=[
                            {"id": "visit_in_clinic", "title": "In Clinic"},
                            {"id": "visit_online",    "title": "Online Consultation"},
                        ],
                        footer_text="Reply MENU for main menu",
                    )
                    new_state = "awaiting_consult_type"
                    new_temp  = base_temp
                else:
                    _, date_opts, date_labels = build_date_options(p["name"])
                    await send_date_selection_interactive(from_number, p["name"], date_opts)
                    new_state = "awaiting_booking_date"
                    new_temp  = {**base_temp, "consult_type": "in_clinic",
                                 "date_options": date_opts, "date_labels": date_labels}
            else:
                reply     = build_patient_select_msg(all_patients)
                new_state = "awaiting_booking_patient_select"
                new_temp  = temp_data
        except ValueError:
            reply     = build_patient_select_msg(all_patients)
            new_state = "awaiting_booking_patient_select"
            new_temp  = temp_data

    # ── NEW FAMILY MEMBER — multi-step ────────────────────────
    elif intent == "new_member_name_provided":
        reply     = f"What is their date of birth?\n\n(e.g. 15 Jun 1990)"
        new_state = "awaiting_new_member_dob"
        _md_carry = {"selected_doctor_id": temp_data["selected_doctor_id"]} if temp_data.get("selected_doctor_id") else {}
        new_temp  = {"new_name": text, **_md_carry}

    elif intent == "new_member_dob_provided":
        await send_meta_buttons(
            to_number=from_number,
            body_text="What is their gender?",
            buttons=[
                {"id": "gender_male",   "title": "Male"},
                {"id": "gender_female", "title": "Female"},
                {"id": "gender_other",  "title": "Other"},
            ],
        )
        new_state = "awaiting_new_member_gender"
        new_temp  = {**temp_data, "new_dob": text}

    elif intent == "new_member_gender_provided":
        await send_meta_buttons(
            to_number=from_number,
            body_text="What is their preferred language?",
            buttons=[
                {"id": "lang_tamil",   "title": "Tamil"},
                {"id": "lang_english", "title": "English"},
                {"id": "lang_hindi",   "title": "Hindi"},
            ],
        )
        new_state = "awaiting_new_member_language"
        new_temp  = {**temp_data, "new_gender": text}

    elif intent == "new_member_language_provided":
        lang_map = {"1": "tamil", "2": "english", "3": "hindi",
                    "tamil": "tamil", "english": "english", "hindi": "hindi"}
        language = lang_map.get(t, "english")
        reply = "Which city are they from?\n\n(e.g. Chennai)"
        new_state = "awaiting_new_member_city"
        new_temp  = {**temp_data, "new_language": language}

    elif intent == "new_member_city_provided":
        city       = text.strip()
        raw_name   = temp_data.get("new_name", "")
        raw_dob    = temp_data.get("new_dob", "")
        raw_gender = temp_data.get("new_gender", "Male")
        language   = temp_data.get("new_language", "english")
        gender_clean = "Male" if raw_gender.lower().startswith("m") else (
                        "Female" if raw_gender.lower().startswith("f") else "Other")

        # Parse DOB: "15 Jun 1990", "15/06/1990", "15-06-1990", "15 June 1990"
        dob_iso  = None
        age      = None
        birth_year = "0000"
        try:
            _dob_text = raw_dob.strip()
            dob_date  = None
            import re as _re
            # Try numeric formats: DD/MM/YYYY, DD-MM-YYYY, or DD MM YYYY
            _nm = _re.match(r"^(\d{1,2})[/\-\s](\d{1,2})[/\-\s](\d{4})$", _dob_text)
            if _nm:
                try:
                    dob_date = date(int(_nm.group(3)), int(_nm.group(2)), int(_nm.group(1)))
                except (ValueError, IndexError):
                    pass
            # Try "15 Jun 1990", "15 June 1990", or "15-Jun-1990" format
            if not dob_date:
                _dm = _re.search(r"(\d{1,2})[\s\-/]+([a-zA-Z]+)[\s\-/]+(\d{4})", _dob_text)
                if _dm:
                    _d, _m_name, _yr = _dm.group(1), _dm.group(2).lower()[:3], _dm.group(3)
                    _m_num = MONTHS.get(_m_name)
                    if _m_num:
                        dob_date = date(int(_yr), int(_m_num), int(_d))
            if dob_date:
                today_d    = date.today()
                age        = today_d.year - dob_date.year - (
                    (today_d.month, today_d.day) < (dob_date.month, dob_date.day)
                )
                dob_iso    = dob_date.isoformat()
                birth_year = str(dob_date.year)
        except Exception as _e:
            print(f"⚠️ DOB parse error: {_e}")

        from database import _next_patient_counter
        doctor_id_val = doctor["id"] if doctor else ""
        name_part  = raw_name[:3].upper().replace(" ", "")
        counter    = _next_patient_counter(doctor_id_val) if doctor_id_val else 1
        patient_code = f"{name_part}-{birth_year}-{counter}"

        try:
            ins_row = {
                "mobile": from_number,
                "whatsapp_number": from_number,
                "name": raw_name,
                "date_of_birth": dob_iso,
                "age": age,
                "gender": gender_clean,
                "language": language,
                "city": city,
                "patient_code": patient_code,
                "family_head_mobile": from_number,
                "registration_source": "whatsapp",
            }
            if doctor_id_val:
                ins_row["doctor_id"] = doctor_id_val
            ins = _supa.table("patients").insert(ins_row).execute()
            new_pid = ins.data[0]["id"] if ins.data else ""
        except Exception as _e:
            print(f"❌ Family member insert error: {_e}")
            new_pid = ""

        age_str = f"{age} yrs" if age is not None else "unknown"
        reg_msg = (
            f"✅ {raw_name} has been added!\n\n"
            f"🪪 Patient Code: *{patient_code}*\n"
            f"🎂 {age_str} · {gender_clean}\n\n"
            f"What would you like to do next?"
        )
        _md_carry = {"selected_doctor_id": temp_data["selected_doctor_id"]} if temp_data.get("selected_doctor_id") else {}
        base_temp = {"booking_for": new_pid, "booking_name": raw_name, **_md_carry}
        if doctor.get("online_consultation_enabled"):
            await send_whatsapp_text(from_number, reg_msg)
            await send_meta_buttons(
                to_number=from_number,
                body_text="Would you like to book:",
                buttons=[
                    {"id": "visit_in_clinic", "title": "In Clinic"},
                    {"id": "visit_online",    "title": "Online Consultation"},
                ],
                footer_text="Reply MENU for main menu",
            )
            new_state = "awaiting_consult_type"
            new_temp  = base_temp
        else:
            _, date_opts, date_labels = build_date_options(raw_name)
            await send_whatsapp_text(from_number, reg_msg)
            await send_date_selection_interactive(from_number, raw_name, date_opts)
            new_state = "awaiting_booking_date"
            new_temp  = {**base_temp, "consult_type": "in_clinic",
                         "date_options": date_opts, "date_labels": date_labels}

    # ── CONSULT TYPE SELECTED ─────────────────────────────────
    elif intent == "consult_type_selected":
        booking_for  = temp_data.get("booking_for", patient_id)
        booking_name = temp_data.get("booking_name", patient_name)
        if t == "1":
            consult_type = "in_clinic"
        elif t == "2":
            consult_type = "online"
        else:
            reply     = "Please reply 1 for In Clinic or 2 for Online Consultation."
            new_state = "awaiting_consult_type"
            new_temp  = temp_data
            save_conversation_state(from_number, new_state, new_temp)
            return reply
        _, date_opts, date_labels = build_date_options(booking_name)
        await send_date_selection_interactive(from_number, booking_name, date_opts)
        new_state = "awaiting_booking_date"
        new_temp  = {
            "booking_for":   booking_for,
            "booking_name":  booking_name,
            "consult_type":  consult_type,
            "date_options":  date_opts,
            "date_labels":   date_labels,
            **({"selected_doctor_id": temp_data["selected_doctor_id"]} if temp_data.get("selected_doctor_id") else {}),
        }

    # ── DATE PROVIDED ─────────────────────────────────────────
    elif intent == "date_provided":
        booking_name = temp_data.get("booking_name", patient_name)
        booking_for  = temp_data.get("booking_for", patient_id)
        date_options = temp_data.get("date_options", [])
        date_labels  = temp_data.get("date_labels", [])

        # Handle quick numeric options 1/2/3 for today/tomorrow/day-after
        if t in ["1", "2", "3"] and date_options:
            idx = int(t) - 1
            parsed_date  = date_options[idx]
            booking_date = date_labels[idx]
            error        = None
        elif t == "4":
            # Ask for a custom date
            reply     = "Please enter the date (e.g. 15 June 2026):"
            new_state = current_state  # stay in same state
            new_temp  = temp_data
            save_conversation_state(from_number, new_state, new_temp)
            return reply
        else:
            parsed_date, error = parse_date(text)
            booking_date = text

        consult_type = temp_data.get("consult_type", "in_clinic")

        if error:
            reply     = error
            new_state = current_state
            new_temp  = temp_data
        elif consult_type == "online":
            # ── Online consultation slot generation ──────────────────
            import pytz as _pytz
            _now = datetime.now(_pytz.timezone("Asia/Kolkata"))
            cutoff = _now.strftime("%H:%M") if parsed_date == _now.date().isoformat() else ""

            cfg = get_full_clinic_config(doctor_id)
            duration = cfg.get("duration", 15)
            all_online = generate_online_slots(doctor, parsed_date, duration)

            # Filter already booked online slots
            _booked_online_res = _supa.table("appointments").select("appointment_time")\
                .eq("doctor_id", doctor_id)\
                .eq("appointment_date", parsed_date)\
                .eq("consultation_type", "online")\
                .in_("status", ["Confirmed", "In Progress", "Completed"])\
                .execute()
            _booked_online = {(r["appointment_time"] or "")[:5] for r in (_booked_online_res.data or [])}

            available_full = [
                (s, sess) for s, sess in all_online
                if s not in _booked_online and (not cutoff or s > cutoff)
            ]
            available = [s for s, _ in available_full]

            if not all_online:
                reply     = f"Sorry! No online consultation hours configured for {booking_date}. Try another date or choose In Clinic."
                new_state = "idle"
            elif not available:
                reply     = f"Sorry! All online consultation slots for {booking_date} are booked. Please try another date."
                new_state = "idle"
            else:
                morning_online = [s for s, sess in available_full if sess == "morning"]
                evening_online = [s for s, sess in available_full if sess != "morning"]
                base_slot_temp = {
                    "booking_date":    booking_date,
                    "parsed_date":     parsed_date,
                    "available_slots": available,
                    "booking_for":     booking_for,
                    "booking_name":    booking_name,
                    "consult_type":    "online",
                    **({"selected_doctor_id": temp_data["selected_doctor_id"]} if temp_data.get("selected_doctor_id") else {}),
                }
                new_state, new_temp = await send_session_or_slot_ui(
                    from_number, morning_online, evening_online,
                    parsed_date, booking_name, base_slot_temp,
                )
        else:
            # ── In-clinic slot generation ─────────────────────────────
            av = get_availability_for_date(doctor_id, parsed_date)
            if av["is_holiday"]:
                name_part = f" ({av['holiday_name']})" if av.get("holiday_name") else ""
                next_open = get_next_open_date(doctor_id, parsed_date)
                next_part = f"\n\nThe next available date is {next_open}. Would you like to book for {next_open}?" if next_open else ""
                reply     = f"The clinic is closed on {booking_date}{name_part}.{next_part}"
                new_state = "idle"
            elif not av["morning"]["enabled"] and not av["evening"]["enabled"]:
                next_open = get_next_open_date(doctor_id, parsed_date)
                next_part = f"\n\nThe next available date is {next_open}." if next_open else ""
                reply     = f"Sorry! The clinic has no available sessions on {booking_date}.{next_part}\nPlease choose another date."
                new_state = "idle"
            else:
                import pytz as _pytz
                _now = datetime.now(_pytz.timezone("Asia/Kolkata"))
                cutoff = _now.strftime("%H:%M") if parsed_date == _now.date().isoformat() else ""

                booked = get_booked_slots(doctor_id, parsed_date)
                av_slots = generate_slots_for_date(doctor_id, parsed_date)
                available_full = [
                    s for s in av_slots
                    if s["time"] not in booked and (not cutoff or s["time"] > cutoff)
                ]
                available = [s["time"] for s in available_full]

                if not available:
                    reply     = f"Sorry! No slots available on {booking_date}. Please try another date."
                    new_state = "idle"
                else:
                    morning_time = [s["time"] for s in available_full if s["session"] == "morning"]
                    evening_time = [s["time"] for s in available_full if s["session"] == "evening"]
                    base_slot_temp = {
                        "booking_date":    booking_date,
                        "parsed_date":     parsed_date,
                        "available_slots": available,
                        "booking_for":     booking_for,
                        "booking_name":    booking_name,
                        "consult_type":    "in_clinic",
                        **({"selected_doctor_id": temp_data["selected_doctor_id"]} if temp_data.get("selected_doctor_id") else {}),
                    }
                    new_state, new_temp = await send_session_or_slot_ui(
                        from_number, morning_time, evening_time,
                        parsed_date, booking_name, base_slot_temp,
                    )

    # ── SLOT SELECTED ─────────────────────────────────────────
    elif intent == "slot_selected":
        try:
            slot_index   = int(t) - 1
            slots        = temp_data.get("available_slots", [])
            selected_slot = slots[slot_index]
            parsed_date  = temp_data.get("parsed_date", "")
            booking_date = temp_data.get("booking_date", "")
            booking_name = temp_data.get("booking_name", patient_name)
            booking_for  = temp_data.get("booking_for", patient_id)
            consult_type = temp_data.get("consult_type", "in_clinic")

            import pytz as _pytz
            _now = datetime.now(_pytz.timezone("Asia/Kolkata"))
            if parsed_date == _now.date().isoformat() and selected_slot <= _now.strftime("%H:%M"):
                reply = (
                    "That time has already passed. Please choose a later slot.\n"
                    "Reply MENU to start again."
                )
                new_state = "idle"
                save_conversation_state(from_number, new_state, {})
                return reply

            existing = get_active_appointment(booking_for, doctor_id, parsed_date)
            if existing:
                ex_time = format_time(_time_str(existing.get("appointment_time"))[:5])
                ex_tok = get_display_token(
                    existing.get("token_number"), existing.get("appointment_time"),
                    doctor_id=doctor_id, date_str=parsed_date,
                )
                reply = (
                    f"{booking_name} already has an appointment on {booking_date} "
                    f"at {ex_time} (Token {ex_tok}). ⚠️\n\n"
                    f"To re-schedule, please cancel it first and book again.\n"
                    f"Reply 3 to cancel an appointment."
                    + MENU_HINT
                )
                new_state = "idle"
            elif consult_type == "online":
                # Check online slot not already taken
                _ob = _supa.table("appointments").select("id")\
                    .eq("doctor_id", doctor_id)\
                    .eq("appointment_date", parsed_date)\
                    .eq("appointment_time", selected_slot)\
                    .eq("consultation_type", "online")\
                    .in_("status", ["Confirmed", "In Progress", "Completed"])\
                    .execute()
                if _ob.data:
                    reply = (
                        "Sorry, that online slot is already taken. "
                        "Please choose a different time.\n"
                        "Reply MENU to see available slots."
                    )
                    new_state = "idle"
                else:
                    token = assign_online_token(doctor_id, parsed_date)
                    appt_row = create_online_appointment(booking_for, doctor_id, parsed_date, selected_slot)
                    appt_id  = appt_row["id"] if appt_row else None
                    display_tok = f"O{token}"

                    try:
                        _pc = _supa.table("patients").select("patient_code").eq("id", booking_for).single().execute()
                        patient_code = (_pc.data or {}).get("patient_code", "") if _pc.data else ""
                    except Exception:
                        patient_code = ""

                    pat_line = f"{booking_name} ({patient_code})" if patient_code else booking_name
                    _doc_line = ""
                    if temp_data.get("selected_doctor_id"):
                        _specialty = doctor.get("specialty_display") or doctor.get("speciality", "")
                        _doc_line = f"👨‍⚕️ {doctor_name}" + (f" | {_specialty}" if _specialty else "") + "\n"
                    reply = (
                        f"✅ Online Consultation Confirmed! 🎥\n\n"
                        f"🏥 {clinic_name}\n"
                        f"{_doc_line}"
                        f"👤 {pat_line}\n"
                        f"📅 {booking_date}\n"
                        f"⏰ {format_time(selected_slot)} | Token {display_tok}\n\n"
                        f"You will receive a video join link shortly.\n"
                        f"Reply CANCEL to cancel. Reply MENU for help."
                    )
                    new_state = "idle"

                    try:
                        _pat_lang_res = _supa.table("patients").select("language")\
                            .eq("id", booking_for).single().execute()
                        _pat_lang = (_pat_lang_res.data or {}).get("language", "english") or "english"
                        if appt_id:
                            _consult = await create_consultation_for_appointment(
                                supabase=_supa,
                                doctor_id=doctor_id,
                                patient_id=booking_for,
                                appointment_id=appt_id,
                                appointment_date=parsed_date,
                                appointment_time=selected_slot,
                                chief_complaint="",
                            )
                            if _consult:
                                await send_video_link_to_patient(
                                    mobile=from_number,
                                    room_url=_consult["room_url"],
                                    appointment_time=selected_slot,
                                    appointment_date=parsed_date,
                                    language=_pat_lang,
                                )
                    except Exception as _oc_err:
                        import traceback
                        print(f"[ONLINE BOOKING ERROR] {_oc_err}")
                        traceback.print_exc()

            elif not is_slot_available(doctor_id, parsed_date, selected_slot):
                reply = (
                    "Sorry, that slot is already taken. "
                    "Please choose a different time.\n"
                    "Reply MENU to see available slots."
                )
                new_state = "idle"
            else:
                # In-clinic: token is always server-assigned
                token = assign_token_for_slot(doctor_id, parsed_date, selected_slot)
                try:
                    appt_row = create_appointment(booking_for, doctor_id, parsed_date, selected_slot, token)
                except Exception as _ce:
                    print(f"⚠️ create_appointment failed: {_ce}")
                    save_conversation_state(from_number, "awaiting_slot_selection", temp_data)
                    return "Sorry, there was an error booking your appointment. Please try selecting your slot again."
                appt_id  = appt_row["id"] if appt_row else None

                display_tok = get_display_token(token, selected_slot, doctor_id=doctor_id, date_str=parsed_date)

                try:
                    _pc = _supa.table("patients").select("patient_code").eq("id", booking_for).single().execute()
                    patient_code = (_pc.data or {}).get("patient_code", "") if _pc.data else ""
                except Exception:
                    patient_code = ""

                pat_line = f"{booking_name} ({patient_code})" if patient_code else booking_name
                _doc_line = ""
                if temp_data.get("selected_doctor_id"):
                    _specialty = doctor.get("specialty_display") or doctor.get("speciality", "")
                    _doc_line = f"👨‍⚕️ {doctor_name}" + (f" | {_specialty}" if _specialty else "") + "\n"
                reply = (
                    f"✅ Appointment Confirmed!\n\n"
                    f"🏥 {clinic_name}\n"
                    f"{_doc_line}"
                    f"👤 {pat_line}\n"
                    f"📅 {booking_date}\n"
                    f"⏰ {format_time(selected_slot)} | Token {display_tok}\n\n"
                    f"Please mention your token when you arrive.\n"
                    f"Reply CANCEL to cancel. Reply MENU for help."
                )
                new_state = "idle"

                # ── Online auto-create fallback (for in-clinic slots that happen to be in online hours) ──
                print(f"[BOOKING CONFIRMED] date={parsed_date} time={selected_slot} "
                      f"doctor={doctor_id} patient={booking_for}")
                try:
                    _pat_lang_res = _supa.table("patients").select("language") \
                        .eq("id", booking_for).single().execute()
                    _pat_lang = (_pat_lang_res.data or {}).get("language", "english") or "english"

                    _is_online = await is_online_consultation_slot(
                        supabase=_supa,
                        doctor_id=doctor_id,
                        appointment_date=parsed_date,
                        appointment_time=selected_slot,
                    )

                    if _is_online and appt_id:
                        _consult = await create_consultation_for_appointment(
                            supabase=_supa,
                            doctor_id=doctor_id,
                            patient_id=booking_for,
                            appointment_id=appt_id,
                            appointment_date=parsed_date,
                            appointment_time=selected_slot,
                            chief_complaint="",
                        )
                        if _consult:
                            await send_video_link_to_patient(
                                mobile=from_number,
                                room_url=_consult["room_url"],
                                appointment_time=selected_slot,
                                appointment_date=parsed_date,
                                language=_pat_lang,
                            )
                            print(f"[ONLINE CONSULTATION] Video link sent to {from_number}")
                except Exception as _oc_err:
                    import traceback
                    print(f"[ONLINE CHECK ERROR] {_oc_err}")
                    traceback.print_exc()
                # ── End online consultation block ────────────────────────────
        except (IndexError, ValueError):
            reply     = "Invalid choice. Please reply with a number from the list."
            new_state = "awaiting_slot"
            new_temp  = temp_data

    # ── SESSION TEXT INPUT (in awaiting_session_selection) ────────
    elif intent == "session_text_input":
        if t in ["1", "morning"]:
            session = "morning"
            slots   = temp_data.get("morning_slots", [])
        elif t in ["2", "evening"]:
            session = "evening"
            slots   = temp_data.get("evening_slots", [])
        else:
            reply     = "Please tap 🌅 Morning or 🌙 Evening above, or reply 1 for Morning or 2 for Evening."
            new_state = current_state
            new_temp  = temp_data
            save_conversation_state(from_number, new_state, new_temp)
            return reply
        if not slots:
            reply     = f"Sorry, no {session} slots available. Please choose another date or reply MENU."
            new_state = "idle"
        else:
            parsed_date  = temp_data.get("parsed_date", "")
            booking_name = temp_data.get("booking_name", patient_name)
            await send_slot_list(from_number, slots, parsed_date, session, 0, booking_name)
            new_state = "awaiting_slot_selection"
            new_temp  = {**temp_data, "session": session}

    # ── SLOT TEXT INPUT (in awaiting_slot_selection) ──────────
    elif intent == "slot_text_input":
        reply     = "Please tap a slot from the list above, or reply MENU to start over."
        new_state = current_state
        new_temp  = temp_data

    # ── SLOT CONFIRM TEXT INPUT (in awaiting_slot_confirmation) ──
    elif intent == "slot_confirm_text_input":
        pending_slot = temp_data.get("pending_slot", "")
        if t in ["yes", "confirm", "ok", "y"] and pending_slot:
            available = temp_data.get("available_slots", [])
            try:
                idx = available.index(pending_slot)
                save_conversation_state(from_number, "awaiting_slot", temp_data)
                return await handle_message(from_number, str(idx + 1), to_number)
            except ValueError:
                reply     = "Could not find that slot. Reply MENU to start over."
                new_state = "idle"
        elif t in ["no", "cancel", "n"]:
            session      = temp_data.get("session", "morning")
            parsed_date  = temp_data.get("parsed_date", "")
            booking_name = temp_data.get("booking_name", patient_name)
            slots        = temp_data.get(f"{session}_slots", [])
            await send_slot_list(from_number, slots, parsed_date, session, 0, booking_name)
            new_state = "awaiting_slot_selection"
            new_temp  = temp_data
        else:
            reply     = "Please tap ✅ Confirm or ❌ Cancel above."
            new_state = current_state
            new_temp  = temp_data

    # ── QUEUE STATUS ──────────────────────────────────────────
    elif intent == "queue":
        import pytz
        now_ist = datetime.now(pytz.timezone("Asia/Kolkata"))
        today_ist = now_ist.date().isoformat()

        # 1. Current token from tokens session row (0 = not started)
        tok_res = _supa.table("tokens").select("current_token").eq(
            "doctor_id", doctor_id
        ).eq("queue_date", today_ist).execute()
        current_token = tok_res.data[0]["current_token"] if tok_res.data else 0

        # Self-heal stale session: served token must exist in today's appointments
        if current_token:
            chk = _supa.table("appointments").select("id").eq(
                "doctor_id", doctor_id
            ).eq("appointment_date", today_ist).eq("token_number", current_token).execute()
            if not chk.data:
                current_token = 0
                _supa.table("tokens").update({"current_token": 0}).eq(
                    "doctor_id", doctor_id).eq("queue_date", today_ist).execute()

        # 2. ALL of this mobile's appointments today (self + family members)
        own = _supa.table("patients").select("id").eq("mobile", from_number).execute()
        fam = _supa.table("patients").select("id").eq("family_head_mobile", from_number).execute()
        my_ids = list({p["id"] for p in (own.data or []) + (fam.data or [])})

        clinic_appts = []
        online_appts = []
        if my_ids:
            # In-clinic appointments only
            clinic_res = _supa.table("appointments").select(
                "token_number, status, appointment_time, patient_id, patients(name)"
            ).eq("doctor_id", doctor_id).eq("appointment_date", today_ist).neq(
                "status", "Cancelled"
            ).neq("consultation_type", "online").in_(
                "patient_id", my_ids
            ).order("token_number").execute()
            clinic_appts = clinic_res.data or []

            # Online appointments only
            online_res = _supa.table("appointments").select(
                "id, appointment_time, patient_id, patients(name)"
            ).eq("doctor_id", doctor_id).eq("appointment_date", today_ist).eq(
                "consultation_type", "online"
            ).neq("status", "Cancelled").in_("patient_id", my_ids).execute()
            online_appts = online_res.data or []

        if not clinic_appts and not online_appts:
            reply = (
                "You have no appointment today.\n"
                "Reply 1 to book an appointment."
            )
        else:
            lines = []

            # ── In-clinic section ────────────────────────────────
            if clinic_appts:
                # All of today's in-clinic appointments for wait math
                all_today = _supa.table("appointments").select(
                    "token_number, appointment_time, status"
                ).eq("doctor_id", doctor_id).eq("appointment_date", today_ist).neq(
                    "consultation_type", "online"
                ).execute().data or []

                cfg = get_slot_config()
                eve_start = cfg["evening_start"]
                eve_start_display = format_time(eve_start[:5])
                slot_min = cfg["duration"]

                def appt_name(a):
                    return (a.get("patients") or {}).get("name", "Patient")

                def disp(a):
                    return get_display_token(a.get("token_number"), a.get("appointment_time"),
                                             doctor_id=a.get("doctor_id"),
                                             date_str=str(a.get("appointment_date", ""))[:10])

                def t_of(a):
                    return _time_str(a.get("appointment_time"))

                def slot_display(a):
                    t = t_of(a)[:5]
                    try:
                        return format_time(t)
                    except Exception:
                        return t

                serving = next((a for a in all_today if a.get("status") == "In Progress"), None)
                if not serving and current_token > 0:
                    serving = next(
                        (a for a in all_today if a.get("token_number") == current_token), None
                    )
                serving_time = t_of(serving) if serving else ""
                current_display = disp(serving) if serving else "Not started"

                def session_status(a):
                    if a.get("status") == "In Progress" or (
                        current_token and a.get("token_number") == current_token
                    ):
                        return "🟢 Now being seen"
                    if a.get("status") == "Completed" or (serving_time and t_of(a) < serving_time):
                        return "✅ Done"
                    is_evening = t_of(a) >= "13:00:00"
                    ahead = len([
                        x for x in all_today
                        if x.get("status") == "Confirmed"
                        and (t_of(x) >= "13:00:00") == is_evening
                        and t_of(x) < t_of(a)
                        and (not serving_time or t_of(x) > serving_time)
                    ])
                    wait = ahead * slot_min
                    return f"⏳ ~{wait} mins wait" if wait > 0 else "⏳ Next in line"

                morning = sorted([a for a in clinic_appts if t_of(a) < "13:00:00"], key=t_of)
                evening = sorted([a for a in clinic_appts if t_of(a) >= "13:00:00"], key=t_of)

                lines.append(f"🏥 {clinic_name} - Live Queue\n")
                lines.append(f"Current Token: {current_display}\n")

                for a in morning:
                    lines.append(f"{disp(a)} {appt_name(a)} ({slot_display(a)}) → {session_status(a)}")

                if evening:
                    evening_open = now_ist.strftime("%H:%M") >= eve_start[:5]
                    for a in evening:
                        if evening_open:
                            lines.append(f"{disp(a)} {appt_name(a)} ({slot_display(a)}) → {session_status(a)}")
                        else:
                            lines.append(
                                f"{disp(a)} {appt_name(a)} ({slot_display(a)}) → 🌙 Evening session. "
                                f"Check back after {eve_start_display}"
                            )

            # ── Online consultation section ───────────────────────
            if online_appts:
                if lines:
                    lines.append("")  # blank separator

                for oa in online_appts:
                    pat_name = (oa.get("patients") or {}).get("name", "Patient")
                    appt_time_disp = format_time(_time_str(oa.get("appointment_time"))[:5])

                    # Get the O-token counter and room_url from consultations table
                    consult_res = _supa.table("consultations").select(
                        "room_url"
                    ).eq("appointment_id", oa["id"]).limit(1).execute()

                    room_url = (consult_res.data[0]["room_url"] if consult_res.data else None)

                    # Compute O-token display
                    o_count_res = _supa.table("appointments").select("id").eq(
                        "doctor_id", doctor_id
                    ).eq("appointment_date", today_ist).eq(
                        "consultation_type", "online"
                    ).neq("status", "Cancelled").execute()
                    all_online = o_count_res.data or []
                    appt_ids = [a["id"] for a in all_online]
                    o_token = (appt_ids.index(oa["id"]) + 1) if oa["id"] in appt_ids else 1
                    o_display = f"O{o_token}"

                    lines.append(f"💻 Online Consultation — {pat_name}")
                    lines.append(f"Token: {o_display} | Time: {appt_time_disp}")
                    if room_url:
                        lines.append(f"Join on time using your link:\n{room_url}")
                    else:
                        lines.append("Join link was sent when you booked.")

            lines.append("\nReply MENU for main menu")
            reply = "\n".join(lines)

    # ── CANCEL APPOINTMENT ────────────────────────────────────
    elif intent == "cancel":
        appointments = get_family_upcoming_appointments(from_number, doctor_id, clinic_whatsapp=to_number)
        if not appointments:
            reply     = "You have no upcoming appointments to cancel.\n\nReply 1 to book an appointment."
            new_state = "idle"
        else:
            await send_cancel_appointment_list(from_number, appointments)
            new_state = "awaiting_cancel_selection"
            new_temp  = {"appointments": appointments}

    elif intent == "cancel_choice":
        if t == "0":
            await send_main_menu(from_number, clinic_name)
            new_state = "idle"
        else:
            try:
                choice       = int(t) - 1
                appointments = temp_data.get("appointments", [])
                apt          = appointments[choice]

                token_str = ""
                if apt.get("token_number"):
                    d_tok = get_display_token(apt["token_number"], apt["appointment_time"],
                                              doctor_id=apt.get("doctor_id"),
                                              date_str=str(apt.get("appointment_date", ""))[:10])
                    token_str = f" (Token {d_tok})"

                cancel_appointment(apt["id"])
                apt_date = datetime.strptime(
                    apt["appointment_date"], "%Y-%m-%d"
                ).strftime("%d %B %Y")
                apt_time = format_time(apt["appointment_time"][:5])
                reply = (
                    f"Your appointment{token_str} on {apt_date} at {apt_time} "
                    f"has been cancelled. ✅\n\n"
                    f"Reply 1 to book a new appointment."
                    + MENU_HINT
                )
                new_state = "idle"
            except (IndexError, ValueError):
                reply     = "Invalid choice. Please tap a button or reply with a number from the list."
                new_state = "awaiting_cancel_selection"
                new_temp  = temp_data

    # ── CLINIC TIMINGS ────────────────────────────────────────
    elif intent == "timing":
        address_line = f"\nAddress: {clinic_address}" if clinic_address else ""
        reply = (
            f"{clinic_name} Timings 🕐\n\n"
            f"{clinic_timings}"
            f"{address_line}\n\n"
            f"Reply 1 for appointment."
            + MENU_HINT
        )
        new_state = "idle"

    # ── SPEAK TO RECEPTIONIST ─────────────────────────────────
    elif intent == "speak":
        reply = (
            f"Our team will contact you shortly. 📞\n\n"
            f"Clinic hours: {clinic_timings}\n\n"
            f"Alternatively reply 1 to book online."
            + MENU_HINT
        )
        new_state = "idle"

    # ── ASK DOCTOR A QUESTION ─────────────────────────────────
    elif intent == "ask_question":
        all_patients = get_all_linked_patients(from_number)
        active_map = get_active_prescription_doctors(all_patients)

        if not active_map:
            reply = (
                "None of the patients registered under your number have an "
                "active prescription right now.\n\n"
                "Questions to the doctor can be asked during an ongoing treatment.\n"
                "Reply 1 to book an appointment."
                + MENU_HINT
            )
            new_state = "idle"
        else:
            active_patients = [p for p in all_patients if p["id"] in active_map]
            if len(active_patients) == 1:
                # One patient — go straight to doctor check
                p = active_patients[0]
                doctors = active_map[p["id"]]
                if len(doctors) == 1:
                    d = doctors[0]
                    reply = (
                        f"Please type your question for {d['name']} about {p['name']}.\n\n"
                        "Our doctor will reply within a few hours. 💬"
                    )
                    new_state = "awaiting_query"
                    new_temp  = {"query_patient_id": p["id"], "query_doctor_id": d["id"], "query_doctor_name": d["name"]}
                else:
                    # 1 patient, 2+ doctors — show doctor selection list
                    rows = [{"id": f"qdr_{d['id']}", "title": d["name"], "description": d.get("specialty", "")} for d in doctors]
                    await send_meta_list(
                        to_number=from_number,
                        header_text="Ask a Doctor",
                        body_text=f"Which doctor would you like to ask about {p['name']}?",
                        button_label="Select Doctor",
                        sections=[{"title": "Doctors with active prescription", "rows": rows}],
                        footer_text=clinic_name,
                    )
                    new_state = "awaiting_query_doctor_select"
                    new_temp  = {"query_patient_id": p["id"], "query_doctor_candidates": doctors}
                    save_conversation_state(from_number, new_state, new_temp)
                    return None
            else:
                # Multiple patients — show patient selection
                lines = "Your question is for which patient?\n\n"
                for i, p in enumerate(active_patients, 1):
                    code = f" ({p['patient_code']})" if p.get("patient_code") else ""
                    lines += f"{i}. {p['name']}{code}\n"
                lines += "\nReply with a number."
                reply     = lines
                new_state = "awaiting_query_patient_select"
                new_temp  = {"query_patients": active_patients, "query_active_map": {k: v for k, v in active_map.items()}}

    elif intent == "query_patient_selected":
        _patients = temp_data.get("query_patients", [])
        _active_map = temp_data.get("query_active_map", {})
        try:
            choice = int(t) - 1
            if 0 <= choice < len(_patients):
                selected = _patients[choice]
                doctors = _active_map.get(selected["id"], [])
                if len(doctors) == 1:
                    d = doctors[0]
                    reply = (
                        f"Please type your question for {d['name']}.\n\n"
                        "Our doctor will reply within a few hours. 💬"
                    )
                    new_state = "awaiting_query"
                    new_temp  = {"query_patient_id": selected["id"], "query_doctor_id": d["id"], "query_doctor_name": d["name"]}
                elif len(doctors) > 1:
                    rows = [{"id": f"qdr_{d['id']}", "title": d["name"], "description": d.get("specialty", "")} for d in doctors]
                    await send_meta_list(
                        to_number=from_number,
                        header_text="Ask a Doctor",
                        body_text=f"Which doctor would you like to ask about {selected['name']}?",
                        button_label="Select Doctor",
                        sections=[{"title": "Doctors with active prescription", "rows": rows}],
                        footer_text=clinic_name,
                    )
                    new_state = "awaiting_query_doctor_select"
                    new_temp  = {"query_patient_id": selected["id"], "query_doctor_candidates": doctors}
                    save_conversation_state(from_number, new_state, new_temp)
                    return None
                else:
                    reply = "No active prescription found for this patient." + MENU_HINT
                    new_state = "idle"
            else:
                lines = "Invalid choice. Which patient?\n\n"
                for i, p in enumerate(_patients, 1):
                    code = f" ({p['patient_code']})" if p.get("patient_code") else ""
                    lines += f"{i}. {p['name']}{code}\n"
                reply     = lines
                new_state = "awaiting_query_patient_select"
                new_temp  = temp_data
        except (ValueError, IndexError):
            reply     = "Please reply with a number."
            new_state = "awaiting_query_patient_select"
            new_temp  = temp_data

    elif intent == "query_doctor_selected":
        _candidates = temp_data.get("query_doctor_candidates", [])
        _qpid = temp_data.get("query_patient_id")
        # list_reply comes as "qdr_<uuid>", text reply as a number
        selected_doc = None
        if text.startswith("qdr_"):
            did = text[4:]
            selected_doc = next((d for d in _candidates if d["id"] == did), None)
        else:
            try:
                idx = int(t) - 1
                if 0 <= idx < len(_candidates):
                    selected_doc = _candidates[idx]
            except (ValueError, IndexError):
                pass
        if selected_doc:
            reply = (
                f"Please type your question for {selected_doc['name']}.\n\n"
                "Our doctor will reply within a few hours. 💬"
            )
            new_state = "awaiting_query"
            new_temp  = {"query_patient_id": _qpid, "query_doctor_id": selected_doc["id"], "query_doctor_name": selected_doc["name"]}
        else:
            rows = [{"id": f"qdr_{d['id']}", "title": d["name"], "description": d.get("specialty", "")} for d in _candidates]
            await send_meta_list(
                to_number=from_number,
                header_text="Ask a Doctor",
                body_text="Please select a doctor from the list.",
                button_label="Select Doctor",
                sections=[{"title": "Doctors", "rows": rows}],
                footer_text=clinic_name,
            )
            new_state = "awaiting_query_doctor_select"
            new_temp  = temp_data
            save_conversation_state(from_number, new_state, new_temp)
            return None

    elif intent == "query_text_provided":
        query_patient_id  = temp_data.get("query_patient_id", patient_id)
        query_doctor_id   = temp_data.get("query_doctor_id", doctor_id)
        query_doctor_name = temp_data.get("query_doctor_name", doctor_name)
        try:
            import datetime as _dt
            _supa.table("queries").insert({
                "patient_id": query_patient_id,
                "doctor_id":  query_doctor_id,
                "question":   text,
                "status":     "Pending",
                "created_at": _dt.datetime.utcnow().isoformat(),
            }).execute()
            print(f"✅ Query saved for patient {query_patient_id} → doctor {query_doctor_id}")
        except Exception as _e:
            print(f"❌ Failed to save query: {_e}")
        reply = (
            f"✅ Your question has been sent to {query_doctor_name}!\n\n"
            "You will receive a reply on WhatsApp within a few hours.\n\n"
            "Reply MENU for main menu."
        )
        new_state = "idle"

    # ── MEDIA / LAB REPORT ────────────────────────────────────
    elif intent == "media":
        reply = (
            f"Thank you for sharing the report. 📋 "
            f"{doctor_name} will review it and get back to you shortly."
            + MENU_HINT
        )
        new_state = "idle"

    # ── DEFAULT ───────────────────────────────────────────────
    else:
        await send_main_menu(from_number, clinic_name)
        new_state = "idle"

    # Save conversation state
    save_conversation_state(from_number, new_state, new_temp)

    return reply

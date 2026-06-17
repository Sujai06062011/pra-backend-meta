from datetime import datetime, date, timedelta
import re
from consultation_helpers import (
    is_online_consultation_slot,
    create_consultation_for_appointment,
    send_video_link_to_patient,
)
from database import (
    get_doctor_by_whatsapp, get_patient_by_mobile, get_conversation_state,
    save_conversation_state, get_queue_status, get_patient_token_today, get_family_tokens_today,
    check_holiday, get_booked_slots, get_next_token, create_appointment,
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
    """Generate online consultation time slots for a date from the doctor's online hours."""
    from datetime import datetime as _dt, timedelta as _td
    hours = doctor.get("online_consultation_hours") or []
    day_name = date.fromisoformat(date_str).strftime("%A").lower()
    day_entry = next((h for h in hours if h.get("day", "").lower() == day_name), None)
    if not day_entry:
        return []
    slots = []
    start_h, start_m = map(int, day_entry["start"].split(":"))
    end_h, end_m = map(int, day_entry["end"].split(":"))
    cur = _dt(2000, 1, 1, start_h, start_m)
    end_t = _dt(2000, 1, 1, end_h, end_m)
    while cur < end_t:
        slots.append(cur.strftime("%H:%M"))
        cur += _td(minutes=duration)
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


def build_main_menu(patient_name: str, clinic_name: str) -> str:
    return (
        f"👋 Welcome back to\n🏥 *{clinic_name}*\n\n"
        f"1️⃣ Book Appointment\n"
        f"2️⃣ Queue Status\n"
        f"3️⃣ Cancel Appointment\n"
        f"4️⃣ Clinic Timings\n"
        f"5️⃣ Speak to Receptionist\n"
        f"6️⃣ Ask Doctor a Question\n\n"
        f"Reply with a number.\n"
        f"Reply MENU for main menu or BYE to end."
    )


def get_all_linked_patients(mobile: str) -> list:
    """Return all patients whose mobile = {mobile} OR family_head_mobile = {mobile}, ordered by created_at ASC."""
    result = _supa.table("patients").select(
        "id, name, age, gender, patient_code, date_of_birth"
    ).or_(
        f"mobile.eq.{mobile},family_head_mobile.eq.{mobile}"
    ).order("created_at", desc=False).execute()
    return result.data or []


def filter_patients_with_active_prescriptions(patients: list) -> list:
    """Keep only patients who have a prescription whose medicine course is still running."""
    if not patients:
        return []
    import pytz
    today = datetime.now(pytz.timezone("Asia/Kolkata")).date()
    ids = [p["id"] for p in patients]
    res = _supa.table("prescriptions").select(
        "patient_id, prescription_date, prescription_medicines(duration_days)"
    ).in_("patient_id", ids).execute()

    active_ids = set()
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
                active_ids.add(pres["patient_id"])
                break
    return [p for p in patients if p["id"] in active_ids]


def build_patient_select_msg(patients: list) -> str:
    lines = "📅 Book Appointment\n\nWho is this appointment for?\n\n"
    for i, p in enumerate(patients, 1):
        code = f" ({p['patient_code']})" if p.get("patient_code") else ""
        age  = f" · {p['age']} yrs" if p.get("age") else ""
        lines += f"{i}. {p['name']}{code}{age}\n"
    lines += f"{len(patients) + 1}. ➕ Add new family member\n"
    lines += "\nReply with a number.\nReply MENU for main menu."
    return lines


def build_date_options() -> str:
    today     = date.today()
    tomorrow  = date(today.year, today.month, today.day + 1) if today.day < 28 else date.fromordinal(today.toordinal() + 1)
    day_after = date.fromordinal(today.toordinal() + 2)
    fmt = "%d %B %Y"
    return (
        f"Which date?\n\n"
        f"1. Today ({today.strftime(fmt)})\n"
        f"2. Tomorrow ({tomorrow.strftime(fmt)})\n"
        f"3. Day after ({day_after.strftime(fmt)})\n"
        f"4. Other date (reply with date e.g. 15 June 2026)\n"
        + MENU_HINT
    )


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

    # ── GLOBAL COMMANDS (always reset state) ─────────────────
    if t in ["menu", "main menu", "back", "home",
             "hi", "hello", "hey", "start", "help"]:
        save_conversation_state(from_number, "idle", {})
        return build_main_menu(patient_name, clinic_name)

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
                return (
                    f"No problem! Let us book an appointment for you. 🏥\n\n"
                    + build_main_menu(patient_name, clinic_name)
                )
            save_followup_reply(from_number, t)
            responses = {
                "1": f"Wonderful! We are glad you are feeling better. 😊\n\nStay healthy!\n- {clinic_name}",
                "2": f"We hope you feel better soon. 🙏\n\nPlease rest well and follow the diet instructions.\n- {clinic_name}",
            }
            save_conversation_state(from_number, "idle", {})
            return responses.get(t, "Thank you for your response!")

    # ── INTENT DETECTION ──────────────────────────────────────
    intent = "menu"

    if current_state == "awaiting_name":
        intent = "name_provided"
    elif current_state == "awaiting_dob":
        intent = "dob_provided"
    elif current_state == "awaiting_gender":
        intent = "gender_provided"
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
    elif current_state == "awaiting_booking_date":
        intent = "date_provided"
    elif current_state == "awaiting_date":
        intent = "date_provided"
    elif current_state == "awaiting_consult_type":
        intent = "consult_type_selected"
    elif current_state == "awaiting_slot":
        intent = "slot_selected"
    elif current_state == "awaiting_cancel_choice":
        intent = "cancel_choice"
    elif current_state == "awaiting_query_patient_select":
        intent = "query_patient_selected"
    elif current_state == "awaiting_query":
        intent = "query_text_provided"
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
        reply = f"Thank you {text}! 😊\n\nPlease share your Date of Birth.\n\nExample: 14 May 1982"
        new_state = "awaiting_dob"
        new_temp  = {"name": text}

    elif intent == "dob_provided":
        reply = "Got it! Please share your Gender.\n\nReply M for Male or F for Female."
        new_state = "awaiting_gender"
        new_temp  = {**temp_data, "dob": text}

    elif intent == "gender_provided":
        gender = "Male" if t.startswith("m") else "Female"
        name   = temp_data.get("name", "")
        dob    = temp_data.get("dob", "")
        new_patient = create_patient(from_number, name, dob, gender,
                                     family_head_mobile=from_number)
        patient_id = new_patient["id"] if new_patient else ""
        reply = (
            f"You are now registered at {clinic_name}! Welcome {name}! 🎉\n\n"
            + build_main_menu(name, clinic_name)
        )
        new_state = "idle"

    # ── MAIN MENU ─────────────────────────────────────────────
    elif intent == "menu":
        reply     = build_main_menu(patient_name, clinic_name)
        new_state = "idle"

    # ── BOOK APPOINTMENT — unified flow ───────────────────────
    elif intent == "book":
        all_patients = get_all_linked_patients(from_number)
        if len(all_patients) == 1:
            # Only one patient — skip selection, go straight to date
            p = all_patients[0]
            today     = date.today()
            tomorrow  = date.fromordinal(today.toordinal() + 1)
            day_after = date.fromordinal(today.toordinal() + 2)
            fmt = "%d %B %Y"
            reply = (
                f"📅 Booking appointment for *{p['name']}*\n\n"
                f"Which date?\n\n"
                f"1. Today ({today.strftime(fmt)})\n"
                f"2. Tomorrow ({tomorrow.strftime(fmt)})\n"
                f"3. Day after ({day_after.strftime(fmt)})\n"
                f"4. Other date (reply with date e.g. 15 June 2026)"
                + MENU_HINT
            )
            new_state = "awaiting_booking_date"
            new_temp  = {
                "booking_for": p["id"],
                "booking_name": p["name"],
                "date_options": [
                    today.strftime("%Y-%m-%d"),
                    tomorrow.strftime("%Y-%m-%d"),
                    day_after.strftime("%Y-%m-%d"),
                ],
                "date_labels": [
                    today.strftime(fmt),
                    tomorrow.strftime(fmt),
                    day_after.strftime(fmt),
                ],
            }
        else:
            # Multiple patients — show selection list
            reply     = build_patient_select_msg(all_patients)
            new_state = "awaiting_booking_patient_select"
            new_temp  = {"booking_patients": all_patients}

    # ── PATIENT SELECTED for booking ──────────────────────────
    elif intent == "booking_patient_selected":
        all_patients = temp_data.get("booking_patients", [])
        try:
            choice = int(t) - 1
            if choice == len(all_patients):
                # Add new family member
                reply     = "Please enter the new family member's full name:"
                new_state = "awaiting_new_member_name"
                new_temp  = {}
            elif 0 <= choice < len(all_patients):
                p = all_patients[choice]
                base_temp = {"booking_for": p["id"], "booking_name": p["name"]}
                if doctor.get("online_consultation_enabled"):
                    reply = (
                        f"Would you like to book:\n\n"
                        f"1️⃣ In Clinic\n"
                        f"2️⃣ Online Consultation 💻"
                        + MENU_HINT
                    )
                    new_state = "awaiting_consult_type"
                    new_temp  = base_temp
                else:
                    date_reply, date_opts, date_labels = build_date_options(p["name"])
                    reply     = date_reply
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
        reply     = f"Please enter their date of birth (DD/MM/YYYY):"
        new_state = "awaiting_new_member_dob"
        new_temp  = {"new_name": text}

    elif intent == "new_member_dob_provided":
        reply     = "Please enter their gender (Male/Female/Other):"
        new_state = "awaiting_new_member_gender"
        new_temp  = {**temp_data, "new_dob": text}

    elif intent == "new_member_gender_provided":
        reply = (
            "What language do they prefer?\n\n"
            "1. Tamil\n"
            "2. English\n"
            "3. Hindi"
        )
        new_state = "awaiting_new_member_language"
        new_temp  = {**temp_data, "new_gender": text}

    elif intent == "new_member_language_provided":
        lang_map = {"1": "tamil", "2": "english", "3": "hindi",
                    "tamil": "tamil", "english": "english", "hindi": "hindi"}
        language = lang_map.get(t, "english")

        raw_name   = temp_data.get("new_name", "")
        raw_dob    = temp_data.get("new_dob", "")   # DD/MM/YYYY
        raw_gender = temp_data.get("new_gender", "Male")
        gender_clean = "Male" if raw_gender.lower().startswith("m") else (
                        "Female" if raw_gender.lower().startswith("f") else "Other")

        # Parse DOB: support DD/MM/YYYY or DD-MM-YYYY
        dob_iso  = None
        age      = None
        birth_year = "0000"
        try:
            sep = "/" if "/" in raw_dob else "-"
            parts = raw_dob.split(sep)
            if len(parts) == 3:
                day, mon, yr = parts
                dob_date  = date(int(yr), int(mon), int(day))
                today_d   = date.today()
                age       = today_d.year - dob_date.year - (
                    (today_d.month, today_d.day) < (dob_date.month, dob_date.day)
                )
                dob_iso   = dob_date.isoformat()
                birth_year = str(dob_date.year)
        except Exception as _e:
            print(f"⚠️ DOB parse error: {_e}")

        # Generate patient_code
        name_part  = raw_name[:3].upper().replace(" ", "")
        mobile_sfx = from_number[-4:]
        patient_code = f"{name_part}-{mobile_sfx}-{birth_year}"

        # Insert patient
        try:
            ins = _supa.table("patients").insert({
                "mobile": from_number,
                "whatsapp_number": from_number,
                "name": raw_name,
                "date_of_birth": dob_iso,
                "age": age,
                "gender": gender_clean,
                "language": language,
                "patient_code": patient_code,
                "family_head_mobile": from_number,
                "registration_source": "whatsapp",
            }).execute()
            new_pid = ins.data[0]["id"] if ins.data else ""
        except Exception as _e:
            print(f"❌ Family member insert error: {_e}")
            new_pid = ""

        today     = date.today()
        tomorrow  = date.fromordinal(today.toordinal() + 1)
        day_after = date.fromordinal(today.toordinal() + 2)
        fmt = "%d %B %Y"
        age_str = f"{age} yrs" if age is not None else "unknown"

        reg_msg = (
            f"✅ Family member registered!\n\n"
            f"👤 {raw_name}\n"
            f"🪪 {patient_code}\n"
            f"🎂 {age_str} · {gender_clean}\n\n"
            f"Now let's book their appointment.\n\n"
        )
        base_temp = {"booking_for": new_pid, "booking_name": raw_name}
        if doctor.get("online_consultation_enabled"):
            reply = reg_msg + (
                "Would you like to book:\n\n"
                "1️⃣ In Clinic\n"
                "2️⃣ Online Consultation 💻"
                + MENU_HINT
            )
            new_state = "awaiting_consult_type"
            new_temp  = base_temp
        else:
            date_reply, date_opts, date_labels = build_date_options(raw_name)
            reply     = reg_msg + date_reply
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
        date_reply, date_opts, date_labels = build_date_options(booking_name)
        reply     = date_reply
        new_state = "awaiting_booking_date"
        new_temp  = {
            "booking_for":   booking_for,
            "booking_name":  booking_name,
            "consult_type":  consult_type,
            "date_options":  date_opts,
            "date_labels":   date_labels,
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

            available = [
                s for s in all_online
                if s not in _booked_online and (not cutoff or s > cutoff)
            ]

            if not all_online:
                reply     = f"Sorry! No online consultation hours configured for {booking_date}. Try another date or choose In Clinic."
                new_state = "idle"
            elif not available:
                reply     = f"Sorry! All online consultation slots for {booking_date} are booked. Please try another date."
                new_state = "idle"
            else:
                slot_list = f"💻 Online Consultation slots on {booking_date}:\n"
                for idx, s in enumerate(available, 1):
                    slot_list += f"{idx}. {format_time(s)}\n"
                slot_list += "\nReply with slot number to confirm." + MENU_HINT
                reply     = slot_list
                new_state = "awaiting_slot"
                new_temp  = {
                    "booking_date":    booking_date,
                    "parsed_date":     parsed_date,
                    "available_slots": available,
                    "booking_for":     booking_for,
                    "booking_name":    booking_name,
                    "consult_type":    "online",
                }
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
                    morning_slots = [s for s in available_full if s["session"] == "morning"]
                    evening_slots = [s for s in available_full if s["session"] == "evening"]

                    slot_list = f"🏥 Available slots on {booking_date}:\n"
                    idx = 1
                    if morning_slots:
                        slot_list += "\n🌅 Morning\n"
                        for s in morning_slots:
                            slot_list += f"{idx}. {format_time(s['time'])}\n"
                            idx += 1
                    if evening_slots:
                        slot_list += "\n🌆 Evening\n"
                        for s in evening_slots:
                            slot_list += f"{idx}. {format_time(s['time'])}\n"
                            idx += 1
                    slot_list += "\nReply with slot number to confirm." + MENU_HINT
                    reply     = slot_list
                    new_state = "awaiting_slot"
                    new_temp  = {
                        "booking_date":    booking_date,
                        "parsed_date":     parsed_date,
                        "available_slots": available,
                        "booking_for":     booking_for,
                        "booking_name":    booking_name,
                        "consult_type":    "in_clinic",
                    }

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
                    existing.get("token_number"), existing.get("appointment_time")
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
                    appt_row = create_appointment(booking_for, doctor_id, parsed_date, selected_slot, token)
                    appt_id  = appt_row["id"] if appt_row else None
                    if appt_id:
                        _supa.table("appointments").update({"consultation_type": "online"})\
                            .eq("id", appt_id).execute()
                    display_tok = f"O{token}"

                    try:
                        _pc = _supa.table("patients").select("patient_code").eq("id", booking_for).single().execute()
                        patient_code_line = f"\nPatient Code: {_pc.data['patient_code']}" if _pc.data and _pc.data.get("patient_code") else ""
                    except Exception:
                        patient_code_line = ""

                    reply = (
                        f"Online Consultation Confirmed! 🎥✅\n\n"
                        f"Patient: {booking_name}"
                        f"{patient_code_line}\n"
                        f"Date: {booking_date}\n"
                        f"Time: {format_time(selected_slot)}\n"
                        f"Token: {display_tok}\n\n"
                        f"You will receive a video join link shortly.\n"
                        f"Reply CANCEL to cancel."
                        + MENU_HINT
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
                appt_row = create_appointment(booking_for, doctor_id, parsed_date, selected_slot, token)
                appt_id  = appt_row["id"] if appt_row else None

                display_tok = get_display_token(token, selected_slot)

                try:
                    _pc = _supa.table("patients").select("patient_code").eq("id", booking_for).single().execute()
                    patient_code_line = f"\nPatient Code: {_pc.data['patient_code']}" if _pc.data and _pc.data.get("patient_code") else ""
                except Exception:
                    patient_code_line = ""

                reply = (
                    f"Appointment Confirmed! ✅\n\n"
                    f"Patient: {booking_name}"
                    f"{patient_code_line}\n"
                    f"Date: {booking_date}\n"
                    f"Time: {format_time(selected_slot)}\n"
                    f"Token: {display_tok}\n"
                    f"Clinic: {clinic_name}\n\n"
                    f"Please mention your token when you arrive.\n"
                    f"Reply CANCEL to cancel. See you soon!"
                    + MENU_HINT
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

        my_appts = []
        if my_ids:
            appts_res = _supa.table("appointments").select(
                "token_number, status, appointment_time, patient_id, patients(name)"
            ).eq("doctor_id", doctor_id).eq("appointment_date", today_ist).neq(
                "status", "Cancelled"
            ).in_("patient_id", my_ids).order("token_number").execute()
            my_appts = appts_res.data or []

        if not my_appts:
            reply = (
                "You have no appointment today.\n"
                "Reply 1 to book an appointment."
            )
        else:
            # All of today's appointments — wait math runs in slot-time order
            all_today = _supa.table("appointments").select(
                "token_number, appointment_time, status"
            ).eq("doctor_id", doctor_id).eq("appointment_date", today_ist).execute().data or []

            cfg = get_slot_config()
            eve_start = cfg["evening_start"]            # e.g. "17:00"
            eve_start_display = format_time(eve_start[:5])
            slot_min = cfg["duration"]

            def appt_name(a):
                return (a.get("patients") or {}).get("name", "Patient")

            def disp(a):
                return get_display_token(a.get("token_number"), a.get("appointment_time"))

            def t_of(a):
                return _time_str(a.get("appointment_time"))

            def slot_display(a):
                t = t_of(a)[:5]
                try:
                    return format_time(t)
                except Exception:
                    return t

            # The appointment being served (by status, else by tokens.current_token)
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

            morning = sorted(
                [a for a in my_appts if t_of(a) < "13:00:00"], key=t_of
            )
            evening = sorted(
                [a for a in my_appts if t_of(a) >= "13:00:00"], key=t_of
            )

            lines = [f"🏥 {clinic_name} - Live Queue\n",
                     f"Current Token: {current_display}\n"]

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

            lines.append("\nReply MENU for main menu")
            reply = "\n".join(lines)

    # ── CANCEL APPOINTMENT ────────────────────────────────────
    elif intent == "cancel":
        appointments = get_family_upcoming_appointments(from_number, doctor_id)
        if not appointments:
            reply     = "You have no upcoming appointments to cancel.\n\nReply 1 to book an appointment."
            new_state = "idle"
        else:
            apt_list = "Your upcoming appointments:\n\n"
            for i, apt in enumerate(appointments, 1):
                apt_date = datetime.strptime(
                    apt["appointment_date"], "%Y-%m-%d"
                ).strftime("%d %B %Y")
                apt_time = format_time(apt["appointment_time"][:5])
                token    = apt.get("token_number")
                name     = apt.get("patient_name", "")
                token_str = ""
                if token:
                    d_tok = get_display_token(token, apt["appointment_time"])
                    token_str = f" (Token {d_tok})"
                apt_list += f"{i}. {name} — {apt_date} at {apt_time}{token_str}\n"
            apt_list += "\nReply with number to cancel. Reply 0 to go back."
            reply     = apt_list
            new_state = "awaiting_cancel_choice"
            new_temp  = {"appointments": appointments}

    elif intent == "cancel_choice":
        if t == "0":
            reply     = build_main_menu(patient_name, clinic_name)
            new_state = "idle"
        else:
            try:
                choice       = int(t) - 1
                appointments = temp_data.get("appointments", [])
                apt          = appointments[choice]

                token_str = ""
                if apt.get("token_number"):
                    d_tok = get_display_token(apt["token_number"], apt["appointment_time"])
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
                reply     = "Invalid choice. Please reply with a number from the list."
                new_state = "awaiting_cancel_choice"
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
        active_patients = filter_patients_with_active_prescriptions(all_patients)

        if not active_patients:
            reply = (
                "None of the patients registered under your number have an "
                "active prescription right now.\n\n"
                "Questions to the doctor can be asked during an ongoing treatment.\n"
                "Reply 1 to book an appointment."
                + MENU_HINT
            )
            new_state = "idle"
        elif len(active_patients) == 1:
            p = active_patients[0]
            reply = (
                f"Please type your question for Dr. Kumar about {p['name']}.\n\n"
                "Our doctor will reply within a few hours. 💬"
            )
            new_state = "awaiting_query"
            new_temp  = {"query_patient_id": p["id"]}
        else:
            lines = "Your question is for which patient?\n\n"
            for i, p in enumerate(active_patients, 1):
                code = f" ({p['patient_code']})" if p.get("patient_code") else ""
                lines += f"{i}. {p['name']}{code}\n"
            lines += "\nReply with a number."
            reply     = lines
            new_state = "awaiting_query_patient_select"
            new_temp  = {"query_patients": active_patients}

    elif intent == "query_patient_selected":
        _patients = temp_data.get("query_patients", [])
        try:
            choice = int(t) - 1
            if 0 <= choice < len(_patients):
                selected = _patients[choice]
                reply = (
                    "Please type your question for Dr. Kumar.\n\n"
                    "Our doctor will reply within a few hours. 💬"
                )
                new_state = "awaiting_query"
                new_temp  = {"query_patient_id": selected["id"]}
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

    elif intent == "query_text_provided":
        query_patient_id = temp_data.get("query_patient_id", patient_id)
        try:
            import datetime as _dt
            _supa.table("queries").insert({
                "patient_id": query_patient_id,
                "doctor_id":  doctor_id,
                "question":   text,
                "status":     "Pending",
                "created_at": _dt.datetime.utcnow().isoformat(),
            }).execute()
            print(f"✅ Query saved for patient {query_patient_id}")
        except Exception as _e:
            print(f"❌ Failed to save query: {_e}")
        reply = (
            "✅ Your question has been sent to Dr. Kumar!\n\n"
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
        reply     = build_main_menu(patient_name or "", clinic_name)
        new_state = "idle"

    # Save conversation state
    save_conversation_state(from_number, new_state, new_temp)

    return reply

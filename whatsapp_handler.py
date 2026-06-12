from datetime import datetime, date, timedelta
import re
from database import (
    get_doctor_by_whatsapp, get_patient_by_mobile, get_conversation_state,
    save_conversation_state, get_queue_status, get_patient_token_today, get_family_tokens_today,
    check_holiday, get_booked_slots, get_next_token, create_appointment,
    get_upcoming_appointments, get_family_upcoming_appointments, cancel_appointment, create_patient,
    get_display_token, assign_token_for_slot, is_slot_available, _time_str,
)
from database import supabase as _supa

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

        reply = (
            f"✅ Family member registered!\n\n"
            f"👤 {raw_name}\n"
            f"🪪 {patient_code}\n"
            f"🎂 {age_str} · {gender_clean}\n\n"
            f"Now let's book their appointment.\n\n"
            f"Which date?\n\n"
            f"1. Today ({today.strftime(fmt)})\n"
            f"2. Tomorrow ({tomorrow.strftime(fmt)})\n"
            f"3. Day after ({day_after.strftime(fmt)})\n"
            f"4. Other date (reply with date e.g. 15 June 2026)"
            + MENU_HINT
        )
        new_state = "awaiting_booking_date"
        new_temp  = {
            "booking_for": new_pid,
            "booking_name": raw_name,
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

        if error:
            reply     = error
            new_state = current_state
            new_temp  = temp_data
        else:
            date_obj = datetime.strptime(parsed_date, "%Y-%m-%d").date()
            if date_obj.weekday() == 6:
                reply     = "Sorry! Clinic is closed on Sundays. Please choose Monday to Saturday."
                new_state = "idle"
            else:
                holiday = check_holiday(doctor_id, parsed_date)
                if holiday:
                    reply     = f"Sorry! Clinic is closed on {booking_date} due to {holiday['reason']}. Please choose another date."
                    new_state = "idle"
                else:
                    booked    = get_booked_slots(doctor_id, parsed_date)
                    available = [s for s in ALL_SLOTS if s not in booked][:6]
                    if not available:
                        reply     = f"Sorry! No slots available on {booking_date}. Please try another date."
                        new_state = "idle"
                    else:
                        slot_list = f"Available slots on {booking_date}:\n\n"
                        for i, slot in enumerate(available, 1):
                            slot_list += f"{i}. {format_time(slot)}\n"
                        slot_list += "\nReply with slot number to confirm." + MENU_HINT
                        reply     = slot_list
                        new_state = "awaiting_slot"
                        new_temp  = {
                            "booking_date":    booking_date,
                            "parsed_date":     parsed_date,
                            "available_slots": available,
                            "booking_for":     booking_for,
                            "booking_name":    booking_name,
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

            if not is_slot_available(doctor_id, parsed_date, selected_slot):
                reply = (
                    "Sorry, that slot is already taken. "
                    "Please choose a different time.\n"
                    "Reply MENU to see available slots."
                )
                new_state = "idle"
            else:
                # Token is always server-assigned (reuses cancelled slot's token)
                token = assign_token_for_slot(doctor_id, parsed_date, selected_slot)
                create_appointment(booking_for, doctor_id, parsed_date, selected_slot, token)

                all_appts = _supa.table("appointments").select(
                    "token_number, appointment_time, status"
                ).eq("doctor_id", doctor_id).eq("appointment_date", parsed_date).execute().data or []
                display_tok = get_display_token(token, _time_str(selected_slot), all_appts)

                # Fetch patient_code for confirmation message
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
            # All of today's appointments (any status) for display-token numbering
            all_today = _supa.table("appointments").select(
                "token_number, appointment_time, status"
            ).eq("doctor_id", doctor_id).eq("appointment_date", today_ist).execute().data or []

            def appt_name(a):
                return (a.get("patients") or {}).get("name", "Patient")

            def disp(a):
                return get_display_token(
                    a.get("token_number"), a.get("appointment_time"), all_today
                )

            # Current token shown as display token (M2/E1) when resolvable
            in_prog = next((a for a in all_today if a.get("status") == "In Progress"), None)
            if in_prog:
                current_display = disp(in_prog)
            elif current_token > 0:
                curr = next(
                    (a for a in all_today if a.get("token_number") == current_token), None
                )
                current_display = disp(curr) if curr else str(current_token)
            else:
                current_display = "Not started"

            def session_status(a, is_evening: bool):
                token = a.get("token_number") or 0
                if a.get("status") == "In Progress" or (current_token and token == current_token):
                    return "🟢 Now being seen"
                if a.get("status") == "Completed" or (token and token < current_token):
                    return "✅ Done"
                ahead = len([
                    x for x in all_today
                    if (x.get("token_number") or 0) < token
                    and (x.get("token_number") or 0) > current_token
                    and x.get("status") == "Confirmed"
                    and (_time_str(x.get("appointment_time")) >= "13:00:00") == is_evening
                ])
                wait = ahead * 15
                return f"⏳ ~{wait} mins wait" if wait > 0 else "⏳ Next in line"

            morning = sorted(
                [a for a in my_appts if _time_str(a.get("appointment_time")) < "13:00:00"],
                key=lambda x: x.get("token_number") or 0
            )
            evening = sorted(
                [a for a in my_appts if _time_str(a.get("appointment_time")) >= "13:00:00"],
                key=lambda x: x.get("token_number") or 0
            )

            lines = [f"🏥 {clinic_name} - Live Queue\n",
                     f"Current Token: {current_display}\n"]

            for a in morning:
                lines.append(f"{disp(a)} {appt_name(a)} → {session_status(a, False)}")

            if evening:
                if now_ist.hour < 13:
                    lines.append("\n🌙 Evening Session (5:00 PM – 8:00 PM):")
                    for a in evening:
                        lines.append(
                            f"{disp(a)} {appt_name(a)} → Session not started yet. "
                            f"Check back after 5 PM."
                        )
                else:
                    for a in evening:
                        lines.append(f"{disp(a)} {appt_name(a)} → {session_status(a, True)}")

            lines.append("\nReply MENU for main menu")
            reply = "\n".join(lines)

    # ── CANCEL APPOINTMENT ────────────────────────────────────
    elif intent == "cancel":
        appointments = get_family_upcoming_appointments(from_number, doctor_id)
        if not appointments:
            reply     = "You have no upcoming appointments to cancel.\n\nReply 1 to book an appointment."
            new_state = "idle"
        else:
            # Per-date appointment lists for display-token numbering
            day_cache = {}
            def day_appts(d):
                if d not in day_cache:
                    day_cache[d] = _supa.table("appointments").select(
                        "token_number, appointment_time, status"
                    ).eq("doctor_id", doctor_id).eq("appointment_date", d).execute().data or []
                return day_cache[d]

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
                    d_tok = get_display_token(
                        token, apt["appointment_time"], day_appts(apt["appointment_date"])
                    )
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

                # Display token computed before cancelling (still in session list)
                token_str = ""
                if apt.get("token_number"):
                    day = _supa.table("appointments").select(
                        "token_number, appointment_time, status"
                    ).eq("doctor_id", doctor_id).eq(
                        "appointment_date", apt["appointment_date"]
                    ).execute().data or []
                    d_tok = get_display_token(
                        apt["token_number"], apt["appointment_time"], day
                    )
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

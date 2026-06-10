from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from datetime import date, datetime, timedelta
from supabase import create_client
from twilio.rest import Client
from dotenv import load_dotenv
import os
import config_loader

load_dotenv()

supabase = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_SERVICE_KEY")
)

twilio_client = Client(
    os.getenv("TWILIO_ACCOUNT_SID"),
    os.getenv("TWILIO_AUTH_TOKEN")
)

TWILIO_FROM = os.getenv("TWILIO_WHATSAPP_FROM")


def send_whatsapp(to_number: str, message: str):
    """Send WhatsApp message via Twilio"""
    try:
        msg = twilio_client.messages.create(
            from_=TWILIO_FROM,
            to=f"whatsapp:+{to_number}",
            body=message
        )
        print(f"✅ Sent to {to_number}: SID {msg.sid}")
        return True
    except Exception as e:
        print(f"❌ Twilio error for {to_number}: {e}")
        return False


def get_active_medicines_by_patient(reminder_type: str = "morning"):
    """
    Fetch all active medicines grouped by patient.
    reminder_type: "morning" = all medicines, "evening" = night medicines only
    Returns: dict of {mobile: {patient_info, medicines: []}}
    """
    today = date.today()
    today_str = today.isoformat()

    # Fetch all prescriptions with medicines and patient info
    result = supabase.table("prescriptions").select(
        "id, prescription_date, dietary_instructions, "
        "patient_id, "
        "patients(name, mobile), "
        "doctors(clinic_name), "
        "prescription_medicines(medicine_name, dosage, morning, afternoon, evening, night, before_food, duration_days, instructions, sort_order)"
    ).execute()

    prescriptions = result.data or []

    # Group active medicines by patient mobile
    patients_medicines = {}

    for pres in prescriptions:
        patient = pres.get("patients") or {}
        doctor = pres.get("doctors") or {}
        medicines = pres.get("prescription_medicines", [])
        mobile = patient.get("mobile", "")
        patient_name = patient.get("name", "")
        clinic_name = doctor.get("clinic_name", "Clinic")
        pres_date_str = pres.get("prescription_date", "")
        dietary = pres.get("dietary_instructions", "")

        if not mobile or not medicines or not pres_date_str:
            continue

        pres_date = datetime.strptime(pres_date_str, "%Y-%m-%d").date()
        day_number = (today - pres_date).days + 1

        for med in sorted(medicines, key=lambda x: x.get("sort_order", 0)):
            duration = med.get("duration_days", 1)
            end_date = pres_date + timedelta(days=duration - 1)

            # Skip if medicine course ended
            if today > end_date:
                continue

            # For evening reminder, only include night medicines
            if reminder_type == "evening" and not med.get("night"):
                continue

            if mobile not in patients_medicines:
                patients_medicines[mobile] = {
                    "patient_name": patient_name,
                    "clinic_name": clinic_name,
                    "dietary": dietary,
                    "day_number": day_number,
                    "morning": [],
                    "afternoon": [],
                    "evening": [],
                    "night": []
                }

            # Categorize medicine by timing
            if med.get("morning"):
                patients_medicines[mobile]["morning"].append(med)
            if med.get("afternoon"):
                patients_medicines[mobile]["afternoon"].append(med)
            if med.get("evening"):
                patients_medicines[mobile]["evening"].append(med)
            if med.get("night"):
                patients_medicines[mobile]["night"].append(med)

            # Update dietary if available
            if dietary and not patients_medicines[mobile]["dietary"]:
                patients_medicines[mobile]["dietary"] = dietary

    return patients_medicines


def build_morning_message(mobile: str, data: dict) -> str:
    """Build full day medicine summary message"""
    patient_name = data["patient_name"]
    clinic_name = data["clinic_name"]
    dietary = data["dietary"]
    day_number = data["day_number"]

    morning = data["morning"]
    afternoon = data["afternoon"]
    evening = data["evening"]
    night = data["night"]

    lines = []
    lines.append(f"Good morning {patient_name}! 🌅")
    lines.append(f"")
    lines.append(f"💊 Medicine Reminder - Day {day_number}")
    lines.append(f"")

    if morning:
        lines.append("🌅 Morning:")
        for med in morning:
            food = "before food" if med.get("before_food") else "after food"
            lines.append(f"   • {med['medicine_name']} {med['dosage']} ({food})")

    if afternoon:
        lines.append("🌞 Afternoon:")
        for med in afternoon:
            food = "before food" if med.get("before_food") else "after food"
            lines.append(f"   • {med['medicine_name']} {med['dosage']} ({food})")

    if evening:
        lines.append("🌆 Evening:")
        for med in evening:
            food = "before food" if med.get("before_food") else "after food"
            lines.append(f"   • {med['medicine_name']} {med['dosage']} ({food})")

    if night:
        lines.append("🌙 Night:")
        for med in night:
            food = "before food" if med.get("before_food") else "after food"
            lines.append(f"   • {med['medicine_name']} {med['dosage']} ({food})")

    if dietary:
        lines.append("")
        lines.append(f"🥗 Diet: {dietary}")

    lines.append("")
    lines.append("Take care and get well soon!")
    lines.append(f"- {clinic_name}")

    return "\n".join(lines)


def build_evening_message(mobile: str, data: dict) -> str:
    """Build night medicine reminder message"""
    patient_name = data["patient_name"]
    clinic_name = data["clinic_name"]
    night = data["night"]

    lines = []
    lines.append(f"Good evening {patient_name}! 🌙")
    lines.append("")
    lines.append("Don't forget your night medicines:")
    lines.append("")

    for med in night:
        food = "before food" if med.get("before_food") else "after food"
        instructions = med.get("instructions", "")
        inst_str = f" - {instructions}" if instructions else ""
        lines.append(f"🌙 {med['medicine_name']} {med['dosage']}{inst_str} ({food})")

    lines.append("")
    lines.append("Good night! Rest well. 😴")
    lines.append(f"- {clinic_name}")

    return "\n".join(lines)


async def send_morning_reminders():
    """
    8AM Job: Send full day medicine summary to all patients
    with active prescriptions. One message per patient.
    """
    print("🌅 Running: Morning Medicine Reminder Job")

    patients_medicines = get_active_medicines_by_patient("morning")
    print(f"Found {len(patients_medicines)} patients with active medicines")

    for mobile, data in patients_medicines.items():
        message = build_morning_message(mobile, data)
        send_whatsapp(mobile, message)
        print(f"✅ Morning reminder sent to {data['patient_name']} ({mobile})")


async def send_evening_reminders():
    """
    8PM Job: Send night medicine reminder only if patient
    has night medicines. One message per patient.
    """
    print("🌙 Running: Evening Medicine Reminder Job")

    patients_medicines = get_active_medicines_by_patient("evening")

    # Filter only patients who have night medicines
    night_patients = {
        mobile: data for mobile, data in patients_medicines.items()
        if data["night"]
    }

    print(f"Found {len(night_patients)} patients with night medicines")

    for mobile, data in night_patients.items():
        message = build_evening_message(mobile, data)
        send_whatsapp(mobile, message)
        print(f"✅ Evening reminder sent to {data['patient_name']} ({mobile})")


async def send_visit_summary():
    """
    6PM Job: Send visit summary to patients who visited today.
    """
    print("🏥 Running: Visit Summary Job")
    import pytz
    IST = pytz.timezone('Asia/Kolkata')
    today_ist = datetime.now(IST).date()
    range_start = f"{today_ist.isoformat()}T00:00:00+05:30"
    range_end   = f"{(today_ist + timedelta(days=1)).isoformat()}T00:00:00+05:30"

    result = supabase.table("visits").select(
        "id, patient_id, doctor_id, diagnosis, follow_up_date, "
        "patients(name, mobile), doctors(clinic_name, name)"
    ).gte("created_at", range_start).lt("created_at", range_end).execute()

    visits = result.data or []
    print(f"Found {len(visits)} visits today")

    for visit in visits:
        try:
            patient = visit.get("patients", {})
            doctor  = visit.get("doctors", {})
            patient_name = patient.get("name", "Patient")
            mobile = patient.get("mobile", "")
            _clinic_name  = config_loader.clinic_name() or doctor.get("clinic_name", "Clinic")
            _doctor_name  = config_loader.doctor_name() or doctor.get("name", "Doctor")
            follow_up = visit.get("follow_up_date", "")
            diagnosis = visit.get("diagnosis", "")

            if not mobile:
                continue

            follow_up_str = ""
            if follow_up:
                follow_up_date = datetime.strptime(follow_up, "%Y-%m-%d")
                follow_up_str = f"\n📅 Next Review: {follow_up_date.strftime('%d %B %Y')}"

            # Try DB template, fall back to hardcoded
            template_msg = config_loader.get_template(
                "visit_summary", "english",
                {"name": patient_name, "clinic": _clinic_name,
                 "diagnosis": diagnosis, "followup_str": follow_up_str,
                 "doctor": _doctor_name}
            )
            if template_msg:
                message = template_msg
            else:
                message = (
                    f"Dear {patient_name},\n\n"
                    f"Thank you for visiting {_clinic_name} today. 🙏\n\n"
                    f"Diagnosis: {diagnosis}"
                    f"{follow_up_str}\n\n"
                    f"Please follow the prescribed medicines and instructions.\n\n"
                    f"For any queries reply to this message.\n"
                    f"- {_doctor_name}"
                )

            send_whatsapp(mobile, message)

        except Exception as e:
            print(f"❌ Error sending visit summary: {e}")


async def send_review_requests():
    """
    10AM Job: Send Google review to patients who visited 7 days ago.
    Uses created_at with IST date range to handle timezone correctly.
    """
    print("⭐ Running: Review Request Job")
    import pytz
    IST = pytz.timezone('Asia/Kolkata')
    today_ist = datetime.now(IST).date()
    seven_days_ago = today_ist - timedelta(days=7)
    six_days_ago  = today_ist - timedelta(days=6)

    # Range: entire IST day 7 days ago (00:00 to 24:00 IST = +05:30)
    range_start = f"{seven_days_ago.isoformat()}T00:00:00+05:30"
    range_end   = f"{six_days_ago.isoformat()}T00:00:00+05:30"

    result = supabase.table("visits").select(
        "id, patient_id, created_at, "
        "patients(name, mobile), "
        "doctors(clinic_name, name)"
    ).gte("created_at", range_start).lt("created_at", range_end).execute()

    visits = result.data or []
    print(f"Found {len(visits)} visits from 7 days ago")

    for visit in visits:
        try:
            patient = visit.get("patients", {})
            doctor  = visit.get("doctors", {})
            patient_name = patient.get("name", "Patient")
            mobile = patient.get("mobile", "")
            _clinic_name  = config_loader.clinic_name() or doctor.get("clinic_name", "Clinic")
            _doctor_name  = config_loader.doctor_name() or doctor.get("name", "Doctor")
            _review_link  = config_loader.google_review_link()

            if not mobile:
                continue

            # Try DB template, fall back to hardcoded
            template_msg = config_loader.get_template(
                "review_request", "english",
                {"name": patient_name, "clinic": _clinic_name,
                 "doctor": _doctor_name, "review_link": _review_link}
            )
            if template_msg:
                message = template_msg
            else:
                message = (
                    f"Dear {patient_name},\n\n"
                    f"We hope you are feeling much better now! 😊\n\n"
                    f"It has been a week since your visit to {_clinic_name}. "
                    f"Your feedback means a lot to us!\n\n"
                    f"⭐ Please take 1 minute to share your experience:\n"
                    f"{_review_link}\n\n"
                    f"Thank you for trusting us with your health!\n"
                    f"- {_doctor_name} & Team"
                )

            send_whatsapp(mobile, message)

        except Exception as e:
            print(f"❌ Error sending review request: {e}")


async def init_scheduler() -> AsyncIOScheduler:
    """
    Initialize scheduler with times and feature flags loaded from clinic_config DB.
    Each job is only registered if its feature flag is enabled.
    """
    from followup import send_followup_whatsapp_job, make_followup_calls_job

    # Warm the config cache once
    config_loader.load_config()

    scheduler = AsyncIOScheduler(timezone="Asia/Kolkata")

    def add_job_if_enabled(feature: str, job_id: str, job_name: str, func):
        if not config_loader.is_enabled(feature):
            print(f"⏭️  {job_name} disabled (feature.{feature}.enabled=false)")
            return
        h, m = config_loader.get_scheduler_time(job_id)
        scheduler.add_job(
            func,
            CronTrigger(hour=h, minute=m, timezone="Asia/Kolkata"),
            id=job_id,
            name=job_name,
            replace_existing=True,
            misfire_grace_time=300  # fire even if up to 5 min late (e.g. after reload)
        )
        print(f"   ✅ {job_name}: {h:02d}:{m:02d} IST")

    print("⏰ Initializing scheduler from DB config:")
    add_job_if_enabled("morning_reminders",  "morning_reminders",  "Morning Medicine Reminders",    send_morning_reminders)
    add_job_if_enabled("evening_reminders",  "evening_reminders",  "Evening Night Medicine Reminders", send_evening_reminders)
    add_job_if_enabled("visit_summary",      "visit_summary",      "Evening Visit Summary",          send_visit_summary)
    add_job_if_enabled("review_requests",    "review_requests",    "Day 7 Review Requests",          send_review_requests)
    add_job_if_enabled("followup_whatsapp",  "followup_whatsapp",  "Follow-up WhatsApp",             send_followup_whatsapp_job)
    add_job_if_enabled("followup_calls",     "followup_calls",     "Follow-up Voice Calls",          make_followup_calls_job)

    return scheduler


async def reschedule(scheduler: AsyncIOScheduler):
    """
    Remove all existing jobs and re-add with fresh config from DB.
    Called by /config/reload-scheduler endpoint.
    """
    config_loader.invalidate_cache()
    scheduler.remove_all_jobs()
    await init_scheduler.__wrapped__(scheduler) if hasattr(init_scheduler, "__wrapped__") else None
    # Simpler: just re-populate jobs inline
    from followup import send_followup_whatsapp_job, make_followup_calls_job
    config_loader.load_config()

    def re_add(feature: str, job_id: str, job_name: str, func):
        if not config_loader.is_enabled(feature):
            return
        h, m = config_loader.get_scheduler_time(job_id)
        scheduler.add_job(
            func,
            CronTrigger(hour=h, minute=m, timezone="Asia/Kolkata"),
            id=job_id,
            name=job_name,
            replace_existing=True,
            misfire_grace_time=300  # fire even if up to 5 min late
        )

    re_add("morning_reminders", "morning_reminders", "Morning Medicine Reminders",       send_morning_reminders)
    re_add("evening_reminders", "evening_reminders", "Evening Night Medicine Reminders",  send_evening_reminders)
    re_add("visit_summary",     "visit_summary",     "Evening Visit Summary",             send_visit_summary)
    re_add("review_requests",   "review_requests",   "Day 7 Review Requests",             send_review_requests)
    re_add("followup_whatsapp", "followup_whatsapp", "Follow-up WhatsApp",                send_followup_whatsapp_job)
    re_add("followup_calls",    "followup_calls",    "Follow-up Voice Calls",             make_followup_calls_job)
    print("🔄 Scheduler reloaded with fresh DB config")

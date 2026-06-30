from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from datetime import date, datetime, timedelta
from supabase import create_client
from dotenv import load_dotenv
import os
import config_loader

load_dotenv()

supabase = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_SERVICE_KEY")
)


async def send_whatsapp(to_number: str, message: str):
    """Send WhatsApp message via Meta Cloud API"""
    try:
        # deferred import: main imports this module at startup
        from main import send_meta_text
        result = await send_meta_text(to_number, message)
        if result.get("messages"):
            print(f"✅ Sent to {to_number} via Meta")
            return True
        print(f"❌ Meta send failed for {to_number}: {result}")
        return False
    except Exception as e:
        print(f"❌ Meta error for {to_number}: {e}")
        return False


def get_active_medicines_by_patient(reminder_type: str = "morning", doctor_id: str = None):
    """
    Fetch all active medicines grouped by patient_id.
    reminder_type: "morning" = all medicines, "evening" = night medicines only
    doctor_id: if provided, only fetch prescriptions for this doctor
    Returns: dict of {patient_id: {mobile, patient_info, medicines: []}}
    """
    today = date.today()

    # Fetch prescriptions with medicines and patient info
    q = supabase.table("prescriptions").select(
        "id, prescription_date, dietary_instructions, "
        "patient_id, doctor_id, "
        "patients(name, mobile), "
        "doctors(clinic_name), "
        "prescription_medicines(medicine_name, dosage, morning, afternoon, evening, night, before_food, duration_days, instructions, sort_order)"
    )
    if doctor_id:
        q = q.eq("doctor_id", doctor_id)
    result = q.execute()

    prescriptions = result.data or []

    # Group active medicines by patient_id (not mobile) to avoid merging
    # different patients who share the same test/family phone number.
    patients_medicines = {}

    for pres in prescriptions:
        patient = pres.get("patients") or {}
        doctor = pres.get("doctors") or {}
        medicines = pres.get("prescription_medicines", [])
        patient_id = pres.get("patient_id", "")
        mobile = patient.get("mobile", "")
        patient_name = patient.get("name", "")
        clinic_name = doctor.get("clinic_name", "Clinic")
        pres_date_str = pres.get("prescription_date", "")
        dietary = pres.get("dietary_instructions", "")

        if not patient_id or not mobile or not medicines or not pres_date_str:
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

            if patient_id not in patients_medicines:
                patients_medicines[patient_id] = {
                    "mobile": mobile,
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
                patients_medicines[patient_id]["morning"].append(med)
            if med.get("afternoon"):
                patients_medicines[patient_id]["afternoon"].append(med)
            if med.get("evening"):
                patients_medicines[patient_id]["evening"].append(med)
            if med.get("night"):
                patients_medicines[patient_id]["night"].append(med)

            # Update dietary if available
            if dietary and not patients_medicines[patient_id]["dietary"]:
                patients_medicines[patient_id]["dietary"] = dietary

    return patients_medicines


def build_morning_message(data: dict) -> str:
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


def build_evening_message(data: dict) -> str:
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


def make_morning_reminder_job(doctor_id: str):
    async def send_morning_reminders():
        print(f"🌅 Running: Morning Medicine Reminder Job for doctor {doctor_id[:8]}")
        patients_medicines = get_active_medicines_by_patient("morning", doctor_id=doctor_id)
        print(f"Found {len(patients_medicines)} patients with active medicines")
        for patient_id, data in patients_medicines.items():
            mobile = data["mobile"]
            message = build_morning_message(data)
            await send_whatsapp(mobile, message)
            print(f"✅ Morning reminder sent to {data['patient_name']} ({mobile})")
    return send_morning_reminders


def make_evening_reminder_job(doctor_id: str):
    async def send_evening_reminders():
        print(f"🌙 Running: Evening Medicine Reminder Job for doctor {doctor_id[:8]}")
        patients_medicines = get_active_medicines_by_patient("evening", doctor_id=doctor_id)
        night_patients = {pid: data for pid, data in patients_medicines.items() if data["night"]}
        print(f"Found {len(night_patients)} patients with night medicines")
        for patient_id, data in night_patients.items():
            mobile = data["mobile"]
            message = build_evening_message(data)
            await send_whatsapp(mobile, message)
            print(f"✅ Evening reminder sent to {data['patient_name']} ({mobile})")
    return send_evening_reminders


async def get_all_active_doctors() -> list:
    """Return all doctors where is_available = true. Used by multi-doctor scheduler loops."""
    try:
        result = supabase.table("doctors") \
            .select("id, name, whatsapp_number, mobile, clinic_name") \
            .eq("is_available", True) \
            .execute()
        return result.data or []
    except Exception as e:
        print(f"⚠️ get_all_active_doctors failed: {e}")
        return []


def _is_current_time_match(time_str: str) -> bool:
    """Return True if the current IST time matches HH:MM (within the current minute)."""
    import pytz
    IST = pytz.timezone("Asia/Kolkata")
    now = datetime.now(IST).strftime("%H:%M")
    return now == time_str.strip()


def _format_daily_summary(doctor: dict, today_appts: int, yesterday_seen: int,
                           pending: dict) -> str:
    name = doctor.get("name", "Doctor")
    return (
        f"Good morning {name}! 🌅\n\n"
        f"Today's Schedule:\n"
        f"📋 {today_appts} appointments booked\n\n"
        f"Yesterday:\n"
        f"✅ {yesterday_seen} patients seen\n\n"
        f"Pending:\n"
        f"💬 {pending.get('followup_concerns', 0)} follow-up concerns\n"
        f"🔔 {pending.get('pending_queries', 0)} unanswered queries\n"
        f"🔬 {pending.get('pending_lab_reports', 0)} lab reports\n\n"
        f"Have a great day! 🙏"
    )


async def send_daily_doctor_summary():
    """
    Runs on a 15-minute check loop and sends the morning summary to each doctor
    at their configured time (scheduler.daily_summary.time, default 08:00 IST).
    Each doctor is wrapped in try/except so one failure never blocks others.
    """
    print("📊 Running: Daily Doctor Summary check")
    from analytics import get_stats as _get_stats, get_pending_items as _get_pending_items

    doctors = await get_all_active_doctors()
    for doctor in doctors:
        try:
            configured_time = config_loader.get(
                "scheduler.daily_summary.time", "08:00", doctor["id"]
            )
            if not _is_current_time_match(configured_time):
                continue

            if not config_loader.is_enabled("daily_summary", doctor["id"]):
                continue

            doctor_id = doctor["id"]
            today_appts = _get_stats(doctor_id, "appointment_count", "today").get("value", 0)
            yesterday_seen = _get_stats(doctor_id, "completed_visit_count", "yesterday").get("value", 0)
            pending = _get_pending_items(doctor_id)

            message = _format_daily_summary(doctor, today_appts, yesterday_seen, pending)

            mobile = doctor.get("mobile") or doctor.get("whatsapp_number", "")
            if not mobile:
                print(f"⚠️ [{doctor['name']}] No mobile for daily summary")
                continue

            await send_whatsapp(mobile, message)
            print(f"✅ Daily summary sent to {doctor['name']} ({mobile})")

        except Exception as e:
            print(f"❌ Daily summary failed for {doctor.get('name', '?')}: {e}")


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
        "id, patient_id, doctor_id, diagnosis, follow_up_date, visit_status, "
        "patients(name, mobile), doctors(clinic_name, name)"
    ).gte("created_at", range_start).lt("created_at", range_end).execute()

    visits = result.data or []
    print(f"Found {len(visits)} visits today")

    for visit in visits:
        try:
            # Skip visits never completed (e.g. abandoned pre-created visits)
            if visit.get("visit_status") == "In Progress":
                continue
            patient = visit.get("patients", {})
            doctor  = visit.get("doctors", {})
            visit_doctor_id = visit.get("doctor_id", "")
            patient_name = patient.get("name", "Patient")
            mobile = patient.get("mobile", "")
            _clinic_name  = config_loader.clinic_name(visit_doctor_id) or doctor.get("clinic_name", "Clinic")
            _doctor_name  = config_loader.doctor_name(visit_doctor_id) or doctor.get("name", "Doctor")
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

            await send_whatsapp(mobile, message)

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

            await send_whatsapp(mobile, message)

        except Exception as e:
            print(f"❌ Error sending review request: {e}")


def _populate_scheduler(scheduler: AsyncIOScheduler):
    """Add per-doctor jobs to the scheduler. Called by init and reschedule."""
    from followup import send_followup_whatsapp_job, make_followup_calls_job, mark_no_response_followups

    # Fetch all active doctors
    try:
        doctors_res = supabase.table("doctors").select("id, name").eq("is_available", True).execute()
        doctors = doctors_res.data or []
    except Exception as e:
        print(f"⚠️ Failed to fetch doctors for scheduler: {e}")
        doctors = []

    if not doctors:
        print("⚠️ No active doctors found — scheduler will be empty")
        return

    print(f"⏰ Scheduling jobs for {len(doctors)} doctor(s):")

    for doc in doctors:
        did = doc["id"]
        dname = doc["name"]
        short = did[:8]

        def add(feature, job_id, label, func):
            if not config_loader.is_enabled(feature, did):
                print(f"   ⏭️  [{dname}] {label} disabled")
                return
            h, m = config_loader.get_scheduler_time(job_id, did)
            scheduler.add_job(
                func,
                CronTrigger(hour=h, minute=m, timezone="Asia/Kolkata"),
                id=f"{job_id}_{short}",
                name=f"{label} [{dname}]",
                replace_existing=True,
                misfire_grace_time=300,
            )
            print(f"   ✅ [{dname}] {label}: {h:02d}:{m:02d} IST")

        add("morning_reminders", "morning_reminders", "Morning Reminders",   make_morning_reminder_job(did))
        add("evening_reminders", "evening_reminders", "Evening Reminders",   make_evening_reminder_job(did))
        add("visit_summary",     "visit_summary",     "Visit Summary",       send_visit_summary)
        add("review_requests",   "review_requests",   "Review Requests",     send_review_requests)
        add("followup_whatsapp", "followup_whatsapp", "Followup WhatsApp",   send_followup_whatsapp_job)
        add("followup_calls",    "followup_calls",    "Followup Calls",      make_followup_calls_job)

    # Daily doctor summary — single job runs every 15 mins and checks each doctor's configured time
    scheduler.add_job(
        send_daily_doctor_summary,
        "interval",
        minutes=15,
        id="daily_doctor_summary",
        name="Daily Doctor Summary",
        replace_existing=True,
        misfire_grace_time=300,
    )
    print("   ✅ [Global] Daily Doctor Summary: checks every 15 min")

    # No-response marker runs once daily (not per-doctor) — add only once
    scheduler.add_job(
        mark_no_response_followups,
        CronTrigger(hour=9, minute=0, timezone="Asia/Kolkata"),
        id="followup_no_response",
        name="Followup No-Response Marker",
        replace_existing=True,
        misfire_grace_time=300,
    )
    print("   ✅ [Global] No-Response Marker: 09:00 IST")


async def init_scheduler() -> AsyncIOScheduler:
    """
    Initialize per-doctor scheduler. Each doctor gets their own set of jobs
    running at their configured times with their feature flags.
    """
    config_loader.invalidate_cache()
    scheduler = AsyncIOScheduler(timezone="Asia/Kolkata")
    _populate_scheduler(scheduler)
    return scheduler


async def reschedule(scheduler: AsyncIOScheduler):
    """Remove all jobs and re-add with fresh config. Called by /config/reload-scheduler."""
    config_loader.invalidate_cache()
    scheduler.remove_all_jobs()
    _populate_scheduler(scheduler)
    print("🔄 Scheduler reloaded with fresh per-doctor config")

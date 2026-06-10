"""
seed_test_data.py — Full test data reset for PRA

Wipes all patient-related tables (keeps doctors) then inserts
5 real patients that exercise every scheduled job and flow:

  Patient         Mobile        Flow covered
  ──────────────  ────────────  ──────────────────────────────────────────────
  Sujaikumar      9047099959    Active prescription → morning + evening reminders
  Dhanvanth       9047099959    Family (son of Sujaikumar), followup WhatsApp today
  Poornima        9943941314    WA followup sent, no reply → voice call pending
  Subramaniam     9965553323    Visited 7 days ago → Google review today
  Selvarani       9047099979    Pending query + answered query, visit summary today

  Queue today: token 1 (Sujaikumar)=Done, 2 (Dhanvanth)=In Progress,
               3 (Selvarani)=Waiting, 4 (Poornima)=Waiting

Usage:
  cd pra-backend
  source venv/bin/activate
  python3 seed_test_data.py
"""

import os
import sys
from datetime import datetime, timedelta
from dotenv import load_dotenv
import pytz

load_dotenv()

from supabase import create_client
supabase = create_client(
    os.environ["SUPABASE_URL"],
    os.environ["SUPABASE_SERVICE_KEY"]
)

IST = pytz.timezone("Asia/Kolkata")

def today():      return datetime.now(IST).date()
def days_ago(n):  return (today() - timedelta(days=n)).isoformat()
def days_fwd(n):  return (today() + timedelta(days=n)).isoformat()
def ts(d, t):     return f"{d}T{t}+05:30"   # IST timestamp helper

# ── Constants ───────────────────────────────────────────────────────────
DOCTOR_ID = "8c33abe0-5d2e-4613-9437-c7c375e8d162"

TODAY     = today().isoformat()
YESTERDAY = days_ago(1)
SEVEN_AGO = days_ago(7)
THREE_FWD = days_fwd(3)

# Patients
SUJAI = "aaaaaaaa-1001-1001-1001-000000000001"  # Sujaikumar — morning/evening reminders
DHAN  = "aaaaaaaa-1002-1002-1002-000000000002"  # Dhanvanth  — family son, followup WA today
POOR  = "aaaaaaaa-1003-1003-1003-000000000003"  # Poornima   — voice call pending
SUBR  = "aaaaaaaa-1004-1004-1004-000000000004"  # Subramaniam — Google review
SELV  = "aaaaaaaa-1005-1005-1005-000000000005"  # Selvarani  — queries + visit summary

# Appointments
A_SUJAI = "bbbbbbbb-1001-1001-1001-000000000001"  # today token 1
A_DHAN  = "bbbbbbbb-1002-1002-1002-000000000002"  # today token 2
A_SELV  = "bbbbbbbb-1005-1005-1005-000000000005"  # today token 3
A_POOR  = "bbbbbbbb-1003-1003-1003-000000000003"  # today token 4
A_POOR2 = "bbbbbbbb-1003-1003-1003-000000000099"  # yesterday (for voice call prescription)
A_SUBR  = "bbbbbbbb-1004-1004-1004-000000000004"  # 7 days ago

# Visits
V_SUJAI = "cccccccc-1001-1001-1001-000000000001"  # today — active prescription src
V_DHAN  = "cccccccc-1002-1002-1002-000000000002"  # today — followup WA src
V_POOR  = "cccccccc-1003-1003-1003-000000000003"  # yesterday — voice call src
V_SUBR  = "cccccccc-1004-1004-1004-000000000004"  # 7 days ago — Google review src
V_SELV  = "cccccccc-1005-1005-1005-000000000005"  # today — visit summary + query src

# Prescriptions
RX_SUJAI = "dddddddd-1001-1001-1001-000000000001"  # active 5-day → morning/evening reminders
RX_DHAN  = "dddddddd-1002-1002-1002-000000000002"  # 3-day ended yesterday → followup WA
RX_POOR  = "dddddddd-1003-1003-1003-000000000003"  # WA sent, no reply → voice call
RX_SELV  = "dddddddd-1005-1005-1005-000000000005"  # today

# Followups
FU_DHAN = "eeeeeeee-1002-1002-1002-000000000002"  # Dhanvanth followup today → WA job
FU_SELV = "eeeeeeee-1005-1005-1005-000000000005"  # Selvarani followup 3 days ahead (future)

# Queries
Q_SELV_PENDING = "ffffffff-1005-1005-1005-000000000001"  # Selvarani pending query
Q_SELV_CLOSED  = "ffffffff-1005-1005-1005-000000000002"  # Selvarani answered query
Q_SUJAI_PENDING = "ffffffff-1001-1001-1001-000000000001"  # Sujaikumar pending query


# ── Wipe ────────────────────────────────────────────────────────────────
def wipe_all():
    uuid_tables = [
        "prescription_medicines",
        "prescriptions",
        "followups",
        "queries",
        "reviews",
        "visits",
        "appointments",
        "tokens",
        "patients",
    ]
    print("🗑️  Wiping tables...")
    for table in uuid_tables:
        try:
            supabase.table(table).delete().gte("id", "00000000-0000-0000-0000-000000000000").execute()
            print(f"   ✓ {table}")
        except Exception as e:
            print(f"   ✗ {table}: {e}")
    try:
        supabase.table("conversation_state").delete().neq("mobile", "").execute()
        print("   ✓ conversation_state")
    except Exception as e:
        print(f"   ✗ conversation_state: {e}")


# ── Seed ────────────────────────────────────────────────────────────────

def seed_patients():
    rows = [
        # Sujaikumar — 43 yrs, M, DOB 05-06-1983, mobile 9047099959
        {
            "id": SUJAI, "patient_code": "SUJ-9959-1983",
            "name": "Sujaikumar",
            "mobile": "919047099959", "age": 43, "gender": "Male",
            "date_of_birth": "1983-06-05",
            "language": "tamil", "registration_source": "whatsapp",
            "created_at": ts(TODAY, "08:00:00"),
        },
        # Dhanvanth — 13 yrs, M, DOB 03-04-2013, mobile 9047099959 (son of Sujaikumar)
        {
            "id": DHAN, "patient_code": "DHA-9959-2013",
            "name": "Dhanvanth",
            "mobile": "919047099959", "age": 13, "gender": "Male",
            "date_of_birth": "2013-04-03",
            "language": "tamil", "registration_source": "whatsapp",
            "family_head_mobile": "919047099959",
            "created_at": ts(TODAY, "08:05:00"),
        },
        # Poornima — 43 yrs, F, DOB 02-05-1983, mobile 9943941314
        {
            "id": POOR, "patient_code": "POO-1314-1983",
            "name": "Poornima",
            "mobile": "919943941314", "age": 43, "gender": "Female",
            "date_of_birth": "1983-05-02",
            "language": "tamil", "registration_source": "whatsapp",
            "created_at": ts(YESTERDAY, "09:00:00"),
        },
        # Subramaniam — 71 yrs, M, DOB 31-01-1955, mobile 9965553323
        {
            "id": SUBR, "patient_code": "SUB-3323-1955",
            "name": "Subramaniam",
            "mobile": "919965553323", "age": 71, "gender": "Male",
            "date_of_birth": "1955-01-31",
            "language": "tamil", "registration_source": "whatsapp",
            "created_at": ts(SEVEN_AGO, "08:00:00"),
        },
        # Selvarani — 61 yrs, F, DOB 20-04-1965, mobile 9047099979
        {
            "id": SELV, "patient_code": "SEL-9979-1965",
            "name": "Selvarani",
            "mobile": "919047099979", "age": 61, "gender": "Female",
            "date_of_birth": "1965-04-20",
            "language": "tamil", "registration_source": "whatsapp",
            "created_at": ts(TODAY, "07:30:00"),
        },
    ]
    supabase.table("patients").insert(rows).execute()
    print(f"   ✓ patients ({len(rows)} rows)")


def seed_appointments():
    rows = [
        # Today's queue: Sujaikumar=1, Dhanvanth=2, Selvarani=3, Poornima=4
        {"id": A_SUJAI, "patient_id": SUJAI, "doctor_id": DOCTOR_ID, "appointment_date": TODAY,     "appointment_time": "09:00", "token_number": 1, "status": "Confirmed"},
        {"id": A_DHAN,  "patient_id": DHAN,  "doctor_id": DOCTOR_ID, "appointment_date": TODAY,     "appointment_time": "09:15", "token_number": 2, "status": "Confirmed"},
        {"id": A_SELV,  "patient_id": SELV,  "doctor_id": DOCTOR_ID, "appointment_date": TODAY,     "appointment_time": "09:30", "token_number": 3, "status": "Confirmed"},
        {"id": A_POOR,  "patient_id": POOR,  "doctor_id": DOCTOR_ID, "appointment_date": TODAY,     "appointment_time": "09:45", "token_number": 4, "status": "Confirmed"},
        # Poornima also visited yesterday (source of voice-call prescription)
        {"id": A_POOR2, "patient_id": POOR,  "doctor_id": DOCTOR_ID, "appointment_date": YESTERDAY, "appointment_time": "10:00", "token_number": 2, "status": "Confirmed"},
        # Subramaniam visited 7 days ago
        {"id": A_SUBR,  "patient_id": SUBR,  "doctor_id": DOCTOR_ID, "appointment_date": SEVEN_AGO, "appointment_time": "10:30", "token_number": 1, "status": "Confirmed"},
    ]
    supabase.table("appointments").insert(rows).execute()
    print(f"   ✓ appointments ({len(rows)} rows)")


def seed_tokens():
    # current_token=1: Sujaikumar=Done, Dhanvanth=In Progress, Selvarani+Poornima=Waiting
    rows = [
        {"doctor_id": DOCTOR_ID, "queue_date": TODAY, "current_token": 1, "total_tokens": 4, "is_active": True},
    ]
    supabase.table("tokens").insert(rows).execute()
    print("   ✓ tokens (today: current=1, total=4)")


def seed_visits():
    rows = [
        # Sujaikumar — visited today, active prescription → morning/evening reminders
        {
            "id": V_SUJAI, "patient_id": SUJAI, "doctor_id": DOCTOR_ID, "appointment_id": A_SUJAI,
            "visit_date": TODAY,
            "chief_complaint": "Fever, body ache",
            "symptoms": "Fever 38.5°C, headache, body ache since 2 days",
            "diagnosis": "Viral fever",
            "notes": "Rest for 3 days. Plenty of fluids. Avoid cold food.",
            "follow_up_date": THREE_FWD,
            "follow_up_notes": "Review if fever persists beyond 3 days",
            "visit_status": "Completed",
            "created_at": ts(TODAY, "09:30:00"),
        },
        # Dhanvanth — visited today, 3-day prescription → followup WA in 3 days
        # Followup scheduled today (we set scheduled_date=TODAY to trigger immediately)
        {
            "id": V_DHAN, "patient_id": DHAN, "doctor_id": DOCTOR_ID, "appointment_id": A_DHAN,
            "visit_date": TODAY,
            "chief_complaint": "Loose stools, mild fever",
            "symptoms": "Loose stools x4/day, fever 37.8°C",
            "diagnosis": "Acute gastroenteritis",
            "notes": "ORS after every loose stool. Light diet.",
            "follow_up_date": THREE_FWD,
            "visit_status": "Completed",
            "created_at": ts(TODAY, "09:45:00"),
        },
        # Poornima — visited YESTERDAY, WA followup sent, awaiting voice call
        {
            "id": V_POOR, "patient_id": POOR, "doctor_id": DOCTOR_ID, "appointment_id": A_POOR2,
            "visit_date": YESTERDAY,
            "chief_complaint": "Knee pain, swelling",
            "diagnosis": "Osteoarthritis right knee",
            "notes": "Avoid stair climbing. Apply warm compress twice daily.",
            "visit_status": "Completed",
            "created_at": ts(YESTERDAY, "10:30:00"),
        },
        # Subramaniam — visited 7 DAYS AGO → triggers Google review job today
        {
            "id": V_SUBR, "patient_id": SUBR, "doctor_id": DOCTOR_ID, "appointment_id": A_SUBR,
            "visit_date": SEVEN_AGO,
            "chief_complaint": "BP check, dizziness",
            "diagnosis": "Hypertension Grade 1",
            "notes": "Low salt diet. Monitor BP daily. Follow up in 1 week.",
            "visit_status": "Completed",
            "created_at": ts(SEVEN_AGO, "10:00:00"),
        },
        # Selvarani — visited today → visit summary tonight + query
        {
            "id": V_SELV, "patient_id": SELV, "doctor_id": DOCTOR_ID, "appointment_id": A_SELV,
            "visit_date": TODAY,
            "chief_complaint": "Sore throat, mild cough",
            "diagnosis": "Acute pharyngitis",
            "notes": "Warm water gargles. Steam inhalation. Avoid cold drinks.",
            "follow_up_date": THREE_FWD,
            "visit_status": "Completed",
            "created_at": ts(TODAY, "09:00:00"),
        },
    ]
    supabase.table("visits").insert(rows).execute()
    print(f"   ✓ visits ({len(rows)} rows)")


def seed_prescriptions():
    rows = [
        # Sujaikumar — ACTIVE 5-day course starting today → morning + evening reminders
        {
            "id": RX_SUJAI, "patient_id": SUJAI, "doctor_id": DOCTOR_ID, "visit_id": V_SUJAI,
            "prescription_date": TODAY,
            "dietary_instructions": "Avoid oily/spicy food. Drink warm water.",
            "precautions": "Rest at home. Avoid exposure to rain.",
            "general_notes": "Review after 3 days if fever persists.",
            "followup_whatsapp_sent": False, "followup_replied": False, "followup_call_sent": False,
        },
        # Dhanvanth — 3-day course starts today → followup WA scheduled today (immediate test)
        {
            "id": RX_DHAN, "patient_id": DHAN, "doctor_id": DOCTOR_ID, "visit_id": V_DHAN,
            "prescription_date": TODAY,
            "dietary_instructions": "ORS after each loose stool. Rice kanji. Avoid dairy.",
            "followup_whatsapp_sent": False, "followup_replied": False, "followup_call_sent": False,
        },
        # Poornima — WA followup already SENT, no reply → voice call pending
        {
            "id": RX_POOR, "patient_id": POOR, "doctor_id": DOCTOR_ID, "visit_id": V_POOR,
            "prescription_date": YESTERDAY,
            "dietary_instructions": "Apply warm compress. Avoid prolonged standing.",
            "followup_whatsapp_sent": True,
            "followup_whatsapp_sent_at": ts(YESTERDAY, "08:00:00"),
            "followup_replied": False,
            "followup_call_sent": False,
        },
        # Selvarani — prescription today
        {
            "id": RX_SELV, "patient_id": SELV, "doctor_id": DOCTOR_ID, "visit_id": V_SELV,
            "prescription_date": TODAY,
            "dietary_instructions": "Warm soups. Honey + ginger for throat.",
            "precautions": "Avoid cold drinks and ice cream.",
            "followup_whatsapp_sent": False, "followup_replied": False, "followup_call_sent": False,
        },
    ]
    supabase.table("prescriptions").insert(rows).execute()
    print(f"   ✓ prescriptions ({len(rows)} rows)")


def seed_prescription_medicines():
    rows = [
        # Sujaikumar — Paracetamol M+A+N (5 days), Cetirizine N (5 days), ORS M+A+E (3 days)
        # → morning reminder: Paracetamol + ORS
        # → evening reminder: Paracetamol + Cetirizine (night meds)
        {"prescription_id": RX_SUJAI, "medicine_name": "Paracetamol 650mg", "dosage": "1 tablet",
         "morning": True,  "afternoon": True,  "evening": False, "night": True,
         "before_food": False, "duration_days": 5, "sort_order": 1},
        {"prescription_id": RX_SUJAI, "medicine_name": "Cetirizine 10mg",   "dosage": "1 tablet",
         "morning": False, "afternoon": False, "evening": False, "night": True,
         "before_food": False, "duration_days": 5, "sort_order": 2},
        {"prescription_id": RX_SUJAI, "medicine_name": "ORS Sachet",        "dosage": "1 sachet in 200ml water",
         "morning": True,  "afternoon": True,  "evening": True,  "night": False,
         "before_food": False, "duration_days": 3, "sort_order": 3},

        # Dhanvanth — ORS M+A+E (3 days), Zinc M (14 days), Norflox-TZ M+N (3 days)
        {"prescription_id": RX_DHAN, "medicine_name": "ORS Sachet",     "dosage": "1 sachet in 200ml",
         "morning": True,  "afternoon": True,  "evening": True,  "night": False,
         "before_food": False, "duration_days": 3, "sort_order": 1},
        {"prescription_id": RX_DHAN, "medicine_name": "Zinc 20mg",      "dosage": "1 tablet",
         "morning": True,  "afternoon": False, "evening": False, "night": False,
         "before_food": False, "duration_days": 14, "sort_order": 2},
        {"prescription_id": RX_DHAN, "medicine_name": "Norflox-TZ",     "dosage": "1 tablet",
         "morning": True,  "afternoon": False, "evening": False, "night": True,
         "before_food": True,  "duration_days": 3, "sort_order": 3},

        # Poornima — Diclofenac M+N (5 days), Rabeprazole M (5 days)
        {"prescription_id": RX_POOR, "medicine_name": "Diclofenac 50mg",  "dosage": "1 tablet",
         "morning": True,  "afternoon": False, "evening": False, "night": True,
         "before_food": True,  "duration_days": 5, "sort_order": 1},
        {"prescription_id": RX_POOR, "medicine_name": "Rabeprazole 20mg", "dosage": "1 tablet",
         "morning": True,  "afternoon": False, "evening": False, "night": False,
         "before_food": True,  "duration_days": 5, "sort_order": 2},

        # Selvarani — Azithromycin M (5 days), Cetirizine N (5 days), Montelukast N (5 days)
        {"prescription_id": RX_SELV, "medicine_name": "Azithromycin 500mg", "dosage": "1 tablet",
         "morning": True,  "afternoon": False, "evening": False, "night": False,
         "before_food": False, "duration_days": 5, "sort_order": 1},
        {"prescription_id": RX_SELV, "medicine_name": "Cetirizine 10mg",   "dosage": "1 tablet",
         "morning": False, "afternoon": False, "evening": False, "night": True,
         "before_food": False, "duration_days": 5, "sort_order": 2},
        {"prescription_id": RX_SELV, "medicine_name": "Montelukast 10mg",  "dosage": "1 tablet",
         "morning": False, "afternoon": False, "evening": False, "night": True,
         "before_food": False, "duration_days": 5, "sort_order": 3},
    ]
    supabase.table("prescription_medicines").insert(rows).execute()
    print(f"   ✓ prescription_medicines ({len(rows)} rows)")


def seed_followups():
    rows = [
        # Dhanvanth — followup scheduled TODAY → triggers followup_whatsapp_job
        {
            "id": FU_DHAN,
            "patient_id": DHAN, "doctor_id": DOCTOR_ID, "visit_id": V_DHAN,
            "followup_day": 3, "scheduled_date": TODAY,
            "channel": "whatsapp", "call_status": "Pending",
            "created_at": ts(TODAY, "09:45:00"),
        },
        # Selvarani — followup 3 days from now (future, must NOT trigger today)
        {
            "id": FU_SELV,
            "patient_id": SELV, "doctor_id": DOCTOR_ID, "visit_id": V_SELV,
            "followup_day": 3, "scheduled_date": THREE_FWD,
            "channel": "whatsapp", "call_status": "Pending",
            "created_at": ts(TODAY, "09:00:00"),
        },
    ]
    supabase.table("followups").insert(rows).execute()
    print(f"   ✓ followups ({len(rows)} rows)")


def seed_queries():
    rows = [
        # Selvarani — PENDING query (doctor hasn't replied yet)
        {
            "id": Q_SELV_PENDING,
            "patient_id": SELV, "doctor_id": DOCTOR_ID, "visit_id": V_SELV,
            "question": "Doctor, my throat pain has reduced a little but I still have difficulty swallowing. Is this normal on day 1?",
            "question_source": "whatsapp",
            "status": "Pending",
            "created_at": ts(TODAY, "14:00:00"),
        },
        # Selvarani — CLOSED/ANSWERED query
        {
            "id": Q_SELV_CLOSED,
            "patient_id": SELV, "doctor_id": DOCTOR_ID, "visit_id": V_SELV,
            "question": "Can I take warm milk with honey for throat pain?",
            "question_source": "whatsapp",
            "reply": "Yes, warm milk with honey is very good for throat pain. Have it at night before sleep. Avoid cold milk.",
            "replied_by": DOCTOR_ID,
            "status": "Closed",
            "created_at": ts(TODAY, "11:00:00"),
            "replied_at": ts(TODAY, "12:30:00"),
        },
        # Sujaikumar — PENDING query (fever followup)
        {
            "id": Q_SUJAI_PENDING,
            "patient_id": SUJAI, "doctor_id": DOCTOR_ID, "visit_id": V_SUJAI,
            "question": "Fever came back at night to 39°C. I gave Paracetamol. Should I come in tomorrow or continue medicines?",
            "question_source": "whatsapp",
            "status": "Pending",
            "created_at": ts(TODAY, "21:00:00"),
        },
    ]
    supabase.table("queries").insert(rows).execute()
    print(f"   ✓ queries ({len(rows)} rows)")


def seed_reviews():
    rows = [
        # Subramaniam visited 7 days ago → Google review job sends link today
        {
            "patient_id": SUBR, "doctor_id": DOCTOR_ID, "visit_id": V_SUBR,
            "google_review_link_sent": False,
            "created_at": ts(SEVEN_AGO, "10:00:00"),
        },
    ]
    supabase.table("reviews").insert(rows).execute()
    print(f"   ✓ reviews ({len(rows)} rows)")


def seed_conversation_state():
    rows = [
        {"mobile": "919047099959", "state": "idle", "temp_data": {}, "updated_at": ts(TODAY, "08:00:00")},
        {"mobile": "919943941314", "state": "idle", "temp_data": {}, "updated_at": ts(TODAY, "08:00:00")},
        {"mobile": "919965553323", "state": "idle", "temp_data": {}, "updated_at": ts(TODAY, "08:00:00")},
        {"mobile": "919047099979", "state": "idle", "temp_data": {}, "updated_at": ts(TODAY, "08:00:00")},
    ]
    supabase.table("conversation_state").upsert(rows, on_conflict="mobile").execute()
    print(f"   ✓ conversation_state ({len(rows)} rows, all idle)")


# ── Runner ──────────────────────────────────────────────────────────────

def main():
    print(f"\n{'='*65}")
    print(f"  PRA Test Data Seed")
    print(f"  Doctor : Dr. Kumar  ({DOCTOR_ID[:8]}...)")
    print(f"  Today  : {TODAY}")
    print(f"{'='*65}\n")

    wipe_all()
    print()
    print("🌱 Seeding 5 patients + all test data...")

    seed_patients()
    seed_appointments()
    seed_tokens()
    seed_visits()
    seed_prescriptions()
    seed_prescription_medicines()
    seed_followups()
    seed_queries()
    seed_reviews()
    seed_conversation_state()

    print()
    print("✅ Seed complete!\n")
    print("📋 Patients & flows:")
    print()
    print(f"  👨 Sujaikumar  (SUJ-9959-1983)  +91 90470 99959  — token 1 → Done")
    print(f"     🌅 morning reminder  : Paracetamol M+A+N, Cetirizine N, ORS M+A+E")
    print(f"     🌙 evening reminder  : Paracetamol N + Cetirizine N")
    print(f"     💬 pending query     : fever came back at night")
    print()
    print(f"  👦 Dhanvanth   (DHA-9959-2013)  +91 90470 99959  — token 2 → In Progress (family son)")
    print(f"     💬 followup WhatsApp : scheduled today → triggers /trigger/followup-whatsapp")
    print(f"     💊 medicines         : ORS M+A+E, Zinc M, Norflox-TZ M+N")
    print()
    print(f"  👩 Poornima    (POO-1314-1983)  +91 99439 41314  — token 4 → Waiting")
    print(f"     📞 voice call        : WA followup sent, no reply → /trigger/followup-calls")
    print()
    print(f"  👴 Subramaniam (SUB-3323-1955)  +91 99655 53323  — (visited {SEVEN_AGO})")
    print(f"     ⭐ Google review     : visit 7 days ago → /trigger/review-requests")
    print()
    print(f"  👩 Selvarani   (SEL-9979-1965)  +91 90470 99979  — token 3 → Waiting")
    print(f"     🏥 visit summary     : visited today → /trigger/visit-summary")
    print(f"     💬 pending query     : throat pain day 1")
    print(f"     ✅ answered query    : warm milk with honey?")
    print()
    print(f"📱 WhatsApp tests:")
    print(f"   Send to +91 90470 99959 → MENU → family selector shows Sujaikumar + Dhanvanth")
    print(f"   Send 7 → ask doctor → pick patient → type question")
    print(f"   Send 2 → token check → 'Token 1 is being seen now'")
    print()
    print(f"🚀 Trigger jobs:")
    print(f"   curl -X POST https://web-production-e5f38.up.railway.app/trigger/morning-reminders")
    print(f"   curl -X POST https://web-production-e5f38.up.railway.app/trigger/evening-reminders")
    print(f"   curl -X POST https://web-production-e5f38.up.railway.app/trigger/visit-summary")
    print(f"   curl -X POST https://web-production-e5f38.up.railway.app/trigger/followup-whatsapp")
    print(f"   curl -X POST https://web-production-e5f38.up.railway.app/trigger/followup-calls")
    print(f"   curl -X POST https://web-production-e5f38.up.railway.app/trigger/review-requests")
    print(f"\n{'='*65}\n")


if __name__ == "__main__":
    confirm = input("⚠️  This will DELETE ALL patient data and reseed. Continue? [y/N] ")
    if confirm.strip().lower() != "y":
        print("Aborted.")
        sys.exit(0)
    main()

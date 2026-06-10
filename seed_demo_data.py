"""
Full cleanup + reseed for Dr. Kumar Child Care Clinic.
Run: python3 seed_demo_data.py

Patients:
  Sujaikumar     M 43  05-Jun-1983  9047099959
  Dhanvanth      M 13  03-Apr-2013  9047099959  (Sujaikumar's child)
  Poornima       F 43  02-May-1983  9943941314
  Subramaniam K  M 71  31-Jan-1955  9965553323
  Selvarani S    F 61  20-Apr-1965  9047099979
"""

import os
from datetime import date, timedelta
from supabase import create_client
from dotenv import load_dotenv

load_dotenv()
sb = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_KEY"))

DOCTOR_ID = "8c33abe0-5d2e-4613-9437-c7c375e8d162"

TODAY    = date(2026, 6, 8)
D_MINUS1 = (TODAY - timedelta(days=1)).isoformat()  # Jun 7
D_MINUS2 = (TODAY - timedelta(days=2)).isoformat()  # Jun 6
D_MINUS3 = (TODAY - timedelta(days=3)).isoformat()  # Jun 5
D_MINUS4 = (TODAY - timedelta(days=4)).isoformat()  # Jun 4
D_MINUS5 = (TODAY - timedelta(days=5)).isoformat()  # Jun 3
TODAY_S  = TODAY.isoformat()

results = {}

# ── helpers ──────────────────────────────────────────────────────────────────

def ok(section, n): results[section] = results.get(section, {"ok": 0, "fail": 0}); results[section]["ok"] += n
def fail(section, e):
    results[section] = results.get(section, {"ok": 0, "fail": 0}); results[section]["fail"] += 1
    msg = e.args[0] if e.args else str(e)
    if isinstance(msg, dict): msg = msg.get("message", str(msg))
    print(f"  ❌ {section}: {msg}")

def insert_one(table, row, section):
    try:
        r = sb.table(table).insert(row).execute()
        ok(section, 1)
        return r.data[0] if r.data else None
    except Exception as e:
        fail(section, e)
        return None

def insert_many(table, rows, section):
    out = []
    for row in rows:
        r = insert_one(table, row, section)
        if r: out.append(r)
    return out

# ── 0. Clean up all seeded data ───────────────────────────────────────────────

print("\n=== 0. Cleaning up existing seeded data ===")

# Delete in FK-safe order
def delete_all(table, key="doctor_id"):
    try:
        sb.table(table).delete().eq(key, DOCTOR_ID).execute()
        print(f"  🗑  {table} cleared")
    except Exception as e:
        # prescription_medicines has no doctor_id — delete via prescription IDs
        print(f"  ⚠️  {table}: {e.args[0] if e.args else e}")

# Get prescription IDs first for cascade
prx_ids = [p["id"] for p in (sb.table("prescriptions").select("id").eq("doctor_id", DOCTOR_ID).execute().data or [])]
if prx_ids:
    sb.table("prescription_medicines").delete().in_("prescription_id", prx_ids).execute()
    print("  🗑  prescription_medicines cleared")

for tbl in ["reviews", "queries", "followups", "prescriptions", "visits", "tokens", "appointments"]:
    delete_all(tbl)

# Remove the extra Poornima patient entry (Dhanvanth was incorrectly assigned her mobile)
# Keep: Sujaikumar (a897...) and Dhanvanth (6b30...)
# Remove any others and re-create clean

# ── 1. Patients — upsert all 5 ───────────────────────────────────────────────

print("\n=== 1. Patients ===")

patients_data = [
    # (id,                                  name,           dob,           gender, age, mobile,       patient_code,   fhm)
    ("a897e8af-749a-4846-a39e-053eb43ab55d","Sujaikumar",   "1983-06-05",  "Male",  43,  "919047099959","SUJ-9959-1983","919047099959"),
    ("6b30e1d3-debe-428c-965b-39b5a11b52a5","Dhanvanth",    "2013-04-03",  "Male",  13,  "919047099959","DHA-9959-2013","919047099959"),
    ("a10ed600-4e79-4ff9-9545-560f7b842451","Poornima",     "1983-05-02",  "Female",43,  "919943941314","POO-1314-1983","919943941314"),
]

# Insert new patients for Subramaniam and Selvarani
new_patients = [
    {"name":"Subramaniam K","date_of_birth":"1955-01-31","gender":"Male",  "age":71,"mobile":"919965553323","whatsapp_number":"919965553323","patient_code":"SUB-3323-1955","family_head_mobile":"919965553323","registration_source":"walk-in"},
    {"name":"Selvarani S",  "date_of_birth":"1965-04-20","gender":"Female","age":61,"mobile":"919047099979","whatsapp_number":"919047099979","patient_code":"SEL-9979-1965","family_head_mobile":"919047099979","registration_source":"walk-in"},
]

# Upsert existing 3
for pid, name, dob, gender, age, mobile, code, fhm in patients_data:
    try:
        sb.table("patients").update({
            "name": name, "date_of_birth": dob, "gender": gender,
            "age": age, "mobile": mobile, "whatsapp_number": mobile,
            "patient_code": code, "family_head_mobile": fhm,
        }).eq("id", pid).execute()
        ok("patients", 1)
        print(f"  ✅ Updated: {name}")
    except Exception as e:
        fail("patients", e)

# Remove old Poornima duplicate if mobile was wrong
try:
    sb.table("patients").update({"mobile": "919943941314", "whatsapp_number": "919943941314", "family_head_mobile": "919943941314"}).eq("id", "a10ed600-4e79-4ff9-9545-560f7b842451").execute()
except Exception: pass

# Check if Subramaniam/Selvarani already exist
existing_mobiles = {p["mobile"] for p in (sb.table("patients").select("mobile").execute().data or [])}

sub_id, sel_id = None, None
for np in new_patients:
    if np["mobile"] in existing_mobiles:
        r = sb.table("patients").select("id").eq("mobile", np["mobile"]).execute()
        pid = r.data[0]["id"] if r.data else None
        print(f"  ℹ️  {np['name']} already exists: {pid}")
        if np["name"].startswith("Sub"): sub_id = pid
        else: sel_id = pid
        ok("patients", 1)
    else:
        r = insert_one("patients", np, "patients")
        if r:
            if np["name"].startswith("Sub"): sub_id = r["id"]
            else: sel_id = r["id"]
            print(f"  ✅ Created: {np['name']} → {r['id']}")

# Fetch all 5 patient IDs
all_patients = sb.table("patients").select("id,name,mobile").execute().data or []
pid_map = {p["name"]: p["id"] for p in all_patients}

SUJAI_ID  = "a897e8af-749a-4846-a39e-053eb43ab55d"
DHANV_ID  = "6b30e1d3-debe-428c-965b-39b5a11b52a5"
POOR_ID   = "a10ed600-4e79-4ff9-9545-560f7b842451"
SUB_ID    = sub_id or pid_map.get("Subramaniam K")
SEL_ID    = sel_id or pid_map.get("Selvarani S")

print(f"\n  Patient IDs resolved:")
print(f"    Sujaikumar   : {SUJAI_ID}")
print(f"    Dhanvanth    : {DHANV_ID}")
print(f"    Poornima     : {POOR_ID}")
print(f"    Subramaniam K: {SUB_ID}")
print(f"    Selvarani S  : {SEL_ID}")

if not all([SUJAI_ID, DHANV_ID, POOR_ID, SUB_ID, SEL_ID]):
    print("\n❌ Could not resolve all patient IDs. Aborting.")
    import sys; sys.exit(1)

# ── 2. Appointments ───────────────────────────────────────────────────────────
# 18 appointments spread over 6 days (3/day historical + today's 5)

print("\n=== 2. Appointments ===")

appt_rows = [
    # Jun 3 (5 days ago) — 3 appts
    {"doctor_id": DOCTOR_ID, "patient_id": SUJAI_ID,  "appointment_date": D_MINUS5, "appointment_time": "09:00:00", "token_number": 1, "status": "Confirmed", "booking_source": "walk-in"},
    {"doctor_id": DOCTOR_ID, "patient_id": SEL_ID,    "appointment_date": D_MINUS5, "appointment_time": "09:30:00", "token_number": 2, "status": "Confirmed", "booking_source": "whatsapp"},
    {"doctor_id": DOCTOR_ID, "patient_id": SUB_ID,    "appointment_date": D_MINUS5, "appointment_time": "10:00:00", "token_number": 3, "status": "Confirmed", "booking_source": "walk-in"},
    # Jun 4
    {"doctor_id": DOCTOR_ID, "patient_id": POOR_ID,   "appointment_date": D_MINUS4, "appointment_time": "09:00:00", "token_number": 1, "status": "Confirmed", "booking_source": "walk-in"},
    {"doctor_id": DOCTOR_ID, "patient_id": DHANV_ID,  "appointment_date": D_MINUS4, "appointment_time": "09:30:00", "token_number": 2, "status": "Confirmed", "booking_source": "whatsapp"},
    {"doctor_id": DOCTOR_ID, "patient_id": SEL_ID,    "appointment_date": D_MINUS4, "appointment_time": "10:00:00", "token_number": 3, "status": "Confirmed", "booking_source": "walk-in"},
    # Jun 5
    {"doctor_id": DOCTOR_ID, "patient_id": SUJAI_ID,  "appointment_date": D_MINUS3, "appointment_time": "09:00:00", "token_number": 1, "status": "Confirmed", "booking_source": "whatsapp"},
    {"doctor_id": DOCTOR_ID, "patient_id": SUB_ID,    "appointment_date": D_MINUS3, "appointment_time": "09:30:00", "token_number": 2, "status": "Confirmed", "booking_source": "walk-in"},
    {"doctor_id": DOCTOR_ID, "patient_id": POOR_ID,   "appointment_date": D_MINUS3, "appointment_time": "10:00:00", "token_number": 3, "status": "Confirmed", "booking_source": "walk-in"},
    # Jun 6
    {"doctor_id": DOCTOR_ID, "patient_id": DHANV_ID,  "appointment_date": D_MINUS2, "appointment_time": "09:00:00", "token_number": 1, "status": "Confirmed", "booking_source": "walk-in"},
    {"doctor_id": DOCTOR_ID, "patient_id": SEL_ID,    "appointment_date": D_MINUS2, "appointment_time": "09:30:00", "token_number": 2, "status": "Confirmed", "booking_source": "walk-in"},
    # Jun 7
    {"doctor_id": DOCTOR_ID, "patient_id": SUB_ID,    "appointment_date": D_MINUS1, "appointment_time": "09:00:00", "token_number": 1, "status": "Confirmed", "booking_source": "walk-in"},
    {"doctor_id": DOCTOR_ID, "patient_id": POOR_ID,   "appointment_date": D_MINUS1, "appointment_time": "09:30:00", "token_number": 2, "status": "Confirmed", "booking_source": "whatsapp"},
    {"doctor_id": DOCTOR_ID, "patient_id": SUJAI_ID,  "appointment_date": D_MINUS1, "appointment_time": "10:00:00", "token_number": 3, "status": "Cancelled",  "booking_source": "walk-in", "cancellation_reason": "Patient called to reschedule"},
    # Today (Jun 8) — 5 in queue, token 2 currently being served
    {"doctor_id": DOCTOR_ID, "patient_id": SUJAI_ID,  "appointment_date": TODAY_S, "appointment_time": "09:00:00", "token_number": 1, "status": "Confirmed", "booking_source": "walk-in"},
    {"doctor_id": DOCTOR_ID, "patient_id": DHANV_ID,  "appointment_date": TODAY_S, "appointment_time": "09:30:00", "token_number": 2, "status": "Confirmed", "booking_source": "walk-in"},
    {"doctor_id": DOCTOR_ID, "patient_id": POOR_ID,   "appointment_date": TODAY_S, "appointment_time": "10:00:00", "token_number": 3, "status": "Confirmed", "booking_source": "whatsapp"},
    {"doctor_id": DOCTOR_ID, "patient_id": SUB_ID,    "appointment_date": TODAY_S, "appointment_time": "10:30:00", "token_number": 4, "status": "Confirmed", "booking_source": "walk-in"},
    {"doctor_id": DOCTOR_ID, "patient_id": SEL_ID,    "appointment_date": TODAY_S, "appointment_time": "11:00:00", "token_number": 5, "status": "Confirmed", "booking_source": "walk-in"},
]
inserted_appts = insert_many("appointments", appt_rows, "appointments")
print(f"  → {len(inserted_appts)} appointments created")

# Fetch IDs by date for visits
def get_appts(d):
    return sb.table("appointments").select("id,patient_id,token_number").eq("doctor_id", DOCTOR_ID).eq("appointment_date", d).order("token_number").execute().data or []

appts_d5 = get_appts(D_MINUS5)
appts_d4 = get_appts(D_MINUS4)
appts_d3 = get_appts(D_MINUS3)
appts_d2 = get_appts(D_MINUS2)
appts_d1 = get_appts(D_MINUS1)

# ── 3. Tokens — current queue (token 2 being served) ─────────────────────────

print("\n=== 3. Tokens ===")
insert_one("tokens", {
    "doctor_id": DOCTOR_ID, "queue_date": TODAY_S,
    "current_token": 2, "total_tokens": 5, "is_active": True, "avg_minutes_per_patient": 12,
}, "tokens")

# ── 4. Visits ─────────────────────────────────────────────────────────────────

print("\n=== 4. Visits ===")

# Map: (date, patient_id) → appointment_id
def appt_id(appts, pid):
    for a in appts:
        if a["patient_id"] == pid: return a["id"]
    return None

visit_defs = [
    # Jun 3
    (D_MINUS5, SUJAI_ID, appt_id(appts_d5, SUJAI_ID), "Viral Fever",                  "Fever 38.5°C, body ache, no cough", "Rest, paracetamol, fluids"),
    (D_MINUS5, SEL_ID,   appt_id(appts_d5, SEL_ID),   "Hypertension Review",           "BP 148/92, mild headache",           "Medication adjusted"),
    (D_MINUS5, SUB_ID,   appt_id(appts_d5, SUB_ID),   "Type 2 Diabetes Follow-up",     "FBS 142, HbA1c 7.2%",               "Continue medication, diet counselled"),
    # Jun 4
    (D_MINUS4, POOR_ID,  appt_id(appts_d4, POOR_ID),  "Allergic Rhinitis",             "Sneezing, watery eyes, nasal block", "Antihistamine prescribed"),
    (D_MINUS4, DHANV_ID, appt_id(appts_d4, DHANV_ID), "Acute Gastroenteritis",         "Loose stools x4, vomiting x2",       "ORS, probiotics, light diet"),
    (D_MINUS4, SEL_ID,   appt_id(appts_d4, SEL_ID),   "Knee Pain (Osteoarthritis)",    "Bilateral knee pain, stiffness AM",  "Physiotherapy referral + NSAIDs"),
    # Jun 5
    (D_MINUS3, SUJAI_ID, appt_id(appts_d3, SUJAI_ID), "Upper Respiratory Infection",   "Sore throat, mild fever 37.8°C",     "Antibiotics + lozenges"),
    (D_MINUS3, SUB_ID,   appt_id(appts_d3, SUB_ID),   "Chest Pain Evaluation",         "Exertional chest tightness",         "ECG done, referral to cardiologist"),
    (D_MINUS3, POOR_ID,  appt_id(appts_d3, POOR_ID),  "Migraine",                      "Throbbing headache, photophobia",    "Sumatriptan prescribed, rest"),
    # Jun 6
    (D_MINUS2, DHANV_ID, appt_id(appts_d2, DHANV_ID), "Follow-up Gastroenteritis",     "Stools normalised, afebrile",        "Recovered, no further meds"),
    (D_MINUS2, SEL_ID,   appt_id(appts_d2, SEL_ID),   "Shoulder Pain",                 "Right shoulder restricted movement", "Physiotherapy + analgesic gel"),
    # Jun 7
    (D_MINUS1, SUB_ID,   appt_id(appts_d1, SUB_ID),   "Diabetes + BP Combo Review",    "FBS 128, BP 138/86 – improving",     "Dose adjusted, continue lifestyle changes"),
    (D_MINUS1, POOR_ID,  appt_id(appts_d1, POOR_ID),  "Allergic Rhinitis Follow-up",   "Symptoms 60% better",               "Continue antihistamine 1 more week"),
]

visit_ids = {}  # patient_id → latest visit_id
for vdate, pid, aid, diag, complaint, notes in visit_defs:
    if not aid: continue
    r = insert_one("visits", {
        "doctor_id": DOCTOR_ID, "patient_id": pid, "appointment_id": aid,
        "visit_date": vdate, "diagnosis": diag,
        "chief_complaint": complaint, "notes": notes,
    }, "visits")
    if r: visit_ids[pid] = r["id"]  # keeps latest

print(f"  → {len(visit_ids)} unique patients have visits")

# ── 5. Prescriptions ─────────────────────────────────────────────────────────

print("\n=== 5. Prescriptions ===")

prx_defs = [
    (SUJAI_ID, visit_ids.get(SUJAI_ID), D_MINUS3, "Take full course. Drink warm fluids.", "Avoid cold drinks"),
    (DHANV_ID, visit_ids.get(DHANV_ID), D_MINUS4, "ORS after every loose stool. Soft diet.", "Avoid dairy for 3 days"),
    (POOR_ID,  visit_ids.get(POOR_ID),  D_MINUS1, "Take antihistamine at bedtime.", "Avoid dust and pollen"),
    (SUB_ID,   visit_ids.get(SUB_ID),   D_MINUS1, "Monitor BP daily. Low-salt diet.", "Avoid exertion till cardiology review"),
    (SEL_ID,   visit_ids.get(SEL_ID),   D_MINUS2, "Apply gel 3x/day. Ice pack 15min after physio.", "Avoid heavy lifting"),
]

prx_ids = {}
for pid, vid, pdate, notes, precautions in prx_defs:
    if not vid: continue
    r = insert_one("prescriptions", {
        "doctor_id": DOCTOR_ID, "patient_id": pid, "visit_id": vid,
        "prescription_date": pdate, "general_notes": notes, "precautions": precautions,
    }, "prescriptions")
    if r: prx_ids[pid] = r["id"]

# ── 6. Prescription medicines ─────────────────────────────────────────────────

print("\n=== 6. Prescription medicines ===")

med_defs = {
    SUJAI_ID: [
        {"medicine_name": "Azithromycin 500mg", "dosage": "1 tablet", "morning": True,  "afternoon": False, "evening": False, "night": False, "duration_days": 5,  "instructions": "Take on empty stomach"},
        {"medicine_name": "Paracetamol 650mg",  "dosage": "1 tablet", "morning": True,  "afternoon": True,  "evening": False, "night": True,  "duration_days": 3,  "instructions": "Take after food, only if fever"},
        {"medicine_name": "Cetirizine 10mg",    "dosage": "1 tablet", "morning": False, "afternoon": False, "evening": False, "night": True,  "duration_days": 5,  "instructions": "Avoid driving"},
    ],
    DHANV_ID: [
        {"medicine_name": "ORS Sachet",         "dosage": "1 sachet", "morning": True,  "afternoon": True,  "evening": True,  "night": False, "duration_days": 3,  "instructions": "Mix in 200ml water"},
        {"medicine_name": "Zinc 20mg",          "dosage": "1 tablet", "morning": True,  "afternoon": False, "evening": False, "night": False, "duration_days": 14, "instructions": "After food"},
        {"medicine_name": "Lacto-B Probiotic",  "dosage": "1 sachet", "morning": True,  "afternoon": False, "evening": False, "night": True,  "duration_days": 5,  "instructions": "Mix in cool water"},
    ],
    POOR_ID: [
        {"medicine_name": "Levocetirizine 5mg", "dosage": "1 tablet", "morning": False, "afternoon": False, "evening": False, "night": True,  "duration_days": 7,  "instructions": "Take at bedtime"},
        {"medicine_name": "Fluticasone Nasal Spray","dosage":"2 puffs","morning":True,   "afternoon": False, "evening": False, "night": False, "duration_days": 10, "instructions": "Each nostril, tilt head slightly"},
    ],
    SUB_ID: [
        {"medicine_name": "Metformin 500mg",    "dosage": "1 tablet", "morning": True,  "afternoon": True,  "evening": False, "night": False, "duration_days": 30, "instructions": "After food"},
        {"medicine_name": "Amlodipine 5mg",     "dosage": "1 tablet", "morning": True,  "afternoon": False, "evening": False, "night": False, "duration_days": 30, "instructions": "Same time daily"},
        {"medicine_name": "Aspirin 75mg",       "dosage": "1 tablet", "morning": False, "afternoon": False, "evening": False, "night": True,  "duration_days": 30, "instructions": "After food"},
    ],
    SEL_ID: [
        {"medicine_name": "Diclofenac Gel 1%",  "dosage": "Apply", "morning": True, "afternoon": True, "evening": True, "night": False, "duration_days": 7, "instructions": "Massage gently, avoid open wounds"},
        {"medicine_name": "Calcium + Vit D3",   "dosage": "1 tablet","morning": True,"afternoon": False,"evening": False,"night": False, "duration_days": 30,"instructions": "After breakfast"},
    ],
}

for pid, meds in med_defs.items():
    prx_id = prx_ids.get(pid)
    if not prx_id: continue
    for i, med in enumerate(meds, 1):
        insert_one("prescription_medicines", {**med, "prescription_id": prx_id, "sort_order": i}, "prescription_medicines")

# ── 7. Follow-ups ─────────────────────────────────────────────────────────────

print("\n=== 7. Follow-ups ===")

followup_defs = [
    # Pending (completed_at = NULL)
    (SUJAI_ID, visit_ids.get(SUJAI_ID), D_MINUS3, "Pending", None, None),
    (DHANV_ID, visit_ids.get(DHANV_ID), D_MINUS4, "Pending", None, None),
    (POOR_ID,  visit_ids.get(POOR_ID),  D_MINUS1, "Pending", None, None),
    # Completed
    (SEL_ID,   visit_ids.get(SEL_ID),   D_MINUS5, "Completed", f"{D_MINUS3}T11:00:00+00:00", "Knee feeling much better after physio sessions"),
    (SUB_ID,   visit_ids.get(SUB_ID),   D_MINUS3, "Completed", f"{D_MINUS1}T10:00:00+00:00", "BP under control, no chest pain since last visit"),
]

for pid, vid, sched, status, completed_at, notes in followup_defs:
    row = {
        "doctor_id": DOCTOR_ID, "patient_id": pid, "visit_id": vid,
        "scheduled_date": sched, "channel": "whatsapp",
        "call_status": status, "completed_at": completed_at,
    }
    if notes: row["response_notes"] = notes
    insert_one("followups", row, "followups")

# ── 8. Queries ────────────────────────────────────────────────────────────────

print("\n=== 8. Queries ===")

query_defs = [
    # Pending
    (SUJAI_ID, visit_ids.get(SUJAI_ID), "Doctor, can I drink coconut water during fever? Is it safe?", "Pending"),
    (DHANV_ID, visit_ids.get(DHANV_ID), "My son is still passing loose stools on day 3. Should I bring him in?", "Pending"),
    (POOR_ID,  visit_ids.get(POOR_ID),  "Is it okay to take the nasal spray along with the Levocetirizine tablet?", "Pending"),
    # Closed (with reply)
    (SEL_ID,   visit_ids.get(SEL_ID),   "Can I continue walking exercise with my knee pain?", "Closed"),
    (SUB_ID,   visit_ids.get(SUB_ID),   "My BP reading at home is 135/85. Is this normal?", "Closed"),
]

for pid, vid, question, status in query_defs:
    row = {
        "doctor_id": DOCTOR_ID, "patient_id": pid, "visit_id": vid,
        "question": question, "question_source": "whatsapp",
        "status": status, "priority": "Normal",
    }
    if status == "Closed":
        row["reply"] = "Yes, that is within acceptable range. Continue medication and monitor daily." if "BP" in question else "Light walking on flat surfaces is fine. Avoid stairs and inclines until physio clears you."
        row["replied_by"] = DOCTOR_ID
    insert_one("queries", row, "queries")

# ── 9. Reviews ────────────────────────────────────────────────────────────────

print("\n=== 9. Reviews ===")

review_defs = [
    (SUJAI_ID, visit_ids.get(SUJAI_ID), 5, "Very caring and attentive. Diagnosed my son's fever quickly and the medicines worked within 2 days."),
    (DHANV_ID, visit_ids.get(DHANV_ID), 5, "Doctor was so patient with my child. Explained everything clearly to us. Highly recommend!"),
    (POOR_ID,  visit_ids.get(POOR_ID),  4, "Good consultation. The waiting time was a bit long but the doctor gave full attention."),
    (SEL_ID,   visit_ids.get(SEL_ID),   4, "Helpful advice on physiotherapy. Knee has improved a lot since the visit."),
    (SUB_ID,   visit_ids.get(SUB_ID),   5, "Dr. Kumar takes the time to understand elderly patients. Very thorough checkup."),
]

for pid, vid, rating, feedback in review_defs:
    insert_one("reviews", {
        "doctor_id": DOCTOR_ID, "patient_id": pid, "visit_id": vid,
        "rating": rating, "feedback": feedback,
    }, "reviews")

# ── Summary ───────────────────────────────────────────────────────────────────

print("\n=== Summary ===")
total_ok   = sum(v["ok"]   for v in results.values())
total_fail = sum(v["fail"] for v in results.values())
for section, counts in sorted(results.items()):
    icon = "✅" if counts["fail"] == 0 else "⚠️"
    print(f"  {icon} {section:<25} {counts['ok']} ok, {counts['fail']} failed")
print(f"\n  Total: {total_ok} inserted/updated, {total_fail} failed")
print("\n  Live queue: token 2 (Dhanvanth) currently being served, 3 waiting")

import os
from supabase import create_client
from datetime import date, timedelta
from collections import defaultdict

url = os.environ.get("SUPABASE_URL")
key = os.environ.get("SUPABASE_SERVICE_KEY")
supabase = create_client(url, key)

doctor_id = "8c33abe0-5d2e-4613-9437-c7c375e8d162"
today = date.today().isoformat()

print(f"Today: {today}")
print(f"Doctor ID: {doctor_id}")

# Test 1 - appointments count
try:
    r = supabase.table("appointments").select("id", count="exact").eq("doctor_id", doctor_id).eq("appointment_date", today).execute()
    print(f"✅ appointments count: {r.count}")
except Exception as e:
    print(f"❌ appointments count FAILED: {e}")

# Test 2 - tokens
try:
    r = supabase.table("tokens").select("current_token").eq("doctor_id", doctor_id).eq("appointment_date", today).execute()
    print(f"✅ tokens: {r.data}")
except Exception as e:
    print(f"❌ tokens FAILED: {e}")

# Test 3 - patients count
try:
    r = supabase.table("patients").select("id", count="exact").eq("doctor_id", doctor_id).execute()
    print(f"✅ patients count: {r.count}")
except Exception as e:
    print(f"❌ patients count FAILED: {e}")

# Test 4 - follow_ups
try:
    r = supabase.table("follow_ups").select("id", count="exact").eq("doctor_id", doctor_id).eq("status", "pending").execute()
    print(f"✅ follow_ups count: {r.count}")
except Exception as e:
    print(f"❌ follow_ups FAILED: {e}")

# Test 5 - prescriptions
try:
    r = supabase.table("prescriptions").select("diagnosis").eq("doctor_id", doctor_id).execute()
    print(f"✅ prescriptions: {len(r.data)} rows")
except Exception as e:
    print(f"❌ prescriptions FAILED: {e}")

# Test 6 - patients search
try:
    r = supabase.table("patients").select("*").eq("doctor_id", doctor_id).order("created_at", desc=True).execute()
    print(f"✅ patients list: {len(r.data)} rows")
    if r.data:
        print(f"   First patient keys: {list(r.data[0].keys())}")
except Exception as e:
    print(f"❌ patients list FAILED: {e}")

# Test 7 - queue status
try:
    r = supabase.table("appointments").select("*, patients(*)").eq("doctor_id", doctor_id).eq("appointment_date", today).order("token_number").execute()
    print(f"✅ queue appointments: {len(r.data)} rows")
except Exception as e:
    print(f"❌ queue appointments FAILED: {e}")

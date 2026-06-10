"""
Follow-up flow:
Day after prescription ends:
  8AM → WhatsApp message sent
  6PM → If no reply → Twilio Voice Call (Sarvam AI TTS)

Audio caching:
  Cache key = patient_id + language
  filename: followup_{patient_id}_{language}.wav
  Generate once per patient per language → reuse forever
"""
import os
import base64
import httpx
from datetime import date, datetime, timedelta
from supabase import create_client
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse, Gather
from dotenv import load_dotenv
from fastapi import Request
from fastapi.responses import PlainTextResponse

load_dotenv()
import config_loader

supabase = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_SERVICE_KEY")
)

twilio_client = Client(
    os.getenv("TWILIO_ACCOUNT_SID"),
    os.getenv("TWILIO_AUTH_TOKEN")
)

TWILIO_FROM = os.getenv("TWILIO_WHATSAPP_FROM")
TWILIO_VOICE_NUMBER = os.getenv("TWILIO_VOICE_NUMBER")
SARVAM_API_KEY = os.getenv("SARVAM_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
BASE_URL = os.getenv("BASE_URL", "https://web-production-e5f38.up.railway.app")
BUCKET_NAME = "clinic-audio"

# Language config for Sarvam AI
LANGUAGE_CONFIG = {
    "tamil": {
        "code": "ta-IN",
        "speaker": "kavitha",
        "script": (
            "வணக்கம் {name}! நான் {doctor} கிளினிக்கிலிருந்து பேசுகிறேன். "
            "உங்கள் மருந்து கோர்ஸ் முடிந்தது. "
            "நீங்கள் எப்படி உணர்கிறீர்கள்? "
            "நலமாக இருந்தால் ஒன்று அழுத்தவும். "
            "மீண்டும் மருத்துவரை சந்திக்க வேண்டும் என்றால் இரண்டு அழுத்தவும்."
        ),
        "whatsapp": (
            "வணக்கம் {name}! 🙏\n\n"
            "உங்கள் மருந்து கோர்ஸ் முடிந்தது.\n"
            "நீங்கள் எப்படி உணர்கிறீர்கள்?\n\n"
            "1. நலமாக இருக்கிறேன் 😊\n"
            "2. இன்னும் குணமாகவில்லை 🤒\n"
            "3. மீண்டும் மருத்துவரை சந்திக்க வேண்டும் 🏥\n\n"
            "- {clinic}"
        )
    },
    "hindi": {
        "code": "hi-IN",
        "speaker": "priya",
        "script": (
            "नमस्ते {name}! मैं {doctor} क्लिनिक से बोल रही हूं। "
            "आपका दवाई का कोर्स पूरा हो गया है। "
            "आप कैसा महसूस कर रहे हैं? "
            "अच्छा महसूस हो रहा है तो एक दबाएं। "
            "डॉक्टर से मिलना है तो दो दबाएं।"
        ),
        "whatsapp": (
            "नमस्ते {name}! 🙏\n\n"
            "आपका दवाई का कोर्स पूरा हो गया है।\n"
            "आप कैसा महसूस कर रहे हैं?\n\n"
            "1. बहुत बेहतर हूं 😊\n"
            "2. अभी भी ठीक नहीं हूं 🤒\n"
            "3. डॉक्टर से मिलना है 🏥\n\n"
            "- {clinic}"
        )
    },
    "english": {
        "code": "en-IN",
        "speaker": "ritu",
        "script": (
            "Hello {name}! This is a call from {doctor} Clinic. "
            "Your medicine course has been completed. "
            "How are you feeling today? "
            "Press one if you are feeling better. "
            "Press two if you would like to book an appointment."
        ),
        "whatsapp": (
            "Hello {name}! 🙏\n\n"
            "Your medicine course has been completed.\n"
            "How are you feeling today?\n\n"
            "1. Much better 😊\n"
            "2. Still recovering 🤒\n"
            "3. Need to see doctor again 🏥\n\n"
            "- {clinic}"
        )
    }
}


async def generate_sarvam_audio(text: str, language: str) -> bytes:
    """Generate audio using Sarvam AI Bulbul v3"""
    config = LANGUAGE_CONFIG.get(language, LANGUAGE_CONFIG["english"])

    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://api.sarvam.ai/text-to-speech",
            headers={
                "api-subscription-key": SARVAM_API_KEY,
                "Content-Type": "application/json"
            },
            json={
                "inputs": [text],
                "target_language_code": config["code"],
                "speaker": config["speaker"],
                "model": "bulbul:v3",
                "audio_format": "wav",
                "pace": 0.9,
                "enable_preprocessing": True
            },
            timeout=30.0
        )
        response.raise_for_status()
        data = response.json()
        audio_b64 = data["audios"][0]
        return base64.b64decode(audio_b64)


async def upload_audio(audio_bytes: bytes, file_path: str) -> str:
    """Upload audio to Supabase Storage and return public URL"""
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{SUPABASE_URL}/storage/v1/object/{BUCKET_NAME}/{file_path}",
            headers={
                "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                "Content-Type": "audio/wav",
                "x-upsert": "true"
            },
            content=audio_bytes,
            timeout=30.0
        )
        response.raise_for_status()

    public_url = f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET_NAME}/{file_path}"
    print(f"✅ Audio uploaded: {public_url}")
    return public_url


async def check_audio_exists(file_path: str) -> bool:
    """Check if audio file already exists in Supabase Storage"""
    async with httpx.AsyncClient() as client:
        response = await client.head(
            f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET_NAME}/{file_path}",
            timeout=10.0
        )
        return response.status_code == 200


async def get_or_generate_audio(patient_id: str, patient_name: str,
                                 doctor_name: str, language: str) -> str:
    """
    Get cached audio URL or generate new one.
    Cache key = patient_id + language
    Generates once per patient per language → reuses forever
    """
    file_path = f"followup_{patient_id}_{language}.wav"

    # Check if already cached
    exists = await check_audio_exists(file_path)
    if exists:
        print(f"✅ Using cached audio for {patient_name} ({language})")
        return f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET_NAME}/{file_path}"

    # Generate new audio
    print(f"🎙️ Generating {language} audio for {patient_name} (first time)...")
    config = LANGUAGE_CONFIG.get(language, LANGUAGE_CONFIG["english"])
    script = config["script"].format(
        name=patient_name,
        doctor=doctor_name
    )

    audio_bytes = await generate_sarvam_audio(script, language)
    audio_url = await upload_audio(audio_bytes, file_path)
    return audio_url


def get_prescriptions_ending_today():
    """
    Get prescriptions where course ended yesterday.
    followup_whatsapp_sent = false only.
    """
    yesterday = (date.today() - timedelta(days=1)).isoformat()

    result = supabase.table("prescriptions").select(
        "id, prescription_date, followup_whatsapp_sent, followup_replied, "
        "followup_call_sent, patient_id, doctor_id, "
        "patients(id, name, mobile, language), "
        "doctors(name, clinic_name), "
        "prescription_medicines(duration_days)"
    ).eq("followup_whatsapp_sent", False).execute()

    prescriptions = result.data or []
    due_prescriptions = []

    for pres in prescriptions:
        medicines = pres.get("prescription_medicines", [])
        if not medicines:
            continue

        pres_date_str = pres.get("prescription_date", "")
        if not pres_date_str:
            continue

        pres_date = datetime.strptime(pres_date_str, "%Y-%m-%d").date()
        max_duration = max(m.get("duration_days", 1) for m in medicines)
        course_end = pres_date + timedelta(days=max_duration - 1)

        if course_end.isoformat() == yesterday:
            due_prescriptions.append(pres)

    return due_prescriptions


def get_prescriptions_needing_call():
    """
    Prescriptions where WhatsApp sent but no reply and no call yet.
    """
    result = supabase.table("prescriptions").select(
        "id, patient_id, doctor_id, "
        "patients(id, name, mobile, language), "
        "doctors(name, clinic_name)"
    ).eq("followup_whatsapp_sent", True).eq(
        "followup_replied", False
    ).eq("followup_call_sent", False).execute()

    return result.data or []


def has_pending_followup(mobile: str) -> bool:
    """Check if patient has a pending follow-up reply"""
    # Try with family_head_mobile match first
    patient_result = supabase.table("patients").select(
        "id"
    ).eq("mobile", mobile).eq("family_head_mobile", mobile).execute()

    if not patient_result.data:
        # Fallback for patients without family_head_mobile
        patient_result = supabase.table("patients").select(
            "id"
        ).eq("mobile", mobile).is_("family_head_mobile", "null").execute()

    if not patient_result.data:
        return False

    patient_id = patient_result.data[0]["id"]

    result = supabase.table("prescriptions").select(
        "id"
    ).eq("patient_id", patient_id).eq(
        "followup_whatsapp_sent", True
    ).eq("followup_replied", False).execute()

    return len(result.data) > 0


def save_followup_reply(mobile: str, reply_text: str):
    """Save WhatsApp reply from patient"""
    reply_map = {
        "1": "feeling_better",
        "2": "still_recovering",
        "3": "needs_appointment"
    }
    response = reply_map.get(reply_text.strip(), reply_text)

    # Find patient
    patient_result = supabase.table("patients").select(
        "id"
    ).eq("mobile", mobile).eq("family_head_mobile", mobile).execute()

    if not patient_result.data:
        patient_result = supabase.table("patients").select(
            "id"
        ).eq("mobile", mobile).is_("family_head_mobile", "null").execute()

    if not patient_result.data:
        return

    patient_id = patient_result.data[0]["id"]

    pres_result = supabase.table("prescriptions").select(
        "id"
    ).eq("patient_id", patient_id).eq(
        "followup_whatsapp_sent", True
    ).eq("followup_replied", False).order(
        "created_at", desc=True
    ).limit(1).execute()

    if pres_result.data:
        pres_id = pres_result.data[0]["id"]
        supabase.table("prescriptions").update({
            "followup_replied": True,
            "followup_reply": response
        }).eq("id", pres_id).execute()
        print(f"✅ Follow-up reply saved: {mobile} → {response}")


async def send_followup_whatsapp(pres: dict):
    """Send follow-up WhatsApp message"""
    patient = pres.get("patients", {})
    doctor = pres.get("doctors", {})
    patient_name = patient.get("name", "Patient")
    mobile = patient.get("mobile", "")
    language = patient.get("language", "english")
    clinic_name = doctor.get("clinic_name", "Clinic")

    if not mobile:
        return

    config = LANGUAGE_CONFIG.get(language, LANGUAGE_CONFIG["english"])
    message = config["whatsapp"].format(
        name=patient_name,
        clinic=clinic_name
    )

    try:
        twilio_client.messages.create(
            from_=TWILIO_FROM,
            to=f"whatsapp:+{mobile}",
            body=message
        )

        supabase.table("prescriptions").update({
            "followup_whatsapp_sent": True,
            "followup_whatsapp_sent_at": datetime.now().isoformat()
        }).eq("id", pres["id"]).execute()

        print(f"✅ Follow-up WhatsApp sent to {patient_name} ({mobile})")

    except Exception as e:
        print(f"❌ Error sending follow-up WhatsApp: {e}")


async def make_followup_call(pres: dict):
    """Make Twilio voice call with cached Sarvam AI audio"""
    patient = pres.get("patients", {})
    doctor = pres.get("doctors", {})
    patient_name = patient.get("name", "Patient")
    patient_id = patient.get("id", "")
    mobile = patient.get("mobile", "")
    language = patient.get("language", "english")
    doctor_name = doctor.get("name", "Doctor")
    pres_id = pres["id"]

    if not mobile or not patient_id:
        return

    try:
        # Get or generate cached audio
        audio_url = await get_or_generate_audio(
            patient_id, patient_name, doctor_name, language
        )

        # Make Twilio call
        call = twilio_client.calls.create(
            from_=TWILIO_VOICE_NUMBER,
            to=f"+{mobile}",
            url=f"{BASE_URL}/webhook/voice/followup?pres_id={pres_id}&audio_url={audio_url}&lang={language}",
            timeout=30
        )

        # Mark call as sent
        supabase.table("prescriptions").update({
            "followup_call_sent": True,
            "followup_call_sent_at": datetime.now().isoformat()
        }).eq("id", pres_id).execute()

        print(f"✅ Follow-up call initiated to {patient_name} ({mobile}): {call.sid}")

    except Exception as e:
        print(f"❌ Error making follow-up call: {e}")
        import traceback
        traceback.print_exc()


def get_pending_followups():
    """
    Get followups from the followups table where:
    - scheduled_date = today (date comparison only)
    - call_status = 'Pending'
    """
    import pytz
    IST = pytz.timezone('Asia/Kolkata')
    today_ist = datetime.now(IST).date().isoformat()

    print(f"🔍 [get_pending_followups] Today (IST): {today_ist}")

    # Fetch all Pending rows due today or earlier (date-only comparison)
    result = supabase.table("followups").select(
        "id, scheduled_date, channel, call_status, patient_id, doctor_id, visit_id, "
        "patients(id, name, mobile, language), "
        "visits(diagnosis)"
    ).eq("call_status", "Pending").lte("scheduled_date", today_ist).execute()

    rows = result.data or []
    print(f"🔍 [get_pending_followups] Raw rows returned: {len(rows)}")
    for r in rows:
        print(f"    id={r['id']} scheduled_date={r['scheduled_date']} channel={r['channel']} call_status={r['call_status']}")

    return rows


def get_followups_needing_call():
    """
    Get followups where WhatsApp was already sent (call_status = 'Whatsapp-Sent')
    and scheduled_date <= today — these need a voice call now.
    """
    import pytz
    IST = pytz.timezone('Asia/Kolkata')
    today_ist = datetime.now(IST).date().isoformat()

    print(f"🔍 [get_followups_needing_call] Today (IST): {today_ist}")

    result = supabase.table("followups").select(
        "id, scheduled_date, channel, call_status, patient_id, doctor_id, visit_id, "
        "patients(id, name, mobile, language), "
        "visits(diagnosis)"
    ).eq("call_status", "Whatsapp-Sent").lte("scheduled_date", today_ist).execute()

    rows = result.data or []
    print(f"🔍 [get_followups_needing_call] Raw rows returned: {len(rows)}")
    for r in rows:
        print(f"    id={r['id']} scheduled_date={r['scheduled_date']} channel={r['channel']} call_status={r['call_status']}")

    return rows


async def send_followup_whatsapp_from_followups(followup: dict):
    """Send follow-up WhatsApp message based on followups table row"""
    patient = followup.get("patients") or {}
    visit = followup.get("visits") or {}
    patient_name = patient.get("name", "Patient")
    mobile = patient.get("mobile", "")
    language = patient.get("language", "english")
    diagnosis = visit.get("diagnosis", "")

    if not mobile:
        print(f"⚠️ No mobile for followup {followup['id']}")
        return

    # Use config_loader for clinic name (avoids extra DB query per followup)
    doctor_id = followup.get("doctor_id", "")
    clinic_name = config_loader.clinic_name(doctor_id) if doctor_id else config_loader.clinic_name()

    # Try DB template first, fall back to LANGUAGE_CONFIG hardcoded
    message = config_loader.get_template(
        "followup_whatsapp", language,
        {"name": patient_name, "clinic": clinic_name},
        doctor_id or config_loader.DOCTOR_ID
    )
    if not message:
        config = LANGUAGE_CONFIG.get(language, LANGUAGE_CONFIG["english"])
        message = config["whatsapp"].format(name=patient_name, clinic=clinic_name)

    try:
        twilio_client.messages.create(
            from_=TWILIO_FROM,
            to=f"whatsapp:+{mobile}",
            body=message
        )

        # Mark followup as Whatsapp-Sent so voice call job picks it up next
        supabase.table("followups").update({
            "call_status": "Whatsapp-Sent",
        }).eq("id", followup["id"]).execute()

        print(f"✅ Follow-up WhatsApp sent to {patient_name} ({mobile})")

    except Exception as e:
        print(f"❌ Error sending follow-up WhatsApp to {patient_name}: {e}")


async def make_followup_call_from_followup(followup: dict):
    """Make Twilio voice call for a followups-table row; marks call_status = 'Completed'."""
    patient = followup.get("patients") or {}
    patient_name = patient.get("name", "Patient")
    patient_id = patient.get("id", "")
    mobile = patient.get("mobile", "")
    language = patient.get("language", "english")
    followup_id = followup["id"]

    if not mobile or not patient_id:
        print(f"⚠️ Skipping followup {followup_id} — no mobile/patient")
        return

    # Fetch doctor name for audio generation
    doctor_id = followup.get("doctor_id", "")
    doctor_name = config_loader.doctor_name(doctor_id) if doctor_id else "Doctor"

    try:
        audio_url = await get_or_generate_audio(
            patient_id, patient_name, doctor_name, language
        )

        call = twilio_client.calls.create(
            from_=TWILIO_VOICE_NUMBER,
            to=f"+{mobile}",
            url=f"{BASE_URL}/webhook/voice/followup?followup_id={followup_id}&audio_url={audio_url}&lang={language}",
            timeout=30
        )

        # Mark as triggered — webhook will set 'Completed' if patient responds
        supabase.table("followups").update({
            "call_status": "Call-Triggered",
        }).eq("id", followup_id).execute()

        print(f"✅ Follow-up call initiated to {patient_name} ({mobile}): {call.sid}")

    except Exception as e:
        print(f"❌ Error making follow-up call to {patient_name}: {e}")
        import traceback
        traceback.print_exc()


async def send_followup_whatsapp_job():
    """8AM Job: Send follow-up WhatsApp from followups table"""
    print("💬 Running: Follow-up WhatsApp Job")
    followups = get_pending_followups()
    print(f"Found {len(followups)} pending followups")
    for f in followups:
        await send_followup_whatsapp_from_followups(f)


async def make_followup_calls_job():
    """6PM Job: Call patients whose WhatsApp was sent (call_status = 'Whatsapp-Sent')"""
    print("📞 Running: Follow-up Voice Call Job")
    followups = get_followups_needing_call()
    print(f"Found {len(followups)} patients needing follow-up call")
    for followup in followups:
        await make_followup_call_from_followup(followup)


async def handle_voice_followup_webhook(request: Request):
    """Twilio Voice webhook — plays cached audio and captures keypress"""
    params = dict(request.query_params)
    followup_id = params.get("followup_id", "")
    audio_url = params.get("audio_url", "")
    lang = params.get("lang", "english")

    response = VoiceResponse()
    gather = Gather(
        num_digits=1,
        action=f"{BASE_URL}/webhook/voice/followup-response?followup_id={followup_id}&lang={lang}",
        method="POST",
        timeout=10
    )

    if audio_url:
        gather.play(audio_url)
    else:
        gather.say(
            "Hello! This is a follow up call from your clinic. "
            "Press 1 if you are feeling better. Press 2 to book appointment.",
            language="en-IN"
        )

    response.append(gather)
    response.say("We did not receive your input. Please reply on WhatsApp. Thank you.")

    return PlainTextResponse(str(response), media_type="application/xml")


# Pre-generated response scripts - 6 files total, cached forever
RESPONSE_SCRIPTS = {
    "tamil": {
        "1": "மிக்க மகிழ்ச்சி! நீங்கள் நலமாக இருக்கிறீர்கள் என்று தெரிந்து மகிழ்ச்சி. நன்றி. வணக்கம்!",
        "2": "சரி! உங்கள் WhatsApp-ல் appointment book பண்ண message வரும். நன்றி. வணக்கம்!"
    },
    "hindi": {
        "1": "बहुत अच्छा! हमें खुशी है कि आप बेहतर हैं। धन्यवाद। नमस्ते!",
        "2": "ठीक है! आपके WhatsApp पर appointment book करने का message आएगा। धन्यवाद। नमस्ते!"
    },
    "english": {
        "1": "Wonderful! We are glad you are feeling better. Stay healthy. Goodbye!",
        "2": "Sure! We will send you a WhatsApp message to book your appointment. Thank you. Goodbye!"
    }
}


async def get_or_generate_response_audio(digit: str, language: str) -> str:
    """
    Get cached response audio or generate new one.
    Only 6 files total - generated once, reused forever.
    filename: response_{digit}_{language}.wav
    """
    file_path = f"response_{digit}_{language}.wav"
    exists = await check_audio_exists(file_path)
    if exists:
        print(f"✅ Using cached response: {file_path}")
        return f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET_NAME}/{file_path}"

    print(f"🎙️ Generating response audio: {file_path} (first time)...")
    scripts = RESPONSE_SCRIPTS.get(language, RESPONSE_SCRIPTS["english"])
    script = scripts.get(digit, "Thank you. Goodbye!")
    audio_bytes = await generate_sarvam_audio(script, language)
    audio_url = await upload_audio(audio_bytes, file_path)
    return audio_url


async def handle_voice_followup_response(request: Request):
    """Handle patient keypress - plays Sarvam cached response audio"""
    params = dict(request.query_params)
    followup_id = params.get("followup_id", "")
    lang = params.get("lang", "english")
    form_data = await request.form()
    digit = form_data.get("Digits", "")

    response_map = {
        "1": "feeling_better",
        "2": "needs_appointment",
        "3": "still_recovering",
    }
    followup_response = response_map.get(digit, "no_response")

    # Mark followup Completed for all valid responses
    if followup_id:
        supabase.table("followups").update({
            "call_status": "Completed",
            "completed_at": datetime.now().isoformat(),
            "response_notes": followup_response,
        }).eq("id", followup_id).execute()

    # digit=2 only: send appointment booking WhatsApp
    if digit == "2" and followup_id:
        try:
            fu_result = supabase.table("followups").select(
                "patient_id, doctor_id, patients(name, mobile, language)"
            ).eq("id", followup_id).execute()

            if fu_result.data:
                patient = (fu_result.data[0].get("patients") or {})
                doctor_id = fu_result.data[0].get("doctor_id", "")
                mobile = patient.get("mobile", "")
                patient_name = patient.get("name", "Patient")
                patient_lang = patient.get("language", "english")

                if mobile:
                    if patient_lang == "tamil":
                        booking_msg = f"வணக்கம் {patient_name}! மருத்துவர் உங்களுக்கு அப்பாயிண்ட்மெந்ட் புக் செய்ய சொன்னார். ‘1’ அனுப்புங்கள்."
                    elif patient_lang == "hindi":
                        booking_msg = f"नमस्ते {patient_name}! डॉक्टर ने अपॉइंटमेंट बुक करने को कहा है। ‘1’ भेजें।"
                    else:
                        booking_msg = f"Hi {patient_name}! The doctor recommends a follow-up appointment. Reply ‘1’ to book."

                    twilio_client.messages.create(
                        from_=TWILIO_FROM,
                        to=f"whatsapp:+{mobile}",
                        body=booking_msg
                    )

                    # Reset conversation state so patient can reply '1' to book
                    supabase.rpc("upsert_conversation_state", {"p_mobile": mobile}).execute()
                    supabase.table("conversation_state").update({
                        "state": "idle",
                        "temp_data": {}
                    }).eq("mobile", mobile).execute()

                    print(f"✅ Booking WhatsApp sent to {patient_name} ({mobile})")

        except Exception as e:
            print(f"❌ Error sending booking WhatsApp: {e}")
            import traceback; traceback.print_exc()

    # Play cached Sarvam audio response
    valid_digits = ["1", "2"]
    if digit in valid_digits:
        audio_url = await get_or_generate_response_audio(digit, lang)
    else:
        audio_url = await get_or_generate_response_audio("1", lang)

    response = VoiceResponse()
    response.play(audio_url)

    print(f"✅ Voice response: followup_id={followup_id}, lang={lang}, digit={digit}, response={followup_response}")
    return PlainTextResponse(str(response), media_type="application/xml")

async def prewarm_response_audios():
    """
    Pre-generate all response audios at startup.
    6 files total - 3 languages x 2 responses.
    Skips if already cached.
    """
    print("🔥 Pre-warming response audios...")
    languages = ["english", "tamil", "hindi"]
    digits = ["1", "2"]

    for lang in languages:
        for digit in digits:
            try:
                await get_or_generate_response_audio(digit, lang)
            except Exception as e:
                print(f"❌ Pre-warm failed for {lang}/{digit}: {e}")

    print("✅ All response audios ready!")

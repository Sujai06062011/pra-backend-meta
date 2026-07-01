import asyncio
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks, Response
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from mcp_tools import create_parro_mcp_server
from fastapi.middleware.cors import CORSMiddleware
from routers.availability import router as availability_router
from routers.schedule import router as schedule_router
from routers.clinic_schedule import router as clinic_schedule_router
from twilio.twiml.messaging_response import MessagingResponse
from dotenv import load_dotenv
import os
import httpx
import json
import pytz
from datetime import date, datetime, timedelta
from collections import defaultdict
from whatsapp_handler import handle_message, send_slot_list, format_display_date, format_time, send_cancel_appointment_list
from scheduler import init_scheduler, reschedule
from followup import prewarm_response_audios
from database import supabase, save_conversation_state as upsert_conversation_state
from database import get_display_token, assign_token_for_slot, is_slot_available, _time_str
from database import get_conversation_state, cancel_appointment
import config_loader
import jwt as pyjwt
import time as time_module
import uuid as uuid_lib
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.backends import default_backend
from consultation_helpers import (
    generate_room_id,
    get_patient_join_url,
    is_online_consultation_slot,
    create_consultation_for_appointment,
    send_video_link_to_patient,
    send_meta_buttons,
    send_meta_list,
    send_whatsapp_text,
)


load_dotenv()

IST = pytz.timezone("Asia/Kolkata")

# ── JaaS (8x8 Video) ─────────────────────────────────────────────────────────
# generate_room_id / get_patient_join_url imported from consultation_helpers
JAAS_APP_ID = os.getenv("JAAS_APP_ID", "")
JAAS_API_KEY_ID = os.getenv("JAAS_API_KEY_ID", "")
JAAS_PRIVATE_KEY_STR = os.getenv("JAAS_PRIVATE_KEY", "")


def generate_jaas_jwt(
    room_name: str,
    user_name: str,
    user_email: str = "user@praclinic.in",
    is_moderator: bool = False,
) -> str:
    try:
        private_key_pem = JAAS_PRIVATE_KEY_STR.replace("\\n", "\n")

        if "-----BEGIN" not in private_key_pem:
            private_key_pem = (
                "-----BEGIN PRIVATE KEY-----\n"
                + private_key_pem.strip()
                + "\n-----END PRIVATE KEY-----"
            )

        private_key = serialization.load_pem_private_key(
            private_key_pem.encode(), password=None, backend=default_backend()
        )
        now = int(time_module.time())
        payload = {
            "iss": "chat",
            "iat": now,
            "exp": now + 7200,
            "nbf": now - 10,
            "aud": "jitsi",
            "sub": JAAS_APP_ID,
            "room": room_name,
            "context": {
                "features": {
                    "livestreaming": False,
                    "outbound-call": False,
                    "sip-outbound-call": False,
                    "transcription": False,
                    "recording": is_moderator,
                },
                "user": {
                    "hidden-from-recorder": False,
                    "moderator": is_moderator,
                    "name": user_name,
                    "id": str(uuid_lib.uuid4()),
                    "avatar": "",
                    "email": user_email,
                },
            },
        }
        token = pyjwt.encode(
            payload,
            private_key,
            algorithm="RS256",
            headers={"kid": JAAS_API_KEY_ID},
        )
        return token
    except Exception as e:
        print(f"[JaaS JWT Error] {e}")
        raise HTTPException(status_code=500, detail=f"Failed to generate video token: {e}")


# ─────────────────────────────────────────────────────────────────────────────

app = FastAPI(title="PRA - Patient Relationship Assistant")

# app.add_middleware(
#     CORSMiddleware,
#     allow_origins=["*"],
#     allow_credentials=True,
#     allow_methods=["*"],
#     allow_headers=["*"],
# )

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://localhost:3000",
        "https://pra-frontend.vercel.app",
        "https://pra-frontend-sujai06062011.vercel.app",
        "https://www.anthropic.com",
        "https://api.anthropic.com",
        "https://claude.ai",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(availability_router)
app.include_router(schedule_router)
app.include_router(clinic_schedule_router)

# ── MCP HTTP Transport ────────────────────────────────────────────────────────
_mcp_server = create_parro_mcp_server(supabase)

_MCP_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Authorization",
}


@app.middleware("http")
async def add_mcp_cors(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/mcp"):
        for k, v in _MCP_CORS_HEADERS.items():
            response.headers[k] = v
    return response


@app.options("/mcp")
async def mcp_options():
    return Response(status_code=200, headers=_MCP_CORS_HEADERS)


@app.get("/mcp")
async def mcp_get(request: Request):
    """GET /mcp — SSE stream for clients that want it, JSON info otherwise."""
    if "text/event-stream" in request.headers.get("accept", ""):
        async def event_stream():
            yield f"data: {json.dumps({'type': 'connection', 'status': 'connected'})}\n\n"
            while True:
                await asyncio.sleep(30)
                yield f"data: {json.dumps({'type': 'ping'})}\n\n"
        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive",
                     "X-Accel-Buffering": "no"},
        )
    return {
        "name": "parro-connect-clinic",
        "version": "1.0.0",
        "protocol": "mcp",
        "capabilities": {"tools": {}},
    }


@app.post("/mcp")
async def mcp_post(request: Request):
    """POST /mcp — JSON-RPC 2.0 handler for MCP protocol messages."""
    body = await request.json()
    method = body.get("method", "")
    msg_id = body.get("id", 1)

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "parro-connect-clinic", "version": "1.0.0"},
            },
        }

    elif method == "tools/list":
        tools = await _mcp_server._direct_list_tools()
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "tools": [
                    {"name": t.name, "description": t.description,
                     "inputSchema": t.inputSchema}
                    for t in tools
                ]
            },
        }

    elif method == "tools/call":
        params = body.get("params", {})
        tool_name = params.get("name")
        arguments = params.get("arguments", {})
        result = await _mcp_server._direct_call_tool(tool_name, arguments)
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "content": [{"type": "text", "text": r.text} for r in result]
            },
        }

    elif method == "notifications/initialized":
        return Response(status_code=200)

    else:
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }
# ─────────────────────────────────────────────────────────────────────────────


@app.get("/mcp-info")
async def mcp_info():
    return {
        "name": "Parro Connect Clinic MCP",
        "version": "1.0.0",
        "mcp_endpoint": "https://web-production-a0717.up.railway.app/mcp",
        "tools": [
            "get_clinic_info", "get_patient", "register_patient",
            "get_available_slots", "book_appointment", "get_queue_status",
            "get_upcoming_appointments", "cancel_appointment", "add_family_member",
        ],
        "transport": "HTTP JSON-RPC",
        "status": "active",
    }


@app.get("/mcp-routes")
async def mcp_routes():
    routes = []
    for route in app.routes:
        if hasattr(route, "path"):
            routes.append({"path": route.path,
                           "methods": list(getattr(route, "methods", []))})
    return {"routes": routes}


@app.post("/mcp-call")
async def mcp_call(request: Request):
    """Direct tool call — bypasses MCP protocol for easy curl testing."""
    body = await request.json()
    tool_name = body.get("tool")
    arguments = body.get("arguments", {})
    try:
        result = await _mcp_server._direct_call_tool(tool_name, arguments)
        text = result[0].text if result else "{}"
        return json.loads(text)
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/test/voice-transcribe")
async def test_voice_transcribe(request: Request):
    """Test endpoint: download a Meta media file and transcribe it via Sarvam."""
    body = await request.json()
    media_id = body.get("media_id")
    lang = body.get("language", "ta")
    audio = await download_meta_media(media_id)
    transcript = await transcribe_audio(audio, lang)
    return {"media_id": media_id, "language": lang, "transcript": transcript}

# ── META CLOUD API (WhatsApp) ─────────────────────────────
META_API_VERSION = os.getenv("META_API_VERSION", "v18.0")
META_PHONE_NUMBER_ID = os.getenv("META_PHONE_NUMBER_ID")
META_ACCESS_TOKEN = os.getenv("META_ACCESS_TOKEN")
META_WEBHOOK_VERIFY_TOKEN = os.getenv("META_WEBHOOK_VERIFY_TOKEN", "pra_meta_webhook_verify")
META_BASE_URL = f"https://graph.facebook.com/{META_API_VERSION}/{META_PHONE_NUMBER_ID}/messages"


@app.post("/admin/onboard-clinic")
async def onboard_clinic(request: Request):
    data = await request.json()
    
    # Normalize WhatsApp number - always strip +
    whatsapp = data.get("whatsapp_number", "").replace("+", "").strip()
    
    result = supabase.table("doctors").insert({
        "name": data.get("doctor_name"),
        "clinic_name": data.get("clinic_name"),
        "whatsapp_number": whatsapp,  # always stored without +
        "clinic_timings": data.get("timings", "Mon-Sat: 9AM-1PM, 5PM-8PM"),
        "clinic_address": data.get("address", ""),
        "email": data.get("email", ""),
        "mobile": data.get("mobile", "")
    }).execute()
    
    return {"status": "success", "doctor_id": result.data[0]["id"]}

# ── GLOBAL SCHEDULER REFERENCE (for reload endpoint) ─────
_scheduler = None


@app.on_event("startup")
async def startup_event():
    global _scheduler
    _scheduler = await init_scheduler()
    _scheduler.start()

    # Pre-warm all response audios
    await prewarm_response_audios()

    print("🚀 PRA Backend started with DB-driven scheduler")

async def send_meta_text(to_number: str, message: str):
    headers = {
        "Authorization": f"Bearer {META_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to_number.lstrip("+"),
        "type": "text",
        "text": {"body": message}
    }
    async with httpx.AsyncClient() as client:
        r = await client.post(META_BASE_URL, json=payload, headers=headers)
        print(f"[Meta TEXT] to={to_number} status={r.status_code} body={r.text}")
        return r.json()


async def send_meta_interactive(to_number: str, body_text: str, buttons: list, footer: str = None):
    interactive = {
        "type": "button",
        "body": {"text": body_text},
        "action": {
            "buttons": [
                {"type": "reply", "reply": {"id": b["id"], "title": b["title"]}}
                for b in buttons[:3]
            ]
        }
    }
    if footer:
        interactive["footer"] = {"text": footer}
    headers = {
        "Authorization": f"Bearer {META_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to_number.lstrip("+"),
        "type": "interactive",
        "interactive": interactive
    }
    async with httpx.AsyncClient() as client:
        r = await client.post(META_BASE_URL, json=payload, headers=headers)
        print(f"[Meta INTERACTIVE] to={to_number} status={r.status_code} body={r.text}")
        return r.json()


async def send_meta_template(to_number: str, template_name: str, lang_code: str = "en", components: list = None):
    template = {"name": template_name, "language": {"code": lang_code}}
    if components:
        template["components"] = components
    headers = {
        "Authorization": f"Bearer {META_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to_number.lstrip("+"),
        "type": "template",
        "template": template
    }
    async with httpx.AsyncClient() as client:
        r = await client.post(META_BASE_URL, json=payload, headers=headers)
        print(f"[Meta TEMPLATE] to={to_number} template={template_name} status={r.status_code} body={r.text}")
        return r.json()


async def handle_session_selected(from_number: str, session: str, clinic_number: str = ""):
    """Translate a session_morning / session_evening button tap into a slot list."""
    _, temp_data = get_conversation_state(from_number)
    slots        = temp_data.get(f"{session}_slots", [])
    booking_name = temp_data.get("booking_name", "")
    parsed_date  = temp_data.get("parsed_date", "")
    if not slots:
        await send_meta_text(from_number,
            f"Sorry, no {session} slots available. Reply MENU to start over.")
        return
    upsert_conversation_state(from_number, "awaiting_slot_selection", {**temp_data, "session": session})
    await send_slot_list(from_number, slots, parsed_date, session, 0, booking_name)


async def handle_slot_confirmed(from_number: str, time_str: str, clinic_number: str = ""):
    """Translate a confirm_slot button tap into the existing slot_selected flow."""
    _, temp_data     = get_conversation_state(from_number)
    available_slots  = temp_data.get("available_slots", [])
    slot_time        = time_str[:5]  # normalise to "HH:MM"
    try:
        idx = available_slots.index(slot_time)
    except ValueError:
        # try with seconds suffix
        idx = next((i for i, s in enumerate(available_slots) if str(s)[:5] == slot_time), -1)
    if idx < 0:
        await send_meta_text(from_number, "Sorry, could not find that slot. Reply MENU to start over.")
        return
    print(f"[SLOT CONFIRMED] {from_number} time={slot_time} idx={idx}")
    # Restore awaiting_slot state so slot_selected intent fires correctly
    upsert_conversation_state(from_number, "awaiting_slot", temp_data)
    await handle_inbound_message(from_number, str(idx + 1), clinic_number)


async def handle_date_selected(from_number: str, date_str: str, clinic_number: str = ""):
    """Translate a date_today_* / date_tomorrow_* button tap into the existing date flow."""
    _, temp_data = get_conversation_state(from_number)
    date_options = temp_data.get("date_options", [])
    try:
        idx = date_options.index(date_str)
        synthetic_text = str(idx + 1)  # "1" for today, "2" for tomorrow
    except ValueError:
        # date not in saved options — pass raw ISO date; parse_date won't handle it,
        # but the existing handler accepts free text and tries parse_date; this is a fallback
        synthetic_text = date_str
    print(f"[DATE SELECTED] {from_number} date={date_str} → '{synthetic_text}'")
    await handle_inbound_message(from_number, synthetic_text, clinic_number)


async def handle_visit_type_selected(from_number: str, visit_type: str, clinic_number: str = ""):
    """Translate a visit_in_clinic / visit_online button tap into the existing consult-type flow."""
    visit_text = "1" if visit_type == "in_clinic" else "2"
    print(f"[VISIT TYPE] {from_number} type={visit_type} → '{visit_text}'")
    await handle_inbound_message(from_number, visit_text, clinic_number)


async def handle_member_interactive(from_number: str, selection_id: str, clinic_number: str = ""):
    """Translate a member_* interactive button/list tap into the existing booking flow."""
    _, temp_data = get_conversation_state(from_number)
    booking_patients = temp_data.get("booking_patients", [])

    if selection_id == "member_new":
        # Synthesize the "add new" choice number
        synthetic_text = str(len(booking_patients) + 1)
    else:
        patient_id = selection_id.replace("member_", "", 1)
        idx = next(
            (i for i, p in enumerate(booking_patients) if p["id"] == patient_id),
            None,
        )
        if idx is None:
            await send_meta_text(from_number, "Sorry, could not find that patient. Please try again.")
            return
        synthetic_text = str(idx + 1)

    print(f"[MEMBER INTERACTIVE] {from_number} selection={selection_id} → '{synthetic_text}'")
    await handle_inbound_message(from_number, synthetic_text, clinic_number)


async def handle_appointment_cancel(from_number: str, appointment_id: str):
    """Show a confirm/keep confirmation before cancelling an appointment."""
    appt_res = supabase.table("appointments").select(
        "id, appointment_date, appointment_time, token_number, doctor_id, consultation_type, patients(name)"
    ).eq("id", appointment_id).single().execute()

    if not appt_res.data:
        await send_meta_text(from_number, "Appointment not found. Reply MENU for main menu.")
        return

    appt = appt_res.data
    patient_name = (appt.get("patients") or {}).get("name", "Patient")
    date_str = str(appt.get("appointment_date", ""))[:10]
    time_str = str(appt.get("appointment_time", ""))[:5]
    token_num = appt.get("token_number", "")
    d_tok = get_display_token(token_num, appt.get("appointment_time", ""),
                               doctor_id=appt.get("doctor_id"), date_str=date_str) if token_num else ""

    display_date = format_display_date(date_str)
    display_time = format_time(time_str)
    type_label = "💻 Online" if appt.get("consultation_type") == "online" else "🏥 In Clinic"
    token_line = f"Token {d_tok}" if d_tok else ""

    body = (
        f"Cancel this appointment?\n\n"
        f"👤 {patient_name}\n"
        f"📅 {display_date}\n"
        f"⏰ {display_time}\n"
        f"{type_label}" + (f" · {token_line}" if token_line else "")
    )

    await send_meta_buttons(
        to_number=from_number,
        body_text=body,
        buttons=[
            {"id": f"confirm_cancel_{appointment_id}", "title": "Yes, Cancel"},
            {"id": "keep_appointment", "title": "Keep it"},
        ],
        footer_text="This cannot be undone",
    )

    _, temp_data = get_conversation_state(from_number)
    upsert_conversation_state(from_number, "awaiting_cancel_confirm",
                              {**temp_data, "cancelling_appt_id": appointment_id})


async def handle_inbound_message(from_number: str, text: str, to_number: str = ""):
    """Run the conversation engine on an inbound Meta text and reply via Meta"""
    reply = await handle_message(from_number, text, to_number, "")
    if reply:
        await send_meta_text(from_number, reply)


@app.get("/")
async def root():
    return {"status": "PRA Backend Running", "version": "1.0.0"}


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/privacy")
async def privacy_policy():
    return {
        "app": "PRA - Patient Relationship Assistant",
        "privacy_policy": "Patient data collected through this application is used solely for clinic management and patient care purposes. Data is stored securely and not shared with third parties.",
        "contact": "support@praclinic.in"
    }


@app.post("/webhook/whatsapp")
async def whatsapp_webhook(request: Request):
    try:
        form_data = await request.form()
        from_raw = form_data.get("From", "")
        to_raw = form_data.get("To", "")
        body = form_data.get("Body", "").strip()
        media_url = form_data.get("MediaUrl0", "")

        from_number = from_raw.replace("whatsapp:+", "").replace("whatsapp:", "")
        to_number = to_raw.replace("whatsapp:", "")

        print(f"\n📱 Inbound: {from_number} → {to_number}: {body}")

        reply = await handle_message(from_number, body, to_number, media_url)
        print(f"💬 Reply: {reply[:80]}...")

        await send_meta_text(from_number, reply)

    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()

    resp = MessagingResponse()
    return PlainTextResponse(str(resp), status_code=200, media_type="application/xml")


@app.get("/webhook/whatsapp")
async def whatsapp_verify(request: Request):
    params = dict(request.query_params)
    challenge = params.get("hub.challenge", "")
    return PlainTextResponse(challenge)


# ── Voice note helpers ────────────────────────────────────────────────────────

async def download_meta_media(media_id: str) -> bytes:
    token = os.getenv("META_ACCESS_TOKEN")
    version = os.getenv("META_API_VERSION", "v18.0")
    async with httpx.AsyncClient() as client:
        url_resp = await client.get(
            f"https://graph.facebook.com/{version}/{media_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        media_url = url_resp.json()["url"]
        audio_resp = await client.get(
            media_url,
            headers={"Authorization": f"Bearer {token}"},
        )
    return audio_resp.content


async def transcribe_audio(audio_bytes: bytes, language_code: str = "ta") -> str:
    """Transcribe audio bytes using Groq Whisper large-v3.
    Result goes through the SAME routing logic as typed text — no special-casing."""
    groq_key = os.getenv("GROQ_API_KEY", "")
    if not groq_key:
        print("[STT ERROR] GROQ_API_KEY not set — add to Railway env vars")
        return ""

    lang_map = {"ta": "ta", "hi": "hi", "en": "en", "te": "te", "kn": "kn", "ml": "ml"}
    whisper_lang = lang_map.get(language_code, "ta")
    print(f"[STT] Audio: {len(audio_bytes)} bytes, lang: {whisper_lang}")

    try:
        import groq as _groq
        import io
        client = _groq.Groq(api_key=groq_key)
        audio_file = io.BytesIO(audio_bytes)
        audio_file.name = "audio.ogg"
        transcription = client.audio.transcriptions.create(
            model="whisper-large-v3",
            file=audio_file,
            language=whisper_lang,
            response_format="text",
        )
        transcript = transcription.strip() if isinstance(transcription, str) else str(transcription).strip()
        print(f"[STT] Transcript: '{transcript}'")
        return transcript
    except Exception as e:
        print(f"[STT EXCEPTION] {e}")
        return ""

# ─────────────────────────────────────────────────────────────────────────────


@app.get("/webhook/meta")
async def meta_webhook_verify(request: Request):
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")
    print(f"[Meta Webhook Verify] mode={mode} token={token} challenge={challenge}")
    if mode == "subscribe" and token == META_WEBHOOK_VERIFY_TOKEN:
        print("[Meta Webhook] Verified successfully")
        return PlainTextResponse(challenge)
    raise HTTPException(status_code=403, detail="Forbidden")


@app.post("/webhook/meta")
async def meta_webhook_inbound(request: Request):
    body = await request.json()
    print(f"[Meta Inbound] {json.dumps(body)[:500]}")
    try:
        entry = body["entry"][0]
        value = entry["changes"][0]["value"]

        if "statuses" in value and "messages" not in value:
            return {"status": "ok"}

        messages = value.get("messages", [])
        if not messages:
            return {"status": "ok"}

        msg = messages[0]
        from_number = msg["from"]
        msg_type = msg["type"]
        clinic_number = value.get("metadata", {}).get("display_phone_number", "")

        if msg_type == "text":
            text = msg["text"]["body"].strip()
            print(f"[Meta TEXT IN] from={from_number} text={text}")
            await handle_inbound_message(from_number, text, clinic_number)

        elif msg_type == "audio":
            try:
                audio_id = msg["audio"]["id"]

                # Detect patient language for STT
                patient_lang = "ta"
                try:
                    lang_result = supabase.table("patients")\
                        .select("language")\
                        .eq("mobile", from_number)\
                        .eq("doctor_id", "8c33abe0-5d2e-4613-9437-c7c375e8d162")\
                        .limit(1).execute()
                    if lang_result.data:
                        lang = lang_result.data[0].get("language", "tamil").lower()
                        patient_lang = {"tamil": "ta", "hindi": "hi", "english": "en"}.get(lang, "ta")
                except Exception:
                    pass

                audio_bytes = await download_meta_media(audio_id)
                transcript = await transcribe_audio(audio_bytes, patient_lang)

                if not transcript:
                    await send_meta_text(
                        from_number,
                        "மன்னிக்கவும், உங்கள் குரல் கேட்கவில்லை. மீண்டும் பேசவும் அல்லது தட்டச்சு செய்யவும்.\n\n"
                        "Sorry, could not hear you clearly. Please try again or type your message.",
                    )
                else:
                    print(f"[VOICE] {from_number} said: {transcript}")
                    await handle_inbound_message(from_number, transcript, clinic_number)
            except Exception as e:
                print(f"[VOICE ERROR] {e}")
                await send_meta_text(
                    from_number,
                    "Voice note processing failed. Please type your message instead.",
                )

        elif msg_type == "interactive":
            interactive_type = msg["interactive"].get("type", "button_reply")

            if interactive_type == "list_reply":
                list_id = msg["interactive"]["list_reply"]["id"]
                print(f"[Meta LIST_REPLY] from={from_number} id={list_id}")
                if list_id.startswith("slots_more_"):
                    # slots_more_{date}_{session}_{offset}  e.g. slots_more_2026-06-20_morning_5
                    current_state, temp_data = get_conversation_state(from_number)
                    if current_state not in ("awaiting_slot_selection", "awaiting_slot_confirmation"):
                        await send_meta_text(from_number,
                            "Your booking is already in progress.\n"
                            "Reply MENU to start over or continue your booking.")
                    else:
                        remainder = list_id[len("slots_more_"):]          # "2026-06-20_morning_5"
                        parts     = remainder.split("_")
                        date_str  = parts[0]                               # "2026-06-20"
                        session   = parts[1]                               # "morning"/"evening"
                        offset    = int(parts[2])                          # 5, 10, …
                        slots     = temp_data.get(f"{session}_slots", [])
                        booking_name = temp_data.get("booking_name", "")
                        upsert_conversation_state(from_number, "awaiting_slot_selection", temp_data)
                        await send_slot_list(from_number, slots, date_str, session, offset, booking_name)

                elif list_id.startswith("slot_"):
                    # slot_{date}_{time}  e.g. slot_2026-06-20_10:00
                    current_state, temp_data = get_conversation_state(from_number)
                    if current_state not in ("awaiting_slot_selection",):
                        print(f"[STALE] slot list tap ignored, state={current_state}")
                        await send_meta_text(from_number,
                            "Your booking is already in progress.\n"
                            "Reply MENU to start over or continue your booking.")
                    else:
                        remainder    = list_id[len("slot_"):]              # "2026-06-20_10:00"
                        date_str, time_str = remainder.split("_", 1)
                        booking_name = temp_data.get("booking_name", "")
                        patient_id_  = temp_data.get("booking_for", "")
                        session      = temp_data.get("session", "morning")
                        display_time = format_time(time_str[:5])
                        display_date = format_display_date(date_str)
                        # Save pending slot and set confirmation state
                        upsert_conversation_state(
                            from_number, "awaiting_slot_confirmation",
                            {**temp_data, "pending_slot": time_str[:5]},
                        )
                        await send_meta_buttons(
                            to_number=from_number,
                            body_text=(
                                f"Confirm this appointment?\n\n"
                                f"👤 {booking_name}\n"
                                f"📅 {display_date}\n"
                                f"⏰ {display_time}"
                            ),
                            buttons=[
                                {"id": f"confirm_slot_{date_str}_{time_str[:5]}", "title": "✅ Confirm"},
                                {"id": "cancel_slot", "title": "❌ Cancel"},
                            ],
                            footer_text="Tap Confirm to book",
                        )

                elif list_id.startswith("cancel_"):
                    current_state, _ = get_conversation_state(from_number)
                    if current_state not in ("awaiting_cancel_selection", "awaiting_cancel_choice"):
                        print(f"[STALE] cancel list tap ignored, state={current_state}")
                        await send_meta_text(from_number,
                            "Your session has moved on. Reply MENU to start over.")
                    else:
                        appointment_id = list_id[len("cancel_"):]
                        await handle_appointment_cancel(from_number, appointment_id)

                elif list_id.startswith("member_"):
                    current_state, _ = get_conversation_state(from_number)
                    if current_state != "awaiting_booking_patient_select":
                        print(f"[STALE] member list ignored, state={current_state}")
                        await send_meta_text(from_number,
                            "Your booking is already in progress.\n"
                            "Reply MENU to start over or continue your booking.")
                    else:
                        await handle_member_interactive(from_number, list_id, clinic_number)
                elif list_id.startswith("doctor_"):
                    current_state, _ = get_conversation_state(from_number)
                    if current_state != "awaiting_doctor_select":
                        print(f"[STALE] doctor list ignored, state={current_state}")
                    else:
                        await handle_inbound_message(from_number, list_id, clinic_number)
                elif list_id.startswith("qdr_"):
                    # query doctor selection
                    current_state, _ = get_conversation_state(from_number)
                    if current_state != "awaiting_query_doctor_select":
                        print(f"[STALE] query doctor list ignored, state={current_state}")
                    else:
                        await handle_inbound_message(from_number, list_id, clinic_number)
                else:
                    list_id_to_text = {
                        "menu_book_appointment":  "1",
                        "menu_my_appointments":   "2",
                        "menu_queue_status":      "3",
                        "menu_cancel_appointment": "4",
                        "menu_clinic_timings":    "5",
                        "menu_ask_doctor":        "6",
                    }
                    mapped_text = list_id_to_text.get(list_id, "MENU")
                    await handle_inbound_message(from_number, mapped_text, clinic_number)

            else:
                # button_reply
                button_id = msg["interactive"]["button_reply"]["id"]
                print(f"[Meta BUTTON] from={from_number} id={button_id}")
                if button_id.startswith("member_"):
                    current_state, _ = get_conversation_state(from_number)
                    if current_state != "awaiting_booking_patient_select":
                        print(f"[STALE] member button ignored, state={current_state}")
                        await send_meta_text(from_number,
                            "Your booking is already in progress.\n"
                            "Reply MENU to start over or continue your booking.")
                    else:
                        await handle_member_interactive(from_number, button_id, clinic_number)
                else:
                    parts = button_id.split("__", 1)
                    action = parts[0]
                    followup_id = parts[1] if len(parts) > 1 else None

                    if button_id in ("gender_male", "gender_female", "gender_other"):
                        current_state, _ = get_conversation_state(from_number)
                        if current_state not in ("awaiting_gender", "awaiting_new_member_gender"):
                            print(f"[STALE] gender button ignored, state={current_state}")
                            await send_meta_text(from_number,
                                "Your session has moved on. Reply MENU to start over.")
                        else:
                            gender_map = {
                                "gender_male": "Male", "gender_female": "Female", "gender_other": "Other"
                            }
                            await handle_inbound_message(from_number, gender_map[button_id], clinic_number)

                    elif button_id in ("lang_tamil", "lang_english", "lang_hindi"):
                        current_state, _ = get_conversation_state(from_number)
                        if current_state not in ("awaiting_language", "awaiting_new_member_language"):
                            print(f"[STALE] lang button ignored, state={current_state}")
                            await send_meta_text(from_number,
                                "Your session has moved on. Reply MENU to start over.")
                        else:
                            lang_map = {
                                "lang_tamil": "Tamil", "lang_english": "English", "lang_hindi": "Hindi"
                            }
                            await handle_inbound_message(from_number, lang_map[button_id], clinic_number)

                    elif button_id.startswith("date_"):
                        current_state, _ = get_conversation_state(from_number)
                        if current_state != "awaiting_booking_date":
                            print(f"[STALE] date button ignored, state={current_state}")
                            await send_meta_text(from_number,
                                "Your booking is already in progress.\n"
                                "Reply MENU to start over or continue your booking.")
                        elif button_id == "date_other":
                            await send_meta_text(from_number,
                                "Please type your preferred date:\n"
                                "(e.g. 20 Jun 2026)")
                            # state stays awaiting_booking_date; patient types date → parse_date handles it
                        else:
                            # date_today_2026-06-17 or date_tomorrow_2026-06-18
                            date_str = button_id.split("_")[-1]  # "2026-06-17"
                            await handle_date_selected(from_number, date_str, clinic_number)

                    elif button_id in ("session_morning", "session_evening"):
                        current_state, _ = get_conversation_state(from_number)
                        if current_state != "awaiting_session_selection":
                            print(f"[STALE] session button ignored, state={current_state}")
                            await send_meta_text(from_number,
                                "Your booking is already in progress.\n"
                                "Reply MENU to start over or continue your booking.")
                        else:
                            session = "morning" if button_id == "session_morning" else "evening"
                            await handle_session_selected(from_number, session, clinic_number)

                    elif button_id.startswith("confirm_slot_"):
                        current_state, _ = get_conversation_state(from_number)
                        if current_state != "awaiting_slot_confirmation":
                            print(f"[STALE] confirm_slot button ignored, state={current_state}")
                            await send_meta_text(from_number,
                                "Your booking is already in progress.\n"
                                "Reply MENU to start over or continue your booking.")
                        else:
                            # confirm_slot_2026-06-20_10:00
                            remainder = button_id[len("confirm_slot_"):]
                            date_str, time_str = remainder.split("_", 1)
                            await handle_slot_confirmed(from_number, time_str, clinic_number)

                    elif button_id == "cancel_slot":
                        current_state, _ = get_conversation_state(from_number)
                        if current_state == "awaiting_slot_confirmation":
                            _, temp_data = get_conversation_state(from_number)
                            session      = temp_data.get("session", "morning")
                            parsed_date  = temp_data.get("parsed_date", "")
                            booking_name = temp_data.get("booking_name", "")
                            slots        = temp_data.get(f"{session}_slots", [])
                            await send_meta_text(from_number, "No problem! Please choose another slot.")
                            upsert_conversation_state(from_number, "awaiting_slot_selection", temp_data)
                            await send_slot_list(from_number, slots, parsed_date, session, 0, booking_name)

                    elif button_id.startswith("confirm_cancel_"):
                        current_state, temp_data = get_conversation_state(from_number)
                        if current_state != "awaiting_cancel_confirm":
                            print(f"[STALE] confirm_cancel ignored, state={current_state}")
                            await send_meta_text(from_number,
                                "Your session has moved on. Reply MENU to start over.")
                        else:
                            appointment_id = button_id[len("confirm_cancel_"):]
                            cancel_appointment(appointment_id)

                            appt_res = supabase.table("appointments").select(
                                "appointment_date, appointment_time, token_number, doctor_id, patients(name)"
                            ).eq("id", appointment_id).single().execute()

                            if appt_res.data:
                                a = appt_res.data
                                pname = (a.get("patients") or {}).get("name", "Patient")
                                d_tok = get_display_token(a.get("token_number", ""), a.get("appointment_time", ""),
                                                           doctor_id=a.get("doctor_id"), date_str=str(a.get("appointment_date", ""))[:10]) if a.get("token_number") else ""
                                display_date = format_display_date(str(a.get("appointment_date", ""))[:10])
                                display_time = format_time(str(a.get("appointment_time", ""))[:5])
                                token_line = f"Token {d_tok}" if d_tok else ""
                                msg = (
                                    f"✅ Appointment cancelled.\n\n"
                                    f"👤 {pname}\n"
                                    f"📅 {display_date} · {display_time}"
                                    + (f"\n{token_line}" if token_line else "")
                                    + "\n\nWe hope to see you soon! Reply MENU to book again."
                                )
                            else:
                                msg = "✅ Appointment cancelled.\n\nReply MENU for main menu."

                            await send_meta_text(from_number, msg)
                            upsert_conversation_state(from_number, "idle", {})
                            await handle_inbound_message(from_number, "MENU", clinic_number)

                    elif button_id == "keep_appointment":
                        current_state, _ = get_conversation_state(from_number)
                        if current_state == "awaiting_cancel_confirm":
                            await send_meta_text(from_number,
                                "No problem! Your appointment is kept. 👍\n\nReply MENU for main menu.")
                            upsert_conversation_state(from_number, "idle", {})
                            await handle_inbound_message(from_number, "MENU", clinic_number)

                    elif button_id.startswith("cancel_") and button_id not in ("cancel_slot",):
                        # cancel_{uuid} — appointment cancellation from buttons (≤3 appts)
                        current_state, _ = get_conversation_state(from_number)
                        if current_state not in ("awaiting_cancel_selection", "awaiting_cancel_choice"):
                            print(f"[STALE] cancel button ignored, state={current_state}")
                            await send_meta_text(from_number,
                                "Your session has moved on. Reply MENU to start over.")
                        else:
                            appointment_id = button_id[len("cancel_"):]
                            await handle_appointment_cancel(from_number, appointment_id)

                    elif button_id in ("visit_in_clinic", "visit_online"):
                        current_state, _ = get_conversation_state(from_number)
                        if current_state != "awaiting_consult_type":
                            print(f"[STALE] visit_type button ignored, state={current_state}")
                            await send_meta_text(from_number,
                                "Your booking is already in progress.\n"
                                "Reply MENU to start over or continue your booking.")
                        else:
                            visit_type = "in_clinic" if button_id == "visit_in_clinic" else "online"
                            await handle_visit_type_selected(from_number, visit_type, clinic_number)
                    elif action == "fu_book_yes" and followup_id:
                        supabase.table("followups").update({
                            "call_status": "Booked"
                        }).eq("id", followup_id).execute()
                        original = supabase.table("followups").select(
                            "*, patients(name, language)"
                        ).eq("id", followup_id).single().execute().data
                        patient_name = original["patients"]["name"]
                        patient_id = original["patient_id"]
                        followup_doctor_id = original.get("doctor_id", doctor_id)
                        upsert_conversation_state(from_number, "awaiting_booking_date", {
                            "patient_id": patient_id,
                            "patient_name": patient_name,
                            "doctor_id": followup_doctor_id
                        })
                        await send_meta_text(from_number,
                            f"Let's book an appointment for {patient_name}. "
                            f"What date works for you? (e.g. 15 Jun or tomorrow)")

                    elif action == "fu_book_no" and followup_id:
                        await send_meta_text(from_number,
                            "Okay, hope you feel better soon! 🙏\n"
                            "Do rest well and follow the prescription. Reply Hi anytime if you need help.")

                    elif followup_id:
                        if action == "ok":
                            supabase.table("followups").update({
                                "call_status": "Recovered", "response": "Better"
                            }).eq("id", followup_id).execute()
                            await send_meta_text(from_number,
                                "Wonderful! So glad to hear you're feeling better. 😊\n\n"
                                "Stay healthy and take care!\n"
                                "— TrueCare Family Clinic")

                        elif action == "recovering":
                            supabase.table("followups").update({
                                "call_status": "Recovering", "response": "Same"
                            }).eq("id", followup_id).execute()
                            await send_meta_interactive(
                                from_number,
                                "Sorry to hear you're still recovering. 🙏\n\n"
                                "Would you like to book an appointment with the doctor?",
                                [
                                    {"id": f"fu_book_yes__{followup_id}", "title": "Yes, book now"},
                                    {"id": f"fu_book_no__{followup_id}",  "title": "No, I'll wait"},
                                ]
                            )

                        elif action == "appt":
                            supabase.table("followups").update({
                                "call_status": "Needs Appointment", "response": "Worse"
                            }).eq("id", followup_id).execute()
                            original = supabase.table("followups").select(
                                "*, patients(name, language)"
                            ).eq("id", followup_id).single().execute().data
                            patient_name = original["patients"]["name"]
                            patient_id = original["patient_id"]
                            followup_doctor_id = original.get("doctor_id", doctor_id)
                            upsert_conversation_state(from_number, "awaiting_booking_date", {
                                "patient_id": patient_id,
                                "patient_name": patient_name,
                                "doctor_id": followup_doctor_id
                            })
                            await send_meta_text(from_number,
                                f"We'll book an appointment for {patient_name}. "
                                f"What date works for you? (e.g. 15 Jun or tomorrow)")

    except Exception as e:
        print(f"[Meta Webhook ERROR] {str(e)}")
        import traceback
        traceback.print_exc()
    return {"status": "ok"}


# ── TEST TRIGGER ENDPOINTS ────────────────────────────────
@app.post("/trigger/morning-reminders")
async def trigger_morning_reminders():
    from scheduler import send_morning_reminders
    await send_morning_reminders()
    return {"status": "Morning reminders sent"}


@app.post("/trigger/evening-reminders")
async def trigger_evening_reminders():
    from scheduler import send_evening_reminders
    await send_evening_reminders()
    return {"status": "Evening reminders sent"}


@app.post("/trigger/visit-summary")
async def trigger_visit_summary():
    from scheduler import send_visit_summary
    await send_visit_summary()
    return {"status": "Visit summaries sent"}


@app.post("/trigger/review-requests")
async def trigger_review_requests():
    from scheduler import send_review_requests
    await send_review_requests()
    return {"status": "Review requests sent"}


# ── CLINIC MEDICINES ──────────────────────────────────────

@app.get("/medicines/categories")
async def get_medicine_categories(doctor_id: str):
    from database import supabase as db
    result = db.table("clinic_medicines").select("category").eq("doctor_id", doctor_id).eq("is_active", True).execute()
    categories = sorted(set(r["category"] for r in (result.data or []) if r.get("category")))
    return categories

def _compute_stock_status(medicine: dict, stock_batches: list) -> dict:
    """Compute stock summary fields for a medicine given its active stock batches."""
    today = date.today()
    ninety_days = today + timedelta(days=90)

    total_stock = sum(b.get("tablets_remaining", 0) or 0 for b in stock_batches)
    threshold = medicine.get("low_stock_threshold")
    has_expired = any(b.get("expiry_date") and b["expiry_date"] < today.isoformat() for b in stock_batches if b.get("tablets_remaining", 0) > 0)
    has_expiring = any(
        b.get("expiry_date") and today.isoformat() < b["expiry_date"] <= ninety_days.isoformat()
        for b in stock_batches if b.get("tablets_remaining", 0) > 0
    )

    expiry_dates = [b["expiry_date"] for b in stock_batches if b.get("expiry_date") and b.get("tablets_remaining", 0) > 0]
    earliest_expiry = min(expiry_dates) if expiry_dates else None

    # Only compute status if stock has ever been entered
    if not stock_batches and total_stock == 0:
        stock_status = None
    elif has_expired:
        stock_status = "expired"
    elif total_stock == 0:
        stock_status = "out_of_stock"
    elif threshold is not None and total_stock <= threshold:
        stock_status = "low_stock"
    elif has_expiring:
        stock_status = "expiring_soon"
    else:
        stock_status = "ok"

    return {
        "total_stock": total_stock if stock_batches else None,
        "stock_status": stock_status,
        "earliest_expiry": earliest_expiry,
    }

@app.get("/medicines")
async def search_medicines(doctor_id: str, search: str = "", limit: int = 10, active: str = ""):
    from database import supabase as db
    q = db.table("clinic_medicines").select("*").eq("doctor_id", doctor_id)
    if active == "true":
        q = q.eq("is_active", True)
    elif active == "false":
        q = q.eq("is_active", False)
    else:
        # Default: return all (for Medicines page). Prescription search passes active=true
        pass
    result = q.order("usage_count", desc=True).limit(500).execute()
    data = result.data or []

    # For prescription search (no active param or active=true), filter active
    # Note: if caller passes active=true explicitly, already filtered above
    # For search (prescription writer), we should only show active
    if search:
        q_lower = search.lower()
        data = [m for m in data if q_lower in (m.get("name") or "").lower()]
        # Prescription search: only return truly active (is_active != false)
        data = [m for m in data if m.get("is_active") is not False]

    if limit and len(data) > limit:
        data = data[:limit]

    # Fetch stock for all medicines in one query
    if data:
        med_ids = [m["id"] for m in data]
        stock_res = db.table("medicine_stock").select("medicine_id, tablets_remaining, expiry_date, is_active").in_("medicine_id", med_ids).eq("is_active", True).execute()
        stock_map: dict = {}
        for s in (stock_res.data or []):
            stock_map.setdefault(s["medicine_id"], []).append(s)

        for m in data:
            batches = stock_map.get(m["id"], [])
            stock_info = _compute_stock_status(m, batches)
            m["total_stock"] = stock_info["total_stock"]
            m["stock_status"] = stock_info["stock_status"]
            m["earliest_expiry"] = stock_info["earliest_expiry"]

    return data

@app.post("/medicines")
async def add_medicine(request: Request):
    from database import supabase as db
    import datetime as dt
    body = await request.json()
    result = db.table("clinic_medicines").insert({
        "doctor_id": body["doctor_id"],
        "name": body["name"],
        "category": body.get("category", "Other"),
        "dosages": body.get("dosages", []),
        "form": body.get("form", "tablet"),
        "is_active": True,
        "purchase_unit": body.get("purchase_unit", "strip"),
        "dispense_unit": body.get("dispense_unit", "tablet"),
        "tablets_per_strip": body.get("tablets_per_strip", 10),
        "low_stock_threshold": body.get("low_stock_threshold") or None,
    }).execute()
    return result.data[0] if result.data else {}

@app.put("/medicines/{medicine_id}")
async def update_medicine(medicine_id: str, request: Request):
    from database import supabase as db
    import datetime as dt
    body = await request.json()
    allowed = ["name", "category", "dosages", "form", "is_active",
               "purchase_unit", "dispense_unit", "tablets_per_strip", "low_stock_threshold"]
    update_data = {k: body[k] for k in allowed if k in body}
    update_data["updated_at"] = dt.datetime.utcnow().isoformat()
    result = db.table("clinic_medicines").update(update_data).eq("id", medicine_id).execute()
    return result.data[0] if result.data else {}

@app.patch("/medicines/{medicine_id}/increment-usage")
async def increment_usage(medicine_id: str):
    from database import supabase as db
    db.rpc("increment_medicine_usage", {"med_id": medicine_id}).execute()
    return {"ok": True}

@app.delete("/medicines/{medicine_id}")
async def deactivate_medicine(medicine_id: str):
    from database import supabase as db
    db.table("clinic_medicines").update({"is_active": False}).eq("id", medicine_id).execute()
    return {"ok": True}

@app.patch("/medicines/{medicine_id}/deactivate")
async def deactivate_medicine_v2(medicine_id: str, request: Request):
    from database import supabase as db
    body = await request.json()
    db.table("clinic_medicines").update({
        "is_active": False,
        "deactivated_at": datetime.now(IST).isoformat(),
        "deactivation_reason": body.get("reason") or None,
    }).eq("id", medicine_id).execute()
    return {"ok": True}

@app.patch("/medicines/{medicine_id}/activate")
async def activate_medicine(medicine_id: str):
    from database import supabase as db
    db.table("clinic_medicines").update({
        "is_active": True,
        "deactivated_at": None,
        "deactivation_reason": None,
    }).eq("id", medicine_id).execute()
    return {"ok": True}

@app.patch("/medicines/{medicine_id}/threshold")
async def set_medicine_threshold(medicine_id: str, request: Request):
    from database import supabase as db
    body = await request.json()
    db.table("clinic_medicines").update({
        "low_stock_threshold": body.get("threshold"),
    }).eq("id", medicine_id).execute()
    return {"ok": True}

@app.get("/medicines/{medicine_id}/stock")
async def get_medicine_stock(medicine_id: str):
    from database import supabase as db
    med_res = db.table("clinic_medicines").select("tablets_per_strip").eq("id", medicine_id).execute()
    tablets_per_strip = (med_res.data[0].get("tablets_per_strip") or 10) if med_res.data else 10

    result = db.table("medicine_stock").select("*").eq("medicine_id", medicine_id).eq("is_active", True).order("expiry_date", desc=False).execute()
    today = date.today().isoformat()
    ninety_days = (date.today() + timedelta(days=90)).isoformat()
    batches = []
    for b in (result.data or []):
        expiry = b.get("expiry_date", "")
        if expiry < today:
            expiry_status = "expired"
        elif expiry <= ninety_days:
            expiry_status = "expiring_soon"
        else:
            expiry_status = "ok"
        remaining = b.get("tablets_remaining", 0) or 0
        strips_remaining = round(remaining / tablets_per_strip, 2) if tablets_per_strip else remaining
        batches.append({**b, "expiry_status": expiry_status, "strips_remaining": strips_remaining})
    return batches

@app.post("/medicines/{medicine_id}/stock")
async def add_medicine_stock(medicine_id: str, request: Request):
    from database import supabase as db
    body = await request.json()

    med_res = db.table("clinic_medicines").select("tablets_per_strip, doctor_id").eq("id", medicine_id).execute()
    if not med_res.data:
        raise HTTPException(404, "Medicine not found")
    med = med_res.data[0]
    tablets_per_strip = med.get("tablets_per_strip") or 10
    doctor_id = med.get("doctor_id")

    strips_received = int(body.get("strips_received", 0))
    tablets_received = strips_received * tablets_per_strip
    batch_number = body.get("batch_number", "")
    supplier_name = body.get("supplier_name", "")

    batch_res = db.table("medicine_stock").insert({
        "medicine_id": medicine_id,
        "doctor_id": doctor_id,
        "batch_number": batch_number,
        "expiry_date": body.get("expiry_date"),
        "strips_received": strips_received,
        "tablets_received": tablets_received,
        "tablets_remaining": tablets_received,
        "purchase_price_per_strip": body.get("purchase_price_per_strip"),
        "supplier_name": supplier_name,
        "invoice_number": body.get("invoice_number"),
        "date_received": body.get("date_received") or date.today().isoformat(),
        "is_active": True,
        "created_at": datetime.now(IST).isoformat(),
        "updated_at": datetime.now(IST).isoformat(),
    }).execute()
    batch = batch_res.data[0] if batch_res.data else {}
    batch_id = batch.get("id")

    db.table("stock_transactions").insert({
        "medicine_id": medicine_id,
        "stock_batch_id": batch_id,
        "doctor_id": doctor_id,
        "transaction_type": "purchase",
        "quantity_change": tablets_received,
        "notes": f"Batch {batch_number} received from {supplier_name}",
        "created_at": datetime.now(IST).isoformat(),
    }).execute()

    return {"ok": True, "batch": batch, "tablets_received": tablets_received}

@app.post("/medicines/{medicine_id}/stock/{batch_id}/writeoff")
async def writeoff_stock(medicine_id: str, batch_id: str, request: Request):
    from database import supabase as db
    body = await request.json()
    reason = body.get("reason", "expired")
    quantity = int(body.get("quantity", 0))

    batch_res = db.table("medicine_stock").select("*").eq("id", batch_id).eq("medicine_id", medicine_id).execute()
    if not batch_res.data:
        raise HTTPException(404, "Batch not found")
    batch = batch_res.data[0]
    available = batch.get("tablets_remaining", 0) or 0
    deduct = min(quantity, available)
    new_remaining = available - deduct

    db.table("medicine_stock").update({
        "tablets_remaining": new_remaining,
        "is_active": new_remaining > 0,
        "updated_at": datetime.now(IST).isoformat(),
    }).eq("id", batch_id).execute()

    db.table("stock_transactions").insert({
        "medicine_id": medicine_id,
        "stock_batch_id": batch_id,
        "doctor_id": batch.get("doctor_id"),
        "transaction_type": "expired_writeoff",
        "quantity_change": -deduct,
        "notes": f"Write-off: {reason}",
        "created_at": datetime.now(IST).isoformat(),
    }).execute()

    return {"ok": True, "deducted": deduct, "remaining": new_remaining}

@app.patch("/medicine-stock/{batch_id}")
async def edit_stock_batch(batch_id: str, request: Request):
    from database import supabase as db
    body = await request.json()

    tx_res = db.table("stock_transactions").select("id").eq("stock_batch_id", batch_id).eq("transaction_type", "dispensed").execute()
    has_dispensing = len(tx_res.data or []) > 0

    update_data = {"updated_at": datetime.now(IST).isoformat()}

    for field in ("batch_number", "expiry_date", "purchase_price_per_strip",
                  "supplier_name", "invoice_number", "date_received"):
        if field in body:
            update_data[field] = body[field]

    if "strips_received" in body and not has_dispensing:
        batch_res = db.table("medicine_stock").select("medicine_id").eq("id", batch_id).single().execute()
        med_res = db.table("clinic_medicines").select("tablets_per_strip").eq("id", batch_res.data["medicine_id"]).single().execute()
        tablets_per_strip = (med_res.data.get("tablets_per_strip") or 10) if med_res.data else 10
        new_strips = int(body["strips_received"])
        new_tablets = new_strips * tablets_per_strip
        update_data["strips_received"] = new_strips
        update_data["tablets_received"] = new_tablets
        update_data["tablets_remaining"] = new_tablets

    result = db.table("medicine_stock").update(update_data).eq("id", batch_id).execute()
    return {
        "success": True,
        "batch": result.data[0] if result.data else None,
        "strips_editable": not has_dispensing,
    }

@app.post("/medicine-stock/{batch_id}/adjust")
async def adjust_stock(batch_id: str, request: Request):
    from database import supabase as db
    body = await request.json()

    batch_res = db.table("medicine_stock").select("*, clinic_medicines(id, doctor_id)").eq("id", batch_id).single().execute()
    if not batch_res.data:
        raise HTTPException(status_code=404, detail="Batch not found")

    batch = batch_res.data
    current_remaining = batch["tablets_remaining"]
    adjusted_quantity = int(body["adjusted_quantity"])
    difference = adjusted_quantity - current_remaining

    db.table("medicine_stock").update({
        "tablets_remaining": adjusted_quantity,
        "is_active": adjusted_quantity > 0,
        "updated_at": datetime.now(IST).isoformat(),
    }).eq("id", batch_id).execute()

    reason = body.get("reason", "Manual adjustment")
    notes = body.get("notes", "")
    note_str = f"Adjustment: {reason}. {notes}".strip(". ") if notes else f"Adjustment: {reason}"

    db.table("stock_transactions").insert({
        "medicine_id": batch["medicine_id"],
        "stock_batch_id": batch_id,
        "doctor_id": batch["clinic_medicines"]["doctor_id"],
        "transaction_type": "adjustment",
        "quantity_change": difference,
        "notes": note_str,
        "created_at": datetime.now(IST).isoformat(),
    }).execute()

    return {
        "success": True,
        "previous_quantity": current_remaining,
        "adjusted_quantity": adjusted_quantity,
        "difference": difference,
    }

@app.get("/medicines/{medicine_id}/transactions")
async def get_medicine_transactions(medicine_id: str):
    from database import supabase as db
    result = db.table("stock_transactions").select("*, medicine_stock(batch_number, expiry_date)").eq("medicine_id", medicine_id).order("created_at", desc=True).limit(50).execute()
    return result.data or []

@app.get("/dashboard/pharmacy-alerts")
async def get_pharmacy_alerts(doctor_id: str):
    from database import supabase as db
    today = date.today().isoformat()
    ninety_days = (date.today() + timedelta(days=90)).isoformat()

    # 1. All active medicines with their stock totals
    meds_res = db.table("clinic_medicines").select("id, name, dispense_unit, low_stock_threshold").eq("doctor_id", doctor_id).eq("is_active", True).execute()
    meds = {m["id"]: m for m in (meds_res.data or [])}

    low_stock = []
    if meds:
        stock_res = db.table("medicine_stock").select("medicine_id, tablets_remaining").in_("medicine_id", list(meds.keys())).eq("is_active", True).gte("expiry_date", today).execute()
        totals: dict = {}
        for s in (stock_res.data or []):
            totals[s["medicine_id"]] = totals.get(s["medicine_id"], 0) + (s["tablets_remaining"] or 0)

        for mid, med in meds.items():
            threshold = med.get("low_stock_threshold")
            if threshold is None:
                continue
            total = totals.get(mid, 0)
            if total <= threshold:
                low_stock.append({
                    "id": mid,
                    "name": med["name"],
                    "dispense_unit": med.get("dispense_unit", "tablet"),
                    "low_stock_threshold": threshold,
                    "total_stock": total,
                })

    # 2. Expiring soon
    exp_res = db.table("medicine_stock").select("id, medicine_id, expiry_date, batch_number, tablets_remaining, clinic_medicines(name, is_active)").eq("is_active", True).gt("expiry_date", today).lte("expiry_date", ninety_days).gt("tablets_remaining", 0).order("expiry_date", desc=False).execute()
    expiring_soon = []
    for s in (exp_res.data or []):
        med_info = s.get("clinic_medicines") or {}
        if med_info.get("is_active") is False:
            continue
        med_id = s["medicine_id"]
        if med_id not in meds and med_info.get("is_active") is not False:
            pass
        expiring_soon.append({
            "id": med_id,
            "batch_id": s["id"],
            "name": med_info.get("name", ""),
            "expiry_date": s["expiry_date"],
            "batch_number": s["batch_number"],
            "tablets_remaining": s["tablets_remaining"],
        })

    # 3. Expired with stock remaining
    expired_res = db.table("medicine_stock").select("id, medicine_id, expiry_date, batch_number, tablets_remaining, clinic_medicines(name, is_active)").eq("is_active", True).lt("expiry_date", today).gt("tablets_remaining", 0).execute()
    expired = []
    for s in (expired_res.data or []):
        med_info = s.get("clinic_medicines") or {}
        if med_info.get("is_active") is False:
            continue
        expired.append({
            "id": s["medicine_id"],
            "batch_id": s["id"],
            "name": med_info.get("name", ""),
            "expiry_date": s["expiry_date"],
            "batch_number": s["batch_number"],
            "tablets_remaining": s["tablets_remaining"],
        })

    total_alerts = len(low_stock) + len(expiring_soon) + len(expired)
    return {
        "low_stock": low_stock,
        "expiring_soon": expiring_soon,
        "expired": expired,
        "summary": {
            "low_stock_count": len(low_stock),
            "expiring_soon_count": len(expiring_soon),
            "expired_count": len(expired),
            "total_alerts": total_alerts,
        },
    }


# ── PRESCRIPTIONS WRITE ───────────────────────────────────

@app.post("/prescriptions/write")
async def write_prescription(request: Request):
    from database import supabase as db
    import datetime as dt
    import pytz

    body = await request.json()
    patient_id       = body["patient_id"]
    doctor_id_req    = body.get("doctor_id", "8c33abe0-5d2e-4613-9437-c7c375e8d162")
    appointment_id   = body.get("appointment_id") or None
    chief_complaint  = body.get("chief_complaint", "")
    diagnosis        = body.get("diagnosis", "")
    notes            = body.get("notes", "")
    dietary          = body.get("dietary_instructions", "")
    precautions      = body.get("precautions", "")
    medicines_input  = body.get("medicines", [])

    IST = pytz.timezone("Asia/Kolkata")
    now_ist = dt.datetime.now(IST)
    today_str = now_ist.date().isoformat()

    # 1. Fetch patient
    pat_res = db.table("patients").select("id, name, mobile, patient_code, language").eq("id", patient_id).execute()
    if not pat_res.data:
        return {"error": "Patient not found"}, 404
    patient = pat_res.data[0]

    # 2. Create visit
    visit_res = db.table("visits").insert({
        "patient_id":      patient_id,
        "doctor_id":       doctor_id_req,
        "appointment_id":  appointment_id,
        "chief_complaint": chief_complaint,
        "diagnosis":       diagnosis,
        "notes":           notes,
        "visit_status":    "Completed",
        "created_at":      now_ist.isoformat(),
    }).execute()
    visit = visit_res.data[0] if visit_res.data else {}
    visit_id = visit.get("id", "")

    # 3. Create prescription
    pres_res = db.table("prescriptions").insert({
        "patient_id":           patient_id,
        "doctor_id":            doctor_id_req,
        "visit_id":             visit_id,
        "prescription_date":    today_str,
        "dietary_instructions": dietary,
        "precautions":          precautions,
        "general_notes":        notes,
        "followup_whatsapp_sent": False,
        "followup_replied":     False,
        "followup_call_sent":   False,
        "created_at":           now_ist.isoformat(),
    }).execute()
    pres = pres_res.data[0] if pres_res.data else {}
    pres_id = pres.get("id", "")

    # 4. Insert medicines
    med_rows = []
    for i, m in enumerate(medicines_input):
        if not m.get("medicine_name", "").strip():
            continue
        med_rows.append({
            "prescription_id": pres_id,
            "medicine_name":   m["medicine_name"],
            "dosage":          m.get("dosage", ""),
            "morning":         m.get("morning", False),
            "afternoon":       m.get("afternoon", False),
            "evening":         m.get("evening", False),
            "night":           m.get("night", False),
            "before_food":     m.get("before_food", False),
            "duration_days":   m.get("duration_days", 5),
            "instructions":    m.get("instructions", ""),
            "sort_order":      m.get("sort_order", i + 1),
        })
    if med_rows:
        db.table("prescription_medicines").insert(med_rows).execute()

    # 5. Create followup record (days from today driven by clinic.followup_days config)
    _fu_cfg = supabase.table("clinic_config").select("config_value").eq("doctor_id", doctor_id_req).eq("config_key", "clinic.followup_days").execute()
    _fu_days = int((_fu_cfg.data[0]["config_value"] if _fu_cfg.data else None) or 7)
    followup_date = (now_ist.date() + dt.timedelta(days=_fu_days)).isoformat()
    try:
        db.table("followups").insert({
            "patient_id":    patient_id,
            "doctor_id":     doctor_id_req,
            "visit_id":      visit_id,
            "scheduled_date": followup_date,
            "channel":       "whatsapp",
            "call_status":   "Pending",
            "followup_day":  7,
        }).execute()
    except Exception as fe:
        print(f"⚠️ Followup insert error: {fe}")

    # 6. Send WhatsApp prescription summary
    whatsapp_sent = False
    try:
        mobile   = patient.get("mobile", "")
        pname    = patient.get("name", "Patient")
        pcode    = patient.get("patient_code", "")
        language = patient.get("language", "english")

        # Build medicine lines
        timing_icons = {"morning": "🌅", "afternoon": "☀️", "evening": "🌆", "night": "🌙"}
        timing_labels_en = {"morning": "Morning", "afternoon": "Afternoon", "evening": "Evening", "night": "Night"}
        timing_labels_ta = {"morning": "காலை", "afternoon": "மதியம்", "evening": "மாலை", "night": "இரவு"}

        def med_line(m, lang, idx):
            timings_keys = [k for k in ["morning", "afternoon", "evening", "night"] if m.get(k)]
            td = m.get("timing_details") or {}
            dur_days = m.get('duration_days', 5)
            inst = f"\n   📝 {m['instructions']}" if m.get("instructions") else ""
            if td:
                parts = []
                for k in timings_keys:
                    t = td.get(k, {})
                    qty = t.get("qty", 1)
                    bf = t.get("before_food", False)
                    lbl = timing_labels_ta[k] if lang == "tamil" else timing_labels_en[k]
                    food_str = ("முன்" if bf else "பின்") if lang == "tamil" else ("before food" if bf else "after food")
                    parts.append(f"{timing_icons[k]} {lbl}: {qty} tab(s) {food_str}")
                timing_str = "\n   ".join(parts)
                dur = f"{dur_days} {'நாட்கள்' if lang == 'tamil' else 'days'}"
                return f"{idx}. {m['medicine_name']} — {m.get('dosage','')}\n   {timing_str}\n   ⏱ {dur}{inst}"
            else:
                icons_str = " + ".join(timing_icons[k] for k in timings_keys)
                qty = m.get("qty_per_dose", 1) or 1
                qty_str = f" × {qty} tab(s)"
                if lang == "tamil":
                    labels = " + ".join(timing_labels_ta[k] for k in timings_keys)
                    food = "சாப்பிடுவதற்கு முன்" if m.get("before_food") else "சாப்பிட்ட பின்"
                    dur = f"{dur_days} நாட்கள்"
                else:
                    labels = " + ".join(timing_labels_en[k] for k in timings_keys)
                    food = "Before food" if m.get("before_food") else "After food"
                    dur = f"{dur_days} days"
                return f"{idx}. {m['medicine_name']} — {m.get('dosage','')}{qty_str}\n   {icons_str} {labels} | {food} | {dur}{inst}"

        med_lines = "\n\n".join(med_line(m, language, i+1) for i, m in enumerate(medicines_input) if m.get("medicine_name","").strip())

        _doc_res = supabase.table("doctors").select("clinic_name, name").eq("id", doctor_id_req).limit(1).execute()
        _doc_row = (_doc_res.data or [{}])[0]
        _clinic_name = _doc_row.get("clinic_name") or "TrueCare Family Clinic"

        if language == "tamil":
            msg = (
                f"💊 *மருந்துச் சீட்டு*\n"
                f"🏥 {_clinic_name}\n\n"
                f"நோயாளி: {pname}" + (f" ({pcode})" if pcode else "") + f"\n"
                f"தேதி: {now_ist.strftime('%d %b %Y')}\n"
                f"நோய்: {diagnosis}\n\n"
                f"மருந்துகள்:\n{med_lines}"
            )
            if dietary:
                msg += f"\n\n🥗 உணவு: {dietary}"
            if precautions:
                msg += f"\n⚠️ எச்சரிக்கை: {precautions}"
            msg += f"\n\nFollow-up: 3 நாட்களில் வரவும்.\nகேள்விகளுக்கு MENU என்று reply பண்ணுங்கள்."
        else:
            msg = (
                f"💊 *Your Prescription*\n"
                f"🏥 {_clinic_name}\n\n"
                f"Patient: {pname}" + (f" ({pcode})" if pcode else "") + f"\n"
                f"Date: {now_ist.strftime('%d %b %Y')}\n"
                f"Diagnosis: {diagnosis}\n\n"
                f"Medicines:\n{med_lines}"
            )
            if dietary:
                msg += f"\n\n🥗 Diet: {dietary}"
            if precautions:
                msg += f"\n⚠️ Precautions: {precautions}"
            msg += f"\n\nFollow-up in 3 days if not improving.\nReply MENU for any help."

        if mobile:
            await send_meta_text(mobile, msg)
            whatsapp_sent = True

    except Exception as we:
        print(f"⚠️ WhatsApp send error: {we}")

    return {
        "prescription_id": pres_id,
        "visit_id": visit_id,
        "whatsapp_sent": whatsapp_sent,
        "patient_name": patient.get("name", ""),
    }


# ── CLINIC CONFIG ─────────────────────────────────────────

@app.get("/config/{doctor_id}")
async def get_config(doctor_id: str):
    """Return all config rows for a doctor as typed dict."""
    result = config_loader._sb.table("clinic_config") \
        .select("config_key, config_value, config_type, description, updated_at") \
        .eq("doctor_id", doctor_id) \
        .order("config_key") \
        .execute()
    return result.data or []


SLOT_SENSITIVE_KEYS = {
    "clinic.slot_start_morning",
    "clinic.slot_end_morning",
    "clinic.slot_start_evening",
    "clinic.slot_end_evening",
    "clinic.slot_duration_minutes",
}

@app.patch("/config/{doctor_id}/{config_key}")
async def update_config(doctor_id: str, config_key: str, request: Request):
    """Upsert a single config key for a doctor."""
    import datetime as dt
    from fastapi.responses import JSONResponse
    body = await request.json()
    config_value = body.get("config_value", "")

    # Guard: block slot-related changes when active appointments exist today or in future
    if config_key in SLOT_SENSITIVE_KEYS:
        today = dt.date.today().isoformat()
        active = config_loader._sb.table("appointments")\
            .select("id", count="exact")\
            .eq("doctor_id", doctor_id)\
            .gte("appointment_date", today)\
            .in_("status", ["Confirmed", "In Progress"])\
            .execute()
        count = active.count or 0
        if count > 0:
            return JSONResponse(
                status_code=409,
                content={
                    "error": "active_appointments",
                    "message": f"Cannot change slot settings — {count} active appointment{'s' if count != 1 else ''} exist{'s' if count == 1 else ''} today or in future. Cancel or complete them first.",
                    "count": count,
                }
            )

    result = config_loader._sb.table("clinic_config").upsert({
        "doctor_id": doctor_id,
        "config_key": config_key,
        "config_value": str(config_value),
        "updated_at": dt.datetime.utcnow().isoformat(),
    }, on_conflict="doctor_id,config_key").execute()
    # Invalidate in-process cache
    config_loader.invalidate_cache()
    return result.data[0] if result.data else {}


@app.post("/config/reload-scheduler")
async def reload_scheduler_endpoint():
    """Invalidate config cache and reschedule all jobs with fresh DB config."""
    global _scheduler
    if _scheduler is None:
        return {"status": "error", "message": "Scheduler not initialized"}
    await reschedule(_scheduler)
    return {"status": "ok", "message": "Scheduler reloaded from DB config"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)


# ── DASHBOARD ─────────────────────────────────────────────
@app.get("/dashboard/stats")
async def dashboard_stats(doctor_id: str):
    from database import supabase
    today = datetime.now(IST).date().isoformat()

    today_appts = supabase.table("appointments").select("id", count="exact").eq("doctor_id", doctor_id).eq("appointment_date", today).neq("status", "Cancelled").neq("consultation_type", "online").execute()
    # tokens uses queue_date, not appointment_date
    token_row = supabase.table("tokens").select("current_token").eq("doctor_id", doctor_id).eq("queue_date", today).execute()
    # patients table has no doctor_id — single-clinic deployment, count all patients
    all_patients = supabase.table("patients").select("id", count="exact").execute()
    total_patients = all_patients.count or 0
    # followups table (no underscore), filter by call_status not status
    pending_followups = supabase.table("followups").select("id", count="exact").eq("doctor_id", doctor_id).is_("completed_at", "null").execute()
    current_token_val = token_row.data[0]["current_token"] if token_row.data else 0

    # Completed/serving derived from appointment statuses + slot-time order,
    # same rule as /queue/status (current_token alone resets to 0 at day end)
    day_rows = supabase.table("appointments").select(
        "token_number, appointment_time, status"
    ).eq("doctor_id", doctor_id).eq("appointment_date", today).neq("consultation_type", "online").execute().data or []

    curr = next((a for a in day_rows if current_token_val and a.get("token_number") == current_token_val), None)
    serving_time = _time_str(curr.get("appointment_time")) if curr else ""

    def _is_done(a):
        if a.get("status") == "Completed":
            return True
        if a.get("status") == "Cancelled":
            return False
        t = _time_str(a.get("appointment_time"))
        return bool(serving_time and t and t < serving_time)

    today_completed_count = sum(1 for a in day_rows if _is_done(a))

    current_display_token = None
    if curr:
        current_display_token = get_display_token(
            current_token_val, curr.get("appointment_time"),
            doctor_id=doctor_id, date_str=today,
        )

    week_map = defaultdict(int)
    for i in range(6, -1, -1):
        week_map[(date.today() - timedelta(days=i)).isoformat()] = 0
    week_appts = supabase.table("appointments").select("appointment_date").eq("doctor_id", doctor_id).gte("appointment_date", (date.today() - timedelta(days=6)).isoformat()).lte("appointment_date", today).execute()
    for row in (week_appts.data or []):
        week_map[row["appointment_date"]] += 1
    weekly = [{"date": d, "count": c} for d, c in sorted(week_map.items())]

    # diagnosis lives in visits table, not prescriptions
    visits = supabase.table("visits").select("diagnosis").eq("doctor_id", doctor_id).execute()
    diag_map = defaultdict(int)
    for row in (visits.data or []):
        if row.get("diagnosis"):
            diag_map[row["diagnosis"]] += 1
    top_diagnoses = sorted([{"diagnosis": k, "count": v} for k, v in diag_map.items()], key=lambda x: -x["count"])[:5]

    return {
        "today_appointments": today_appts.count or 0,
        "current_token": current_token_val,
        "current_display_token": current_display_token,
        "total_patients": total_patients,
        "pending_followups": pending_followups.count or 0,
        "today_completed": today_completed_count,
        "weekly_appointments": weekly,
        "top_diagnoses": top_diagnoses,
    }


# ── PATIENTS ──────────────────────────────────────────────
@app.get("/patients")
async def list_patients(doctor_id: str, search: str = ""):
    from database import supabase
    # patients table has no doctor_id — single-clinic deployment, return all patients
    q = supabase.table("patients").select("*")
    if search:
        # match family members registered under another head's mobile too
        q = q.or_(f"name.ilike.%{search}%,mobile.ilike.%{search}%,family_head_mobile.ilike.%{search}%,patient_code.ilike.%{search}%")
    result = q.order("created_at", desc=True).execute()
    return result.data or []


@app.get("/patients/family/{head_mobile}")
async def family_members(head_mobile: str):
    from database import supabase
    result = supabase.table("patients").select("*").eq("family_head_mobile", head_mobile).execute()
    return result.data or []


@app.get("/patients/lookup")
async def lookup_patient(mobile: str):
    """Look up patients by mobile number (with or without 91 prefix)."""
    from database import supabase
    m = mobile.strip().lstrip("+")
    candidates = {m}
    if m.startswith("91") and len(m) > 10:
        candidates.add(m[2:])
    else:
        candidates.add("91" + m)
    results = []
    for candidate in candidates:
        res = supabase.table("patients")\
            .select("*")\
            .or_(f"mobile.eq.{candidate},family_head_mobile.eq.{candidate}")\
            .execute()
        for p in (res.data or []):
            if not any(r["id"] == p["id"] for r in results):
                results.append(p)
    return results


@app.post("/patients/register")
async def register_patient(request: Request):
    """Register a new patient and generate patient_code."""
    from database import supabase
    body = await request.json()

    name       = (body.get("name") or "").strip()
    mobile_raw = (body.get("mobile") or "").strip().lstrip("+")
    dob        = body.get("date_of_birth") or ""
    gender     = body.get("gender") or ""
    language   = body.get("language") or ""
    email      = body.get("email") or None
    city       = body.get("city") or None
    fhm        = body.get("family_head_mobile") or None
    doctor_id  = body.get("doctor_id") or ""

    name_clean = name.replace(" ", "")
    prefix = name_clean[:3].upper() if len(name_clean) >= 3 else name_clean.upper().ljust(3, "X")
    suffix = mobile_raw[-4:] if len(mobile_raw) >= 4 else mobile_raw
    year   = dob[:4] if len(dob) >= 4 else "0000"
    base_code = f"{prefix}-{suffix}-{year}"

    # Fetch all codes and filter in Python (.like() triggers Cloudflare 1101 on this deployment)
    existing = supabase.table("patients").select("patient_code").execute()
    existing_codes = {r["patient_code"] for r in (existing.data or []) if (r.get("patient_code") or "").startswith(base_code)}
    code = base_code
    counter = 2
    while code in existing_codes:
        code = f"{base_code}-{counter}"
        counter += 1

    age = None
    if dob:
        try:
            from datetime import date as _date
            born = _date.fromisoformat(dob)
            today = _date.today()
            age = today.year - born.year - ((today.month, today.day) < (born.month, born.day))
        except Exception:
            pass

    # Only columns that actually exist in the patients table (verified from schema)
    # Columns: id, patient_code, name, mobile, age, gender, date_of_birth,
    #          email, address, whatsapp_number, registration_source,
    #          created_at, updated_at, family_head_mobile, language
    row = {
        "name":         name,
        "mobile":       mobile_raw,
        "age":          age,
        "gender":       gender,
        "language":     language,
        "patient_code": code,
        "registration_source": "clinic",
    }
    if dob: row["date_of_birth"] = dob
    if fhm: row["family_head_mobile"] = fhm.lstrip("+")

    from fastapi import HTTPException
    result = supabase.table("patients").insert(row).execute()
    if not result.data:
        raise HTTPException(status_code=500, detail="Insert failed")
    patient = result.data[0]

    # Send WhatsApp welcome message
    is_family = bool(fhm)
    lang = language.lower()
    pat_name = patient.get("name") or name
    pat_code = patient.get("patient_code") or code

    if lang == "tamil":
        if is_family:
            msg = (
                f"👋 வணக்கம் {pat_name}!\n\n"
                f"🏥 TrueCare Family Clinic-ல் நீங்கள் பதிவு செய்யப்பட்டீர்கள்.\n"
                f"🪪 உங்கள் Patient ID: *{pat_code}*\n\n"
                f"சந்திப்பு பதிவு செய்ய MENU என்று reply பண்ணுங்கள்."
            )
        else:
            msg = (
                f"👋 வணக்கம் {pat_name}!\n\n"
                f"🏥 TrueCare Family Clinic-ல் உங்களை வரவேற்கிறோம்.\n"
                f"🪪 உங்கள் Patient ID: *{pat_code}*\n\n"
                f"இந்த ID-ஐ ஒவ்வொரு வருகையிலும் பயன்படுத்துங்கள்.\n"
                f"சந்திப்பு பதிவு செய்ய MENU என்று reply பண்ணுங்கள்."
            )
    else:
        if is_family:
            msg = (
                f"👋 Hello {pat_name}!\n\n"
                f"🏥 You have been registered at TrueCare Family Clinic.\n"
                f"🪪 Your Patient ID: *{pat_code}*\n\n"
                f"Reply MENU to book an appointment."
            )
        else:
            msg = (
                f"👋 Welcome to TrueCare Family Clinic, {pat_name}!\n\n"
                f"🪪 Your Patient ID: *{pat_code}*\n\n"
                f"Please save this ID — you'll need it at every visit.\n"
                f"Reply MENU to book an appointment."
            )

    wa_sent = False
    send_to = mobile_raw or (fhm.lstrip("+") if fhm else "")
    if send_to:
        try:
            await send_meta_text(send_to, msg)
            wa_sent = True
        except Exception as e:
            print(f"❌ WhatsApp welcome failed: {e}")

    patient["whatsapp_sent"] = wa_sent
    return patient


@app.get("/patients/search")
async def search_patient_by_mobile(mobile: str):
    from database import supabase
    normalized = mobile.lstrip("+")
    if normalized.startswith("91") and len(normalized) == 12:
        normalized = normalized[2:]
    with_prefix = "91" + normalized

    all_patients = supabase.table("patients").select("id, name, age, mobile, patient_code, gender").execute()
    matches = []
    for p in (all_patients.data or []):
        pm = (p.get("mobile") or "").lstrip("+")
        pm_short = pm[2:] if (pm.startswith("91") and len(pm) == 12) else pm
        if pm_short == normalized or pm == with_prefix or pm == normalized:
            matches.append({
                "patient_id": p["id"],
                "name": p["name"],
                "age": p.get("age"),
                "gender": p.get("gender"),
                "mobile": p["mobile"],
                "patient_code": p.get("patient_code"),
                "last_visit_date": None,
            })

    if not matches:
        raise HTTPException(status_code=404, detail="Patient not found")
    return matches


@app.get("/patients/{patient_id}")
async def get_patient(patient_id: str):
    from database import supabase
    result = supabase.table("patients").select("*").eq("id", patient_id).single().execute()
    return result.data


@app.patch("/patients/{patient_id}")
async def update_patient(patient_id: str, request: Request):
    from database import supabase
    import datetime as dt
    body = await request.json()
    # Only update columns that actually exist in the schema
    allowed = {"name", "mobile", "age", "gender", "language", "date_of_birth",
               "email", "address", "family_head_mobile"}
    update_data = {k: v for k, v in body.items() if k in allowed and v is not None}
    update_data["updated_at"] = dt.datetime.utcnow().isoformat()
    result = supabase.table("patients").update(update_data).eq("id", patient_id).execute()
    return result.data[0] if result.data else {}


@app.get("/patients/{patient_id}/visits")
async def get_patient_visits(patient_id: str):
    from database import supabase
    result = supabase.table("visits") \
        .select("*, appointments(appointment_date, token_number)") \
        .eq("patient_id", patient_id) \
        .order("created_at", desc=True) \
        .limit(5) \
        .execute()
    return result.data or []


# ── VISITS & VITALS ───────────────────────────────────────
@app.post("/visits")
async def create_visit(request: Request):
    """Create a bare visit (In Progress) so vitals can be recorded before the
    prescription is saved. POST /prescriptions completes it with diagnosis."""
    body = await request.json()
    patient_id = body.get("patient_id")
    if not patient_id:
        raise HTTPException(status_code=400, detail="patient_id required")
    now_ist = datetime.now(IST)
    result = supabase.table("visits").insert({
        "patient_id":     patient_id,
        "doctor_id":      body.get("doctor_id", "8c33abe0-5d2e-4613-9437-c7c375e8d162"),
        "appointment_id": body.get("appointment_id") or None,
        "visit_status":   "In Progress",
        "visit_date":     now_ist.date().isoformat(),
        "created_at":     now_ist.isoformat(),
    }).execute()
    if not result.data:
        raise HTTPException(status_code=500, detail="Visit insert failed")
    return result.data[0]


_VITAL_INT_FIELDS = {"spo2_percent", "bp_systolic", "bp_diastolic", "pulse_bpm"}
_VITAL_FLOAT_FIELDS = {"temperature_f", "weight_kg", "height_cm"}
_VITAL_FIELDS = [
    "temperature_f", "weight_kg", "height_cm",
    "spo2_percent", "bp_systolic", "bp_diastolic",
    "pulse_bpm", "key_findings",
]


@app.get("/visits/{visit_id}/vitals")
async def get_visit_vitals(visit_id: str):
    result = supabase.table("visit_vitals").select("*").eq(
        "visit_id", visit_id
    ).order("recorded_at", desc=True).limit(1).execute()
    return {"vitals": result.data[0] if result.data else None}


@app.post("/visits/{visit_id}/vitals")
async def save_visit_vitals(visit_id: str, request: Request):
    body = await request.json()

    visit = supabase.table("visits").select("patient_id, doctor_id").eq(
        "id", visit_id).limit(1).execute()
    if not visit.data:
        raise HTTPException(status_code=404, detail="Visit not found")

    vitals_data = {
        "visit_id": visit_id,
        "patient_id": visit.data[0]["patient_id"],
        "doctor_id": visit.data[0]["doctor_id"],
        "recorded_by_role": body.get("recorded_by_role", "doctor"),
        "updated_at": datetime.now(IST).isoformat(),
    }

    # Fields present in the body are written; "" / None clears the value.
    # Fields absent from the body are left untouched on update.
    for field in _VITAL_FIELDS:
        if field not in body:
            continue
        v = body[field]
        if v in (None, ""):
            vitals_data[field] = None
        elif field in _VITAL_INT_FIELDS:
            try:
                vitals_data[field] = int(float(v))
            except (TypeError, ValueError):
                continue
        elif field in _VITAL_FLOAT_FIELDS:
            try:
                vitals_data[field] = float(v)
            except (TypeError, ValueError):
                continue
        else:
            vitals_data[field] = v

    existing = supabase.table("visit_vitals").select("id").eq(
        "visit_id", visit_id).limit(1).execute()
    if existing.data:
        result = supabase.table("visit_vitals").update(vitals_data).eq(
            "visit_id", visit_id).execute()
    else:
        vitals_data["recorded_at"] = datetime.now(IST).isoformat()
        result = supabase.table("visit_vitals").insert(vitals_data).execute()

    return {"success": True, "vitals": result.data[0] if result.data else None}


@app.get("/patients/{patient_id}/vitals-history")
async def get_patient_vitals_history(patient_id: str):
    result = supabase.table("visit_vitals").select(
        "*, visits(visit_date, chief_complaint)"
    ).eq("patient_id", patient_id).order("recorded_at", desc=True).limit(5).execute()
    return {"history": result.data or []}


# ── APPOINTMENTS ──────────────────────────────────────────
def _annotate_display_tokens(appointments: list, doctor_id: str) -> list:
    """Attach display_token (M1/E1…) and time-order queue_status to rows.
    Done/In Progress/Waiting follow slot time relative to the serving slot."""
    dates = {a.get("appointment_date") for a in appointments if a.get("appointment_date")}
    serving = {}  # date -> (current_token, serving_time)
    for d in dates:
        tk = supabase.table("tokens").select("current_token").eq(
            "doctor_id", doctor_id).eq("queue_date", d).execute()
        cur = tk.data[0]["current_token"] if tk.data else 0
        st = ""
        if cur:
            row = supabase.table("appointments").select("appointment_time").eq(
                "doctor_id", doctor_id).eq("appointment_date", d).eq(
                "token_number", cur).limit(1).execute()
            st = _time_str(row.data[0]["appointment_time"]) if row.data else ""
        serving[d] = (cur, st)

    for a in appointments:
        a["display_token"] = get_display_token(
            a.get("token_number"), a.get("appointment_time"),
            doctor_id=a.get("doctor_id"), date_str=str(a.get("appointment_date", ""))[:10],
        ) if a.get("appointment_time") else None

        cur, st = serving.get(a.get("appointment_date"), (0, ""))
        t = _time_str(a.get("appointment_time"))
        if a.get("status") == "Cancelled":
            a["queue_status"] = "Cancelled"
        elif a.get("status") == "No-Show":
            a["queue_status"] = "No-Show"
        elif a.get("status") == "Completed":
            a["queue_status"] = "Done"
        elif a.get("status") == "Late":
            a["queue_status"] = "Late"
        elif (cur and a.get("token_number") == cur) or a.get("status") == "In Progress":
            a["queue_status"] = "In Progress"
        elif a.get("returned_at"):
            a["queue_status"] = "Waiting"
        elif st and t and t < st:
            a["queue_status"] = "Done"
        else:
            a["queue_status"] = "Waiting"
    return appointments


@app.get("/appointments/today")
async def today_appointments(doctor_id: str):
    from database import supabase
    today = datetime.now(IST).date().isoformat()
    result = supabase.table("appointments").select("*, patients(*)").eq("doctor_id", doctor_id).eq("appointment_date", today).neq("consultation_type", "online").order("appointment_time", desc=False).execute()
    return _annotate_display_tokens(result.data or [], doctor_id)


@app.get("/appointments")
async def list_appointments(doctor_id: str, date: str = "", date_from: str = "", date_to: str = "", patient_id: str = ""):
    from database import supabase
    q = supabase.table("appointments").select("*, patients(*)").eq("doctor_id", doctor_id).neq("consultation_type", "online")
    if patient_id:
        q = q.eq("patient_id", patient_id)
    if date:
        q = q.eq("appointment_date", date)
    elif date_from and date_to:
        q = q.gte("appointment_date", date_from).lte("appointment_date", date_to)
    result = q.order("appointment_date", desc=True).order("appointment_time", desc=False).limit(50).execute()
    return _annotate_display_tokens(result.data or [], doctor_id)


@app.patch("/appointments/{appointment_id}/status")
async def update_appointment_status(appointment_id: str, request: Request):
    from database import supabase
    import datetime as dt
    body = await request.json()
    new_status = body.get("status")

    VALID_STATUSES = {"Confirmed", "In Progress", "Completed", "Cancelled", "No-Show", "Late"}
    if new_status not in VALID_STATUSES:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail=f"Invalid status: {new_status}")

    update_payload: dict = {"status": new_status}

    if new_status == "Confirmed":
        # Revert from Late → Confirmed: set returned_at from body or now
        returned_at = body.get("returned_at")
        if returned_at:
            update_payload["returned_at"] = returned_at
        else:
            # Check if the appointment was previously Late before setting returned_at
            prev = supabase.table("appointments").select("status").eq("id", appointment_id).limit(1).execute()
            if prev.data and prev.data[0].get("status") == "Late":
                update_payload["returned_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    elif new_status == "Late":
        # Clear returned_at when marking Late
        update_payload["returned_at"] = None

    result = supabase.table("appointments").update(update_payload).eq("id", appointment_id).execute()
    return result.data[0] if result.data else {}


@app.post("/appointments/{appointment_id}/no-show")
async def mark_no_show(appointment_id: str, request: Request):
    from database import supabase
    import datetime as dt
    body = await request.json()
    send_whatsapp: bool = body.get("send_whatsapp", False)

    # 1. Fetch appointment + patient
    appt_row = supabase.table("appointments").select("*, patients(*)").eq("id", appointment_id).execute()
    if not appt_row.data:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Appointment not found")
    appt = appt_row.data[0]

    # 2. Update status
    supabase.table("appointments").update({"status": "No-Show"}).eq("id", appointment_id).execute()

    patient     = appt.get("patients") or {}
    patient_id  = appt.get("patient_id", "")
    doctor_id   = appt.get("doctor_id", "")
    appt_date   = appt.get("appointment_date", "")
    appt_time   = appt.get("appointment_time", "")
    mobile      = patient.get("mobile", "")
    patient_name = patient.get("name", "Patient")
    first_name  = patient_name.split()[0] if patient_name else "there"

    # Format time for messages
    time_str = ""
    if appt_time:
        try:
            h = int(appt_time[:2]); m = appt_time[3:5]
            h12 = h % 12 or 12; ampm = "PM" if h >= 12 else "AM"
            time_str = f"{h12}:{m} {ampm}"
        except Exception:
            time_str = appt_time

    # Fetch clinic/doctor name
    clinic_name = "the clinic"
    doctor_name = "the doctor"
    try:
        doc_row = supabase.table("doctors").select("clinic_name, name").eq("id", doctor_id).execute()
        if doc_row.data:
            clinic_name = doc_row.data[0].get("clinic_name") or clinic_name
            doctor_name = doc_row.data[0].get("name") or doctor_name
    except Exception:
        pass

    # 3. WhatsApp notification
    whatsapp_sent = False
    if send_whatsapp and mobile:
        msg = (
            f"Hi {first_name} 👋\n\n"
            f"We noticed you missed your appointment with {doctor_name} today"
            + (f" at {time_str}" if time_str else "")
            + ".\n\n"
            f"We hope everything is okay 🙏\n\n"
            f"Please reply to this message to reschedule your appointment at your convenience.\n\n"
            f"— {clinic_name} Team"
        )
        try:
            await send_meta_text(mobile, msg)
            whatsapp_sent = True
        except Exception as e:
            print(f"[no-show] WhatsApp failed for {appointment_id}: {e}")

    # 4. Create followup record
    followup_created = False
    today = dt.date.today().isoformat()
    notes = f"Patient did not show up for appointment on {appt_date}"
    if time_str:
        notes += f" at {time_str}"
    notes += ". Call to reschedule."
    try:
        supabase.table("followups").insert({
            "patient_id":     patient_id,
            "doctor_id":      doctor_id,
            "appointment_id": appointment_id,
            "scheduled_date": today,
            "channel":        "call",
            "call_status":    "Pending",
            "notes":          notes,
        }).execute()
        followup_created = True
    except Exception as e:
        print(f"[no-show] Followup insert failed for {appointment_id}: {e}")

    return {
        "success":          True,
        "appointment_id":   appointment_id,
        "status":           "No-Show",
        "whatsapp_sent":    whatsapp_sent,
        "followup_created": followup_created,
    }


@app.post("/appointments/bulk-cancel")
async def bulk_cancel_appointments(request: Request):
    from database import supabase
    body = await request.json()
    appointment_ids: list = body.get("appointment_ids", [])
    reason: str = body.get("reason", "doctor_unavailable")
    notify_whatsapp: bool = body.get("notify_whatsapp", True)

    cancelled = []
    failed = []
    whatsapp_sent = 0
    whatsapp_failed = 0

    for appt_id in appointment_ids:
        try:
            upd = supabase.table("appointments").update({
                "status": "Cancelled",
                "cancellation_reason": reason,
            }).eq("id", appt_id).execute()
            if not upd.data:
                failed.append(appt_id)
                continue
            cancelled.append(appt_id)
            row = supabase.table("appointments").select("*, patients(*)").eq("id", appt_id).execute()
            appt = row.data[0] if row.data else {}

            if notify_whatsapp:
                patient = appt.get("patients") or {}
                mobile = patient.get("mobile", "")
                patient_name = patient.get("name", "Patient")
                appt_date = appt.get("appointment_date", "")
                appt_time = appt.get("appointment_time", "")

                # Format date and time for message
                try:
                    from datetime import datetime
                    date_obj = datetime.strptime(appt_date, "%Y-%m-%d")
                    date_str = date_obj.strftime("%d %b %Y")
                except Exception:
                    date_str = appt_date

                if appt_time:
                    try:
                        h = int(appt_time[:2])
                        m = appt_time[3:5]
                        h12 = h % 12 or 12
                        ampm = "PM" if h >= 12 else "AM"
                        time_str = f"{h12}:{m} {ampm}"
                    except Exception:
                        time_str = appt_time
                else:
                    time_str = ""

                clinic_name = "the clinic"
                try:
                    doc_row = supabase.table("doctors").select("clinic_name").eq("id", appt.get("doctor_id", "")).execute()
                    if doc_row.data:
                        clinic_name = doc_row.data[0].get("clinic_name") or clinic_name
                except Exception:
                    pass

                msg = (
                    f"Hi {patient_name} 👋\n\n"
                    f"Your appointment at {clinic_name} on {date_str}"
                    + (f" at {time_str}" if time_str else "")
                    + f" has been cancelled.\n\n"
                    f"We apologise for the inconvenience. Please reply to reschedule.\n\n"
                    f"— {clinic_name} Team"
                )

                if mobile:
                    try:
                        await send_meta_text(mobile, msg)
                        whatsapp_sent += 1
                    except Exception as e:
                        print(f"[bulk-cancel] WhatsApp failed for {appt_id}: {e}")
                        whatsapp_failed += 1
                else:
                    whatsapp_failed += 1

        except Exception as e:
            print(f"[bulk-cancel] Failed to cancel {appt_id}: {e}")
            failed.append(appt_id)

    return {
        "cancelled": cancelled,
        "failed": failed,
        "whatsapp_sent": whatsapp_sent,
        "whatsapp_failed": whatsapp_failed,
    }


# ── QUEUE ─────────────────────────────────────────────────
@app.get("/queue/status")
async def queue_status(doctor_id: str, date: str = ""):
    from database import supabase
    import datetime as dt
    d = date if date else dt.date.today().isoformat()
    # tokens uses queue_date
    token_row = supabase.table("tokens").select("current_token").eq("doctor_id", doctor_id).eq("queue_date", d).execute()
    current = token_row.data[0]["current_token"] if token_row.data else 0

    appts = supabase.table("appointments").select("*, patients(*)").eq("doctor_id", doctor_id).eq("appointment_date", d).neq("consultation_type", "online").order("appointment_time", desc=False).execute()
    all_appts = appts.data or []

    # Self-heal stale queue session: if the token being served no longer exists
    # in today's appointments (e.g. appointments were deleted), reset to 0.
    if current and not any((a.get("token_number") or 0) == current for a in all_appts):
        current = 0
        supabase.table("tokens").update({"current_token": 0}).eq(
            "doctor_id", doctor_id).eq("queue_date", d).execute()

    # current_token identifies the appointment being served; progress through
    # the queue follows slot TIME order (slot-position tokens), not int order.
    curr_appt = next((a for a in all_appts if current and (a.get("token_number") or 0) == current), None)
    serving_time = _time_str(curr_appt.get("appointment_time")) if curr_appt else ""

    for a in all_appts:
        a["display_token"] = get_display_token(
            a.get("token_number"), a.get("appointment_time"),
            doctor_id=a.get("doctor_id"), date_str=str(a.get("appointment_date", ""))[:10],
        ) if a.get("token_number") else None

        t = _time_str(a.get("appointment_time"))
        if a.get("status") == "Cancelled":
            a["queue_status"] = "Cancelled"
        elif a.get("status") == "Completed":
            a["queue_status"] = "Done"
        elif (current and a.get("token_number") == current) or a.get("status") == "In Progress":
            a["queue_status"] = "In Progress"
        elif serving_time and t and t < serving_time:
            a["queue_status"] = "Done"
        else:
            a["queue_status"] = "Waiting"

    current_display = curr_appt["display_token"] if curr_appt else None

    waiting = [a for a in all_appts if a["queue_status"] == "Waiting"]
    seen    = [a for a in all_appts if a["queue_status"] == "Done" and a.get("status") != "Cancelled"]

    return {
        "current_token": current,
        "current_display": current_display,
        "total_today": len(all_appts),
        "waiting": len(waiting),
        "completed": len(seen),
        "appointments": all_appts,
    }


@app.post("/queue/next")
async def queue_next(request: Request):
    from database import supabase
    import datetime as dt
    body = await request.json()
    doctor_id = body["doctor_id"]
    today = dt.date.today().isoformat()
    token_row = supabase.table("tokens").select("current_token").eq("doctor_id", doctor_id).eq("queue_date", today).execute()
    new_token = (token_row.data[0]["current_token"] if token_row.data else 0) + 1
    if token_row.data:
        supabase.table("tokens").update({"current_token": new_token}).eq("doctor_id", doctor_id).eq("queue_date", today).execute()
    else:
        supabase.table("tokens").insert({"doctor_id": doctor_id, "queue_date": today, "current_token": new_token}).execute()
    return {"token": new_token}


@app.post("/queue/prev")
async def queue_prev(request: Request):
    from database import supabase
    import datetime as dt
    body = await request.json()
    doctor_id = body["doctor_id"]
    today = dt.date.today().isoformat()
    token_row = supabase.table("tokens").select("current_token").eq("doctor_id", doctor_id).eq("queue_date", today).execute()
    current = token_row.data[0]["current_token"] if token_row.data else 1
    new_token = max(1, current - 1)
    if token_row.data:
        supabase.table("tokens").update({"current_token": new_token}).eq("doctor_id", doctor_id).eq("queue_date", today).execute()
    else:
        supabase.table("tokens").insert({"doctor_id": doctor_id, "queue_date": today, "current_token": new_token}).execute()
    return {"token": new_token}


@app.post("/queue/set-token")
async def queue_set_token(request: Request):
    from database import supabase
    import datetime as dt
    body = await request.json()
    doctor_id = body["doctor_id"]
    token = body["token"]
    today = dt.date.today().isoformat()
    token_row = supabase.table("tokens").select("current_token").eq("doctor_id", doctor_id).eq("queue_date", today).execute()
    if token_row.data:
        supabase.table("tokens").update({"current_token": token}).eq("doctor_id", doctor_id).eq("queue_date", today).execute()
    else:
        supabase.table("tokens").insert({"doctor_id": doctor_id, "queue_date": today, "current_token": token}).execute()
    return {"token": token}


# ── PRESCRIPTIONS ─────────────────────────────────────────
@app.get("/prescriptions/{prescription_id}/detail")
async def get_prescription_detail(prescription_id: str):
    from database import supabase
    result = supabase.table("prescriptions").select(
        "*, patients(id, name, mobile, patient_code, age, gender, language), prescription_medicines(*), visits(id, chief_complaint, diagnosis, notes)"
    ).eq("id", prescription_id).execute()
    if not result.data:
        return {}
    row = _decode_walkin(result.data[0])
    # For walk-in: inject complaint/diagnosis as a synthetic visits object so frontend can load them
    if not row.get("patient_id") and not row.get("visits") and row.get("walkin_complaint"):
        row["visits"] = {
            "id": None,
            "chief_complaint": row.get("walkin_complaint", ""),
            "diagnosis":       row.get("walkin_diagnosis", ""),
            "notes":           row.get("general_notes", ""),
        }
    return row


@app.put("/prescriptions/{prescription_id}")
async def update_prescription(prescription_id: str, request: Request, background_tasks: BackgroundTasks):
    from database import supabase
    body = await request.json()

    # 1. Update prescription fields
    supabase.table("prescriptions").update({
        "dietary_instructions": body.get("dietary_instructions", ""),
        "precautions":          body.get("precautions", ""),
        "general_notes":        body.get("notes", ""),
    }).eq("id", prescription_id).execute()

    # 2. Update visit (chief_complaint + diagnosis) if visit_id present
    visit_id = body.get("visit_id")
    if visit_id:
        supabase.table("visits").update({
            "chief_complaint": body.get("chief_complaint", ""),
            "diagnosis":       body.get("diagnosis", ""),
            "notes":           body.get("notes", ""),
        }).eq("id", visit_id).execute()

    # 3. Replace medicines: delete all, re-insert
    supabase.table("prescription_medicines").delete().eq("prescription_id", prescription_id).execute()
    medicines = body.get("medicines", [])
    med_rows = []
    for i, m in enumerate(medicines):
        if not m.get("medicine_name", "").strip():
            continue
        med_rows.append({
            "prescription_id": prescription_id,
            "medicine_name":   m["medicine_name"],
            "dosage":          m.get("dosage", ""),
            "morning":         m.get("morning", False),
            "afternoon":       m.get("afternoon", False),
            "evening":         m.get("evening", False),
            "night":           m.get("night", False),
            "before_food":     m.get("before_food", False),
            "duration_days":   m.get("duration_days", 5),
            "instructions":    m.get("instructions", ""),
            "sort_order":      m.get("sort_order", i + 1),
        })
    if med_rows:
        supabase.table("prescription_medicines").insert(med_rows).execute()

    # 4a. Update or create dispense order for updated prescription
    try:
        pres_res = supabase.table("prescriptions").select("doctor_id, patient_id").eq("id", prescription_id).limit(1).execute()
        pres_row = pres_res.data[0] if pres_res.data else {}
        doc_id = pres_row.get("doctor_id") or body.get("doctor_id", "")
        pat_id  = pres_row.get("patient_id") or body.get("patient_id")
        if doc_id:
            background_tasks.add_task(
                upsert_dispense_order,
                prescription_id=prescription_id,
                doctor_id=doc_id,
                patient_id=pat_id,
                patient_name=None,
                medicines=medicines,
            )
    except Exception as se:
        print(f"⚠️ Dispense order upsert error on update: {se}")

    # 4b. Fetch patient info and send WhatsApp with updated prescription
    whatsapp_sent = False
    try:
        import datetime as dt, pytz
        pat_res = supabase.table("patients").select("name, mobile, patient_code, language").eq("id", body.get("patient_id", "")).execute()
        patient = pat_res.data[0] if pat_res.data else {}
        mobile   = patient.get("mobile", "")
        pname    = patient.get("name", "Patient")
        pcode    = patient.get("patient_code", "")
        language = patient.get("language", "english")
        diagnosis = body.get("diagnosis", "")
        dietary   = body.get("dietary_instructions", "")
        precautions = body.get("precautions", "")

        IST = pytz.timezone("Asia/Kolkata")
        now_ist = dt.datetime.now(IST)

        timing_icons = {"morning": "🌅", "afternoon": "☀️", "evening": "🌆", "night": "🌙"}
        timing_labels_en = {"morning": "Morning", "afternoon": "Afternoon", "evening": "Evening", "night": "Night"}
        timing_labels_ta = {"morning": "காலை", "afternoon": "மதியம்", "evening": "மாலை", "night": "இரவு"}

        def med_line(m, lang, idx):
            timings_keys = [k for k in ["morning", "afternoon", "evening", "night"] if m.get(k)]
            td = m.get("timing_details") or {}
            dur_days = m.get('duration_days', 5)
            inst = f"\n   📝 {m['instructions']}" if m.get("instructions") else ""
            if td:
                parts = []
                for k in timings_keys:
                    t = td.get(k, {})
                    qty = t.get("qty", 1)
                    bf = t.get("before_food", False)
                    lbl = timing_labels_ta[k] if lang == "tamil" else timing_labels_en[k]
                    food_str = ("முன்" if bf else "பின்") if lang == "tamil" else ("before food" if bf else "after food")
                    parts.append(f"{timing_icons[k]} {lbl}: {qty} tab(s) {food_str}")
                timing_str = "\n   ".join(parts)
                dur = f"{dur_days} {'நாட்கள்' if lang == 'tamil' else 'days'}"
                return f"{idx}. {m['medicine_name']} — {m.get('dosage','')}\n   {timing_str}\n   ⏱ {dur}{inst}"
            else:
                icons_str = " + ".join(timing_icons[k] for k in timings_keys)
                qty = m.get("qty_per_dose", 1) or 1
                qty_str = f" × {qty} tab(s)"
                if lang == "tamil":
                    labels = " + ".join(timing_labels_ta[k] for k in timings_keys)
                    food = "சாப்பிடுவதற்கு முன்" if m.get("before_food") else "சாப்பிட்ட பின்"
                    dur = f"{dur_days} நாட்கள்"
                else:
                    labels = " + ".join(timing_labels_en[k] for k in timings_keys)
                    food = "Before food" if m.get("before_food") else "After food"
                    dur = f"{dur_days} days"
                return f"{idx}. {m['medicine_name']} — {m.get('dosage','')}{qty_str}\n   {icons_str} {labels} | {food} | {dur}{inst}"

        valid_meds = [m for m in medicines if m.get("medicine_name", "").strip()]
        med_lines = "\n\n".join(med_line(m, language, i+1) for i, m in enumerate(valid_meds))

        if language == "tamil":
            msg = (
                f"💊 *மருந்துச் சீட்டு (புதுப்பிக்கப்பட்டது)*\n"
                f"🏥 TrueCare Family Clinic\n\n"
                f"நோயாளி: {pname}" + (f" ({pcode})" if pcode else "") + f"\n"
                f"தேதி: {now_ist.strftime('%d %b %Y')}\n"
                f"நோய்: {diagnosis}\n\n"
                f"மருந்துகள்:\n{med_lines}"
            )
            if dietary:   msg += f"\n\n🥗 உணவு: {dietary}"
            if precautions: msg += f"\n⚠️ எச்சரிக்கை: {precautions}"
            msg += f"\n\nFollow-up: 3 நாட்களில் வரவும்."
        else:
            msg = (
                f"💊 *Updated Prescription*\n"
                f"🏥 TrueCare Family Clinic\n\n"
                f"Patient: {pname}" + (f" ({pcode})" if pcode else "") + f"\n"
                f"Date: {now_ist.strftime('%d %b %Y')}\n"
                f"Diagnosis: {diagnosis}\n\n"
                f"Medicines:\n{med_lines}"
            )
            if dietary:     msg += f"\n\n🥗 Diet: {dietary}"
            if precautions: msg += f"\n⚠️ Precautions: {precautions}"
            msg += f"\n\nFollow-up in 3 days if not improving.\nReply MENU for any help."

        if mobile:
            await send_meta_text(mobile, msg)
            whatsapp_sent = True

    except Exception as we:
        print(f"⚠️ WhatsApp send error on update: {we}")

    return {"ok": True, "prescription_id": prescription_id, "whatsapp_sent": whatsapp_sent}


@app.get("/prescriptions/active")
async def active_prescriptions(doctor_id: str):
    from database import supabase
    # prescriptions joins via visit_id → visits; filter by doctor_id directly on prescriptions
    result = supabase.table("prescriptions").select("*, patients(name, mobile, patient_code), prescription_medicines(*)").eq("doctor_id", doctor_id).order("created_at", desc=True).execute()
    return result.data or []


def _decode_walkin(row: dict) -> dict:
    """If general_notes has a WALKIN:: prefix, decode and inject walkin fields."""
    import json as _json, re as _re
    notes = row.get("general_notes") or ""
    m = _re.match(r"^WALKIN::(.+?)::END\n?(.*)", notes, _re.DOTALL)
    if m:
        try:
            meta = _json.loads(m.group(1))
            row["walkin_name"] = meta.get("name") or ""
            row["walkin_age"]  = meta.get("age")
            row["walkin_complaint"] = meta.get("complaint") or ""
            row["walkin_diagnosis"] = meta.get("diagnosis") or ""
            row["general_notes"] = m.group(2)  # strip prefix from notes
        except Exception:
            pass
    return row


@app.get("/prescriptions")
async def list_prescriptions(doctor_id: str, patient_id: str = ""):
    from database import supabase
    q = supabase.table("prescriptions").select("*, patients(name, mobile, patient_code), prescription_medicines(*)").eq("doctor_id", doctor_id)
    if patient_id:
        q = q.eq("patient_id", patient_id)
    result = q.order("created_at", desc=True).execute()
    return [_decode_walkin(r) for r in (result.data or [])]


async def deduct_stock_for_prescription(prescription_id: str, doctor_id: str, medicines: list):
    """FIFO stock deduction — runs as background task after prescription save."""
    from database import supabase as db
    try:
        for med in medicines:
            med_name = med.get("medicine_name", "").strip()
            if not med_name:
                continue
            med_id_direct = med.get("medicine_id")
            if med_id_direct:
                med_res = db.table("clinic_medicines").select("id, name, tablets_per_strip, low_stock_threshold").eq("id", med_id_direct).limit(1).execute()
            else:
                med_res = db.table("clinic_medicines").select("id, name, tablets_per_strip, low_stock_threshold").eq("doctor_id", doctor_id).eq("name", med_name).limit(1).execute()
                if not med_res.data:
                    med_res = db.table("clinic_medicines").select("id, name, tablets_per_strip, low_stock_threshold").eq("doctor_id", doctor_id).ilike("name", f"%{med_name}%").limit(1).execute()
            if not med_res.data:
                print(f"[STOCK DEDUCTION] Medicine not found: {med_name} (id={med_id_direct})")
                continue
            medicine = med_res.data[0]
            medicine_id = medicine["id"]

            td = med.get("timing_details") or {}
            if td:
                # Sum actual qty for each enabled timing
                daily_qty = sum(
                    float(v.get("qty", 1))
                    for k, v in td.items()
                    if med.get(k)
                )
            else:
                doses_per_day = sum([
                    1 if med.get("morning") else 0,
                    1 if med.get("afternoon") else 0,
                    1 if med.get("evening") else 0,
                    1 if med.get("night") else 0,
                ])
                daily_qty = doses_per_day * float(med.get("qty_per_dose") or 1)
            tablets_needed = round(daily_qty * int(med.get("duration_days") or 1))
            if tablets_needed <= 0:
                continue

            batches_res = db.table("medicine_stock").select("id, tablets_remaining, expiry_date, batch_number").eq("medicine_id", medicine_id).eq("is_active", True).gte("expiry_date", date.today().isoformat()).order("expiry_date", desc=False).execute()

            remaining_to_deduct = tablets_needed
            for batch in (batches_res.data or []):
                if remaining_to_deduct <= 0:
                    break
                available = batch["tablets_remaining"] or 0
                deduct_from_batch = min(available, remaining_to_deduct)
                new_remaining = available - deduct_from_batch
                db.table("medicine_stock").update({
                    "tablets_remaining": new_remaining,
                    "is_active": new_remaining > 0,
                    "updated_at": datetime.now(IST).isoformat(),
                }).eq("id", batch["id"]).execute()
                db.table("stock_transactions").insert({
                    "medicine_id": medicine_id,
                    "stock_batch_id": batch["id"],
                    "doctor_id": doctor_id,
                    "transaction_type": "dispensed",
                    "quantity_change": -deduct_from_batch,
                    "reference_id": prescription_id,
                    "notes": "Dispensed for prescription",
                    "created_at": datetime.now(IST).isoformat(),
                }).execute()
                remaining_to_deduct -= deduct_from_batch

            threshold = medicine.get("low_stock_threshold")
            if threshold is not None:
                final_res = db.table("medicine_stock").select("tablets_remaining").eq("medicine_id", medicine_id).eq("is_active", True).gte("expiry_date", date.today().isoformat()).execute()
                total_remaining = sum((r["tablets_remaining"] or 0) for r in (final_res.data or []))
                if total_remaining <= threshold:
                    print(f"[LOW STOCK ALERT] {medicine['name']}: {total_remaining} remaining (threshold: {threshold})")
    except Exception as e:
        print(f"[STOCK DEDUCTION ERROR] prescription {prescription_id}: {e}")


def _calc_qty_prescribed(m: dict) -> float:
    """Calculate total tablets for a full prescription course."""
    td = m.get("timing_details") or {}
    if td:
        daily = sum(float(v.get("qty", 1)) for k, v in td.items() if m.get(k))
    else:
        doses = sum(1 for k in ["morning", "afternoon", "evening", "night"] if m.get(k))
        daily = doses * float(m.get("qty_per_dose") or 1)
    return max(1.0, round(daily * int(m.get("duration_days") or 1), 2))


async def create_dispense_order(prescription_id: str, doctor_id: str, patient_id, patient_name, medicines: list):
    """Create a dispense order after prescription save — runs as background task."""
    from database import supabase as db
    try:
        order_res = db.table("dispense_orders").insert({
            "prescription_id": prescription_id,
            "doctor_id":       doctor_id,
            "patient_id":      patient_id,
            "patient_name":    patient_name,
            "status":          "pending",
        }).execute()
        order_id = order_res.data[0]["id"] if order_res.data else None
        if not order_id:
            return
        items = []
        for m in medicines:
            if not m.get("medicine_name", "").strip():
                continue
            items.append({
                "dispense_order_id": order_id,
                "medicine_id":       m.get("medicine_id"),
                "medicine_name":     m["medicine_name"],
                "dosage":            m.get("dosage", ""),
                "qty_prescribed":    _calc_qty_prescribed(m),
                "qty_dispensed":     0,
                "status":            "pending",
            })
        if items:
            db.table("dispense_items").insert(items).execute()
        print(f"[DISPENSE] Order created for prescription {prescription_id}")
    except Exception as e:
        print(f"[DISPENSE] Error creating order for {prescription_id}: {e}")


async def upsert_dispense_order(prescription_id: str, doctor_id: str, patient_id, patient_name, medicines: list):
    """On prescription update: delete pending order and recreate with new medicines."""
    from database import supabase as db
    try:
        existing = db.table("dispense_orders").select("id, status").eq("prescription_id", prescription_id).execute()
        for row in (existing.data or []):
            if row["status"] in ("pending", "partial"):
                db.table("dispense_items").delete().eq("dispense_order_id", row["id"]).execute()
                db.table("dispense_orders").delete().eq("id", row["id"]).execute()
        await create_dispense_order(prescription_id, doctor_id, patient_id, patient_name, medicines)
    except Exception as e:
        print(f"[DISPENSE] Error upserting order for {prescription_id}: {e}")


async def _deduct_for_dispense(medicine_id_hint, medicine_name: str, doctor_id: str, qty: float, prescription_id: str):
    """FIFO stock deduction for a single medicine at dispense time."""
    from database import supabase as db
    from datetime import date
    print(f"[DISPENSE DEDUCT] Starting: {medicine_name} qty={qty} medicine_id={medicine_id_hint} doctor={doctor_id}")
    try:
        if medicine_id_hint:
            med_res = db.table("clinic_medicines").select("id, name, low_stock_threshold").eq("id", medicine_id_hint).limit(1).execute()
            print(f"[DISPENSE DEDUCT] Direct lookup result: {med_res.data}")
        else:
            med_res = db.table("clinic_medicines").select("id, name, low_stock_threshold").eq("doctor_id", doctor_id).eq("name", medicine_name).limit(1).execute()
            if not med_res.data:
                med_res = db.table("clinic_medicines").select("id, name, low_stock_threshold").eq("doctor_id", doctor_id).ilike("name", f"%{medicine_name}%").limit(1).execute()
            print(f"[DISPENSE DEDUCT] Name lookup result: {med_res.data}")
        if not med_res.data:
            print(f"[DISPENSE DEDUCT] Medicine not found: {medicine_name}")
            return
        medicine = med_res.data[0]
        medicine_id = medicine["id"]
        batches = db.table("medicine_stock").select("id, tablets_remaining, expiry_date").eq("medicine_id", medicine_id).eq("is_active", True).gte("expiry_date", date.today().isoformat()).order("expiry_date", desc=False).execute()
        print(f"[DISPENSE DEDUCT] Batches found: {len(batches.data or [])} for {medicine_name}")
        remaining = float(qty)
        for batch in (batches.data or []):
            if remaining <= 0:
                break
            avail = float(batch["tablets_remaining"] or 0)
            if avail <= 0:
                continue
            deduct = min(avail, remaining)
            new_remaining = int(round(avail - deduct))
            db.table("medicine_stock").update({
                "tablets_remaining": new_remaining,
                "is_active": new_remaining > 0,
            }).eq("id", batch["id"]).execute()
            db.table("stock_transactions").insert({
                "medicine_id":       medicine_id,
                "stock_batch_id":    batch["id"],
                "doctor_id":         doctor_id,
                "transaction_type":  "dispensed",
                "quantity_change":   -int(round(deduct)),
                "reference_id":      prescription_id,
                "notes":             "Dispensed at pharmacy counter",
            }).execute()
            remaining -= deduct
            print(f"[DISPENSE DEDUCT] Deducted {deduct} from batch {batch['id']}, remaining={new_remaining}")
    except Exception as e:
        print(f"[DISPENSE DEDUCT] Error for {medicine_name}: {e}")


@app.get("/dispense-orders")
async def list_dispense_orders(
    doctor_id: str,
    status: str = "pending,partial",
    date_from: str = "",
    date_to: str = "",
    limit: int = 25,
    offset: int = 0,
):
    from database import supabase as db
    statuses = [s.strip() for s in status.split(",")]
    q = (
        db.table("dispense_orders")
        .select("*, patients(name, mobile, patient_code), dispense_items(*)", count="exact")
        .eq("doctor_id", doctor_id)
        .in_("status", statuses)
        .order("created_at", desc=True)
        .range(offset, offset + limit - 1)
    )
    if date_from:
        q = q.gte("created_at", f"{date_from}T00:00:00")
    if date_to:
        q = q.lte("created_at", f"{date_to}T23:59:59")
    result = q.execute()
    total = result.count or 0
    orders = result.data or []
    return {"orders": orders, "total": total, "has_more": offset + len(orders) < total}


@app.get("/dispense-orders/{order_id}")
async def get_dispense_order(order_id: str):
    from database import supabase as db
    result = db.table("dispense_orders").select(
        "*, patients(name, mobile, patient_code), dispense_items(*)"
    ).eq("id", order_id).limit(1).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Dispense order not found")
    return result.data[0]


@app.post("/dispense-orders/{order_id}/dispense")
async def process_dispense(order_id: str, request: Request, background_tasks: BackgroundTasks):
    """
    Body: { items: [ { item_id, action: 'dispense'|'external'|'pending', qty: number } ] }
    - dispense: deduct qty from stock, mark item dispensed
    - external: mark item as patient buying outside (no stock change)
    - pending: leave item for later
    """
    from database import supabase as db
    body = await request.json()
    item_actions = body.get("items", [])

    order_res = db.table("dispense_orders").select("*, dispense_items(*)").eq("id", order_id).limit(1).execute()
    if not order_res.data:
        raise HTTPException(status_code=404, detail="Order not found")
    order = order_res.data[0]
    doctor_id = order["doctor_id"]
    prescription_id = order["prescription_id"]

    import datetime as dt, pytz
    IST = pytz.timezone("Asia/Kolkata")
    now_str = dt.datetime.now(IST).isoformat()

    for action_req in item_actions:
        item_id = action_req.get("item_id")
        action  = action_req.get("action")
        qty     = float(action_req.get("qty") or 0)

        item = next((i for i in (order.get("dispense_items") or []) if i["id"] == item_id), None)
        if not item:
            continue

        if action == "dispense" and qty > 0:
            db.table("dispense_items").update({
                "qty_dispensed": (float(item.get("qty_dispensed") or 0)) + qty,
                "status":        "dispensed",
                "dispensed_at":  now_str,
            }).eq("id", item_id).execute()
            background_tasks.add_task(
                _deduct_for_dispense,
                medicine_id_hint=item.get("medicine_id"),
                medicine_name=item["medicine_name"],
                doctor_id=doctor_id,
                qty=qty,
                prescription_id=prescription_id,
            )
        elif action == "external":
            # If some qty was already dispensed from clinic, mark as partial (not external)
            already_dispensed = float(item.get("qty_dispensed") or 0)
            new_status = "partial" if already_dispensed > 0 else "external"
            db.table("dispense_items").update({
                "status": new_status,
            }).eq("id", item_id).execute()

    # Recalculate order status from all items
    DONE_STATUSES = ("dispensed", "external", "partial")
    updated_items = db.table("dispense_items").select("status").eq("dispense_order_id", order_id).execute()
    statuses = [i["status"] for i in (updated_items.data or [])]
    if all(s in DONE_STATUSES for s in statuses):
        order_status = "completed"
    elif any(s in DONE_STATUSES for s in statuses):
        order_status = "partial"
    else:
        order_status = "pending"

    db.table("dispense_orders").update({
        "status":     order_status,
        "updated_at": now_str,
    }).eq("id", order_id).execute()

    return {"ok": True, "order_status": order_status}


@app.post("/dispense-orders/{order_id}/return")
async def return_dispense_item(order_id: str, request: Request):
    """
    Return medicines to stock — handles wrong qty or patient returning unused medicines.
    Body: { item_id, qty }
    - Adds qty back to the medicine's most recent batch
    - Logs a 'returned' transaction
    - Resets dispense_item to pending so staff can re-dispense correct qty
    - Recalculates order status
    """
    from database import supabase as db
    import datetime as dt, pytz
    body = await request.json()
    item_id = body.get("item_id")
    qty     = float(body.get("qty") or 0)

    if qty <= 0:
        raise HTTPException(status_code=400, detail="qty must be > 0")

    order_res = db.table("dispense_orders").select("*, dispense_items(*)").eq("id", order_id).limit(1).execute()
    if not order_res.data:
        raise HTTPException(status_code=404, detail="Order not found")
    order = order_res.data[0]
    doctor_id       = order["doctor_id"]
    prescription_id = order["prescription_id"]

    item = next((i for i in (order.get("dispense_items") or []) if i["id"] == item_id), None)
    if not item:
        raise HTTPException(status_code=404, detail="Dispense item not found")

    IST     = pytz.timezone("Asia/Kolkata")
    now_str = dt.datetime.now(IST).isoformat()

    # Find the clinic_medicine record
    medicine_id_hint = item.get("medicine_id")
    medicine_name    = item["medicine_name"]
    if medicine_id_hint:
        med_res = db.table("clinic_medicines").select("id, name").eq("id", medicine_id_hint).limit(1).execute()
    else:
        med_res = db.table("clinic_medicines").select("id, name").eq("doctor_id", doctor_id).eq("name", medicine_name).limit(1).execute()
        if not med_res.data:
            med_res = db.table("clinic_medicines").select("id, name").eq("doctor_id", doctor_id).ilike("name", f"%{medicine_name}%").limit(1).execute()

    if not med_res.data:
        raise HTTPException(status_code=404, detail=f"Medicine not found in clinic catalogue: {medicine_name}")

    clinic_med_id = med_res.data[0]["id"]

    # Find the most recent stock batch for this medicine to return into
    batch_res = db.table("medicine_stock").select("id, tablets_remaining, expiry_date").eq("medicine_id", clinic_med_id).order("expiry_date", desc=True).limit(1).execute()
    if not batch_res.data:
        raise HTTPException(status_code=404, detail=f"No stock batch found for {medicine_name}")

    batch    = batch_res.data[0]
    batch_id = batch["id"]
    new_remaining = int(round(float(batch.get("tablets_remaining") or 0) + qty))

    # Add stock back
    db.table("medicine_stock").update({
        "tablets_remaining": new_remaining,
        "is_active":         True,
    }).eq("id", batch_id).execute()

    # Log return transaction
    db.table("stock_transactions").insert({
        "medicine_id":      clinic_med_id,
        "stock_batch_id":   batch_id,
        "doctor_id":        doctor_id,
        "transaction_type": "returned",
        "quantity_change":  int(round(qty)),
        "reference_id":     prescription_id,
        "notes":            f"Returned to stock — {body.get('reason', 'Wrong qty / patient return')}",
    }).execute()

    # Reset dispense item to pending
    new_qty_dispensed = max(0, float(item.get("qty_dispensed") or 0) - qty)
    db.table("dispense_items").update({
        "qty_dispensed": int(round(new_qty_dispensed)),
        "status":        "pending",
        "dispensed_at":  None,
    }).eq("id", item_id).execute()

    # Recalculate order status
    DONE_STATUSES = ("dispensed", "external", "partial")
    updated_items = db.table("dispense_items").select("status").eq("dispense_order_id", order_id).execute()
    item_statuses = [i["status"] for i in (updated_items.data or [])]
    if all(s in DONE_STATUSES for s in item_statuses):
        order_status = "completed"
    elif any(s in DONE_STATUSES for s in item_statuses):
        order_status = "partial"
    else:
        order_status = "pending"

    db.table("dispense_orders").update({
        "status":     order_status,
        "updated_at": now_str,
    }).eq("id", order_id).execute()

    print(f"[DISPENSE RETURN] {qty} units of {medicine_name} returned to batch {batch_id} (new total: {new_remaining})")
    return {"ok": True, "order_status": order_status, "new_stock": new_remaining}


@app.post("/dispense-orders/{order_id}/reopen-item")
async def reopen_dispense_item(order_id: str, request: Request):
    """
    Reopen a completed dispense item so remaining qty can be dispensed later.
    Body: { item_id }
    - Resets item status to pending
    - Order moves back to partial so it appears in the Pending/Partial tab
    """
    from database import supabase as db
    import datetime as dt, pytz
    body    = await request.json()
    item_id = body.get("item_id")

    order_res = db.table("dispense_orders").select("id, status").eq("id", order_id).limit(1).execute()
    if not order_res.data:
        raise HTTPException(status_code=404, detail="Order not found")

    item_res = db.table("dispense_items").select("id, status, qty_prescribed, qty_dispensed").eq("id", item_id).limit(1).execute()
    if not item_res.data:
        raise HTTPException(status_code=404, detail="Dispense item not found")

    item = item_res.data[0]
    remaining = float(item["qty_prescribed"]) - float(item.get("qty_dispensed") or 0)
    if remaining <= 0:
        raise HTTPException(status_code=400, detail="No remaining qty to dispense")

    db.table("dispense_items").update({
        "status":       "pending",
        "dispensed_at": None,
    }).eq("id", item_id).execute()

    # Order must move back to partial (some items already done, this one pending)
    IST     = pytz.timezone("Asia/Kolkata")
    now_str = dt.datetime.now(IST).isoformat()
    db.table("dispense_orders").update({
        "status":     "partial",
        "updated_at": now_str,
    }).eq("id", order_id).execute()

    print(f"[DISPENSE REOPEN] Item {item_id} reopened, remaining={remaining}")
    return {"ok": True, "remaining": remaining}


@app.post("/prescriptions")
async def create_prescription_v2(request: Request, background_tasks: BackgroundTasks):
    from database import supabase as db
    import datetime as dt
    import pytz

    body = await request.json()
    doctor_id_req    = body.get("doctor_id", "8c33abe0-5d2e-4613-9437-c7c375e8d162")
    patient_id       = body.get("patient_id") or None
    visit_id_req     = body.get("visit_id") or None
    appointment_id   = body.get("appointment_id") or None
    is_walkin        = body.get("is_walkin", False)
    walkin_name      = body.get("walkin_name") or None
    walkin_age       = body.get("walkin_age") or None
    chief_complaint  = body.get("chief_complaint", "")
    dietary          = body.get("dietary_instructions", "")
    precautions      = body.get("precautions", "")
    notes            = body.get("general_notes", "")
    medicines_input  = body.get("medicines", [])

    IST = pytz.timezone("Asia/Kolkata")
    now_ist = dt.datetime.now(IST)
    today_str = now_ist.date().isoformat()

    import json as _json
    diagnosis = body.get("diagnosis", "")

    # For walk-in: encode name/age/complaint/diagnosis into general_notes since no patient row
    if is_walkin:
        meta = _json.dumps({"__walkin": True, "name": walkin_name or "", "age": walkin_age, "complaint": chief_complaint, "diagnosis": diagnosis}, ensure_ascii=False)
        notes = f"WALKIN::{meta}::END\n{notes}" if notes else f"WALKIN::{meta}::END"

    # Complete a pre-created visit (eager visit from the prescription page)
    visit_id = visit_id_req
    if visit_id:
        visit_update = {
            "chief_complaint": chief_complaint,
            "diagnosis":       diagnosis,
            "visit_status":    "Completed",
        }
        if appointment_id:
            visit_update["appointment_id"] = appointment_id
        db.table("visits").update(visit_update).eq("id", visit_id).execute()

    # Auto-create visit if patient linked and no visit provided
    if patient_id and not visit_id:
        visit_res = db.table("visits").insert({
            "patient_id":      patient_id,
            "doctor_id":       doctor_id_req,
            "appointment_id":  appointment_id,
            "chief_complaint": chief_complaint,
            "diagnosis":       diagnosis,
            "visit_status":    "Completed",
            "visit_date":      today_str,
            "created_at":      now_ist.isoformat(),
        }).execute()
        visit_id = visit_res.data[0]["id"] if visit_res.data else None

    pres_data = {
        "doctor_id":            doctor_id_req,
        "patient_id":           patient_id,
        "visit_id":             visit_id,
        "prescription_date":    today_str,
        "dietary_instructions": dietary,
        "precautions":          precautions,
        "general_notes":        notes,
        "whatsapp_sent":        False,
        "created_at":           now_ist.isoformat(),
    }
    pres_res = db.table("prescriptions").insert(pres_data).execute()
    pres = pres_res.data[0] if pres_res.data else {}
    pres_id = pres.get("id", "")

    med_rows = []
    for i, m in enumerate(medicines_input):
        if not m.get("medicine_name", "").strip():
            continue
        med_rows.append({
            "prescription_id": pres_id,
            "medicine_name":   m["medicine_name"],
            "dosage":          m.get("dosage", ""),
            "morning":         m.get("morning", False),
            "afternoon":       m.get("afternoon", False),
            "evening":         m.get("evening", False),
            "night":           m.get("night", False),
            "before_food":     m.get("before_food", False),
            "duration_days":   m.get("duration_days", 5),
            "instructions":    m.get("instructions", ""),
            "sort_order":      m.get("sort_order", i + 1),
        })
    if med_rows:
        db.table("prescription_medicines").insert(med_rows).execute()

    # Auto followup if patient linked
    if patient_id:
        _fu_cfg = db.table("clinic_config").select("config_value").eq("doctor_id", doctor_id_req).eq("config_key", "clinic.followup_days").execute()
        _fu_days = int((_fu_cfg.data[0]["config_value"] if _fu_cfg.data else None) or 7)
        followup_date = (now_ist.date() + dt.timedelta(days=_fu_days)).isoformat()
        try:
            db.table("followups").insert({
                "patient_id":     patient_id,
                "doctor_id":      doctor_id_req,
                "visit_id":       visit_id,
                "scheduled_date": followup_date,
                "channel":        "whatsapp",
                "call_status":    "Pending",
                "followup_day":   7,
            }).execute()
        except Exception:
            pass

    # Create dispense order for pharmacy (replaces auto-deduct)
    if pres_id and medicines_input:
        background_tasks.add_task(
            create_dispense_order,
            prescription_id=pres_id,
            doctor_id=doctor_id_req,
            patient_id=patient_id,
            patient_name=walkin_name,
            medicines=medicines_input,
        )

    return {"prescription_id": pres_id}


@app.post("/prescriptions/{prescription_id}/send-whatsapp")
async def send_prescription_whatsapp(prescription_id: str):
    from database import supabase as db
    import datetime as dt
    import pytz

    try:
        pres_res = db.table("prescriptions").select("*").eq("id", prescription_id).execute()
        if not pres_res.data:
            raise HTTPException(status_code=404, detail="Prescription not found")
        pres = pres_res.data[0]

        patient_id = pres.get("patient_id")
        if not patient_id:
            raise HTTPException(status_code=400, detail="No patient linked to this prescription")

        pat_res = db.table("patients").select("name, mobile, patient_code, language").eq("id", patient_id).execute()
        if not pat_res.data:
            raise HTTPException(status_code=404, detail="Patient not found")
        pat = pat_res.data[0]

        name = pat.get("name", "")
        mobile = (pat.get("mobile") or "").lstrip("+")
        pcode = pat.get("patient_code", "")
        language = (pat.get("language") or "english").lower()

        meds_res = db.table("prescription_medicines").select("*").eq("prescription_id", prescription_id).execute()
        medicines = meds_res.data or []

        dietary = pres.get("dietary_instructions", "")
        precautions_text = pres.get("precautions", "")

        IST = pytz.timezone("Asia/Kolkata")
        now_ist = dt.datetime.now(IST)
        pdate = pres.get("prescription_date", now_ist.strftime("%Y-%m-%d"))
        try:
            pdate_fmt = dt.datetime.strptime(pdate, "%Y-%m-%d").strftime("%d %b %Y")
        except Exception:
            pdate_fmt = pdate

        timing_icons = {"morning": "🌅", "afternoon": "☀️", "evening": "🌆", "night": "🌙"}
        timing_labels_en = {"morning": "Morning", "afternoon": "Afternoon", "evening": "Evening", "night": "Night"}
        timing_labels_ta = {"morning": "காலை", "afternoon": "மதியம்", "evening": "மாலை", "night": "இரவு"}

        def med_line(m, lang, idx):
            timings_keys = [k for k in ["morning", "afternoon", "evening", "night"] if m.get(k)]
            icons = " + ".join(timing_icons[k] for k in timings_keys)
            if lang == "tamil":
                labels = " + ".join(timing_labels_ta[k] for k in timings_keys)
                food = "சாப்பிடுவதற்கு முன்" if m.get("before_food") else "சாப்பிட்ட பின்"
                dur = f"{m.get('duration_days', 5)} நாட்கள்"
            else:
                labels = " + ".join(timing_labels_en[k] for k in timings_keys)
                food = "Before food" if m.get("before_food") else "After food"
                dur = f"{m.get('duration_days', 5)} days"
            inst = f"\n   {m['instructions']}" if m.get("instructions") else ""
            return f"{idx}. {m['medicine_name']} — {m.get('dosage', '')}\n   {icons} {labels} | {food} | {dur}{inst}"

        med_lines = "\n\n".join(
            med_line(m, language, i + 1)
            for i, m in enumerate(medicines)
            if m.get("medicine_name", "").strip()
        )

        _doc_res2 = db.table("doctors").select("clinic_name").eq("id", pres.get("doctor_id", "")).limit(1).execute()
        _clinic_name2 = ((_doc_res2.data or [{}])[0].get("clinic_name")) or "TrueCare Family Clinic"

        if language == "tamil":
            msg = (
                f"💊 *மருந்துச் சீட்டு*\n"
                f"🏥 {_clinic_name2}\n\n"
                f"நோயாளி: {name}" + (f" ({pcode})" if pcode else "") + f"\n"
                f"தேதி: {pdate_fmt}\n\n"
                f"மருந்துகள்:\n{med_lines}"
            )
            if dietary:
                msg += f"\n\n🥗 உணவு: {dietary}"
            if precautions_text:
                msg += f"\n⚠️ எச்சரிக்கை: {precautions_text}"
            msg += f"\n\nFollow-up: 7 நாட்களில் வரவும்.\nகேள்விகளுக்கு MENU என்று reply பண்ணுங்கள்."
        else:
            msg = (
                f"💊 *Your Prescription*\n"
                f"🏥 {_clinic_name2}\n\n"
                f"Patient: {name}" + (f" ({pcode})" if pcode else "") + f"\n"
                f"Date: {pdate_fmt}\n\n"
                f"Medicines:\n{med_lines}"
            )
            if dietary:
                msg += f"\n\n🥗 Diet: {dietary}"
            if precautions_text:
                msg += f"\n⚠️ Precautions: {precautions_text}"
            msg += f"\n\nFollow-up in 7 days.\nReply MENU for any help."

        await send_meta_text(mobile, msg)

        db.table("prescriptions").update({"whatsapp_sent": True}).eq("id", prescription_id).execute()

        return {"sent": True, "mobile": mobile}

    except HTTPException:
        raise
    except Exception as e:
        return {"sent": False, "error": str(e)}


# ── FOLLOW-UPS ────────────────────────────────────────────
@app.get("/followups/pending")
async def pending_followups(doctor_id: str):
    from database import supabase
    # followups table (no underscore); pending = completed_at is null
    result = supabase.table("followups").select("*, patients(name, mobile, language)").eq("doctor_id", doctor_id).is_("completed_at", "null").order("created_at", desc=True).execute()
    return result.data or []


@app.get("/followups")
async def list_followups(doctor_id: str):
    from database import supabase
    result = supabase.table("followups").select("*, patients(name, mobile, language)").eq("doctor_id", doctor_id).order("created_at", desc=True).execute()
    return result.data or []


@app.get("/followups/summary")
async def followups_summary(doctor_id: str, days: int = 7):
    from database import supabase
    from datetime import datetime, timedelta
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    result = supabase.table("followups").select("call_status").eq("doctor_id", doctor_id).gte("created_at", cutoff).execute()
    rows = result.data or []
    counts: dict[str, int] = {
        "pending": 0, "whatsapp_sent": 0, "recovered": 0,
        "recovering": 0, "needs_appointment": 0, "booked": 0,
        "no_response": 0, "completed": 0, "call_triggered": 0,
    }
    for r in rows:
        cs = (r.get("call_status") or "Pending").lower().replace(" ", "_").replace("-", "_")
        if cs in counts:
            counts[cs] += 1
        else:
            counts["pending"] += 1
    counts["total"] = len(rows)
    counts["awaiting_reply"] = counts["pending"] + counts["whatsapp_sent"]
    counts["resolved"] = counts["recovered"] + counts["completed"] + counts["booked"]
    counts["needs_attention"] = counts["recovering"] + counts["needs_appointment"] + counts["no_response"]
    return counts


@app.get("/followups/list")
async def list_followups_filtered(doctor_id: str, days: int = 7, status: str = "all"):
    from database import supabase
    from datetime import datetime, timedelta
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    q = supabase.table("followups").select("*, patients(name, mobile, language)").eq("doctor_id", doctor_id).gte("created_at", cutoff).order("created_at", desc=True)
    if status != "all":
        status_map = {
            "pending": "Pending",
            "whatsapp_sent": "Whatsapp-Sent",
            "recovered": "Recovered",
            "recovering": "Recovering",
            "needs_appointment": "Needs Appointment",
            "booked": "Booked",
            "no_response": "No Response",
            "completed": "Completed",
        }
        db_status = status_map.get(status.lower())
        if db_status:
            q = q.eq("call_status", db_status)
    result = q.execute()
    return result.data or []


# ── QUERIES ───────────────────────────────────────────────
@app.get("/queries/pending")
async def pending_queries(doctor_id: str):
    from database import supabase
    # table is "queries", not "patient_queries"
    result = supabase.table("queries").select("*, patients(name, mobile, patient_code, age, gender, language, created_at)").eq("doctor_id", doctor_id).eq("status", "Pending").order("created_at", desc=True).execute()
    return result.data or []


@app.get("/queries")
async def list_queries(doctor_id: str, patient_id: str = ""):
    from database import supabase
    q = supabase.table("queries").select("*, patients(name, mobile, patient_code, age, gender, language, created_at)").eq("doctor_id", doctor_id)
    if patient_id:
        q = q.eq("patient_id", patient_id)
    result = q.order("created_at", desc=True).execute()
    return result.data or []


@app.patch("/queries/{query_id}/answer")
async def answer_query(query_id: str, request: Request):
    from database import supabase
    import datetime as dt
    body = await request.json()
    reply_text = body["answer"]
    # replied_by is a UUID column — fetch doctor_id, patient_id and original question
    q_row = supabase.table("queries").select("doctor_id, patient_id, question").eq("id", query_id).execute()
    doctor_id = q_row.data[0]["doctor_id"] if q_row.data else None
    patient_id = q_row.data[0]["patient_id"] if q_row.data else None
    question_text = q_row.data[0]["question"] if q_row.data else ""
    update = {
        "reply": reply_text,
        "status": "Closed",
        "replied_at": dt.datetime.utcnow().isoformat(),
    }
    if doctor_id:
        update["replied_by"] = doctor_id
    result = supabase.table("queries").update(update).eq("id", query_id).execute()

    # Send WhatsApp notification to patient (non-blocking — DB save already succeeded)
    try:
        if patient_id:
            pat = supabase.table("patients").select("mobile, patient_code").eq("id", patient_id).execute()
            mobile = pat.data[0]["mobile"] if pat.data else None
            patient_code = pat.data[0].get("patient_code", "") if pat.data else ""
            if mobile:
                _dr_res = supabase.table("doctors").select("name, clinic_name").eq("id", doctor_id).limit(1).execute()
                _dr_row = (_dr_res.data or [{}])[0]
                _reply_doctor_name = _dr_row.get("name") or "Doctor"
                _reply_clinic_name = _dr_row.get("clinic_name") or "TrueCare Family Clinic"
                msg = (
                    f"👨‍⚕️ *{_reply_clinic_name}*\n\n"
                    f"Patient: *{patient_code}*\n\n"
                    f"{_reply_doctor_name} has replied to your question:\n\n"
                    f"*Your question:* {question_text}\n"
                    f"*{_reply_doctor_name}'s reply:* {reply_text}\n\n"
                    f"Reply MENU for main menu."
                )
                await send_meta_text(mobile, msg)
                print(f"✅ WhatsApp reply sent for query {query_id} to {mobile}")
            else:
                print(f"⚠️ No mobile found for patient {patient_id}, skipping WhatsApp")
    except Exception as e:
        print(f"❌ WhatsApp reply failed for query {query_id}: {e}")

    return result.data[0] if result.data else {}


# ── REVIEWS ───────────────────────────────────────────────
@app.get("/reviews")
async def list_reviews(doctor_id: str):
    from database import supabase
    # table is "reviews" not "review_requests", sort by created_at
    result = supabase.table("reviews").select("*, patients(name, mobile)").eq("doctor_id", doctor_id).order("created_at", desc=True).execute()
    return result.data or []


# ── ANALYTICS ─────────────────────────────────────────────
@app.get("/analytics/summary")
async def analytics_summary():
    from database import supabase
    from datetime import datetime, timedelta, date
    import pytz
    IST = pytz.timezone("Asia/Kolkata")
    today = datetime.now(IST).date()
    month_start = today.replace(day=1).isoformat()
    month_end   = today.isoformat()

    # Last 6 calendar months (inclusive of current)
    def month_label(d: date) -> str:
        return d.strftime("%b")
    def month_start_for(d: date) -> str:
        return d.replace(day=1).isoformat()
    def month_end_for(d: date) -> str:
        # last day of month
        if d.month == 12:
            last = d.replace(year=d.year+1, month=1, day=1) - timedelta(days=1)
        else:
            last = d.replace(month=d.month+1, day=1) - timedelta(days=1)
        return last.isoformat()

    months = []
    for i in range(5, -1, -1):
        # Go back i months from current month
        m = today.month - i
        y = today.year
        while m <= 0:
            m += 12
            y -= 1
        months.append(date(y, m, 1))

    # Fetch all doctors
    doctors_res = supabase.table("doctors").select("id, name").eq("is_available", True).execute()
    doctors = doctors_res.data or []
    doctor_map = {d["id"]: d["name"].split()[-1] for d in doctors}  # last name as short label

    # ── 1. All appointments (clinic-wide, no doctor filter) ──
    appts_res = supabase.table("appointments").select(
        "id, appointment_date, appointment_time, patient_id, doctor_id, status"
    ).execute()
    all_appts = appts_res.data or []

    # ── 2. Total registered patients (clinic-wide) + seen this month ──
    month_appts = [a for a in all_appts if month_start <= (a.get("appointment_date") or "") <= month_end]
    total_patients_res = supabase.table("patients").select("*", count="exact").execute()
    total_patients_month = total_patients_res.count if total_patients_res.count is not None else len(total_patients_res.data or [])
    patients_seen_month = len(set(a["patient_id"] for a in month_appts if a.get("patient_id")))

    # ── 3. Avg daily appts this month ──
    from collections import defaultdict
    daily_counts: dict = defaultdict(int)
    for a in month_appts:
        d = a.get("appointment_date")
        if d:
            daily_counts[d] += 1
    avg_daily = round(sum(daily_counts.values()) / max(len(daily_counts), 1), 1)

    # ── 4. Avg satisfaction (clinic-wide) ──
    reviews_res = supabase.table("reviews").select("rating").not_.is_("rating", "null").execute()
    ratings = [r["rating"] for r in (reviews_res.data or []) if r.get("rating") is not None]
    avg_satisfaction = round(sum(ratings) / len(ratings), 1) if ratings else None

    # ── 5. Follow-up rate this month ──
    visits_res = supabase.table("visits").select("id").gte("created_at", f"{month_start}T00:00:00").lte("created_at", f"{month_end}T23:59:59").execute()
    visit_ids = [v["id"] for v in (visits_res.data or [])]
    followups_month = 0
    if visit_ids:
        fu_res = supabase.table("followups").select("visit_id").in_("visit_id", visit_ids).execute()
        followup_visit_ids = set(f["visit_id"] for f in (fu_res.data or []) if f.get("visit_id"))
        followups_month = len(followup_visit_ids)
    followup_rate = round(followups_month / max(len(visit_ids), 1) * 100) if visit_ids else 0

    # ── 6. Monthly trend (last 6 months) ──
    # Track first-ever appointment per patient for "new patients"
    patient_first: dict = {}
    for a in sorted(all_appts, key=lambda x: x.get("appointment_date") or ""):
        pid = a.get("patient_id")
        d   = a.get("appointment_date")
        if pid and d and pid not in patient_first:
            patient_first[pid] = d

    monthly_trend = []
    for m in months:
        ms = month_start_for(m)
        me = month_end_for(m)
        m_appts = [a for a in all_appts if ms <= (a.get("appointment_date") or "") <= me]
        total = len(set(a["patient_id"] for a in m_appts if a.get("patient_id")))
        new_patients = sum(1 for pid, fd in patient_first.items() if ms <= fd <= me)
        by_doctor = defaultdict(set)
        for a in m_appts:
            did = a.get("doctor_id")
            pid = a.get("patient_id")
            if did and pid:
                by_doctor[did].add(pid)
        row: dict = {"month": month_label(m), "total": total, "new_patients": new_patients}
        for did, name in doctor_map.items():
            row[name] = len(by_doctor.get(did, set()))
        monthly_trend.append(row)

    # ── 7. Appointments by doctor per month ──
    appts_by_doctor = []
    for m in months:
        ms = month_start_for(m)
        me = month_end_for(m)
        m_appts = [a for a in all_appts if ms <= (a.get("appointment_date") or "") <= me]
        row2: dict = {"month": month_label(m)}
        for did, name in doctor_map.items():
            row2[name] = sum(1 for a in m_appts if a.get("doctor_id") == did)
        appts_by_doctor.append(row2)

    # ── 8. Age distribution (all patients) ──
    patients_res = supabase.table("patients").select("age").execute()
    age_buckets: dict = {"0–12": 0, "13–25": 0, "26–40": 0, "41–60": 0, "60+": 0}
    for p in (patients_res.data or []):
        age = p.get("age")
        if age is None:
            continue
        try:
            age = int(age)
        except (ValueError, TypeError):
            continue
        if age <= 12:   age_buckets["0–12"] += 1
        elif age <= 25: age_buckets["13–25"] += 1
        elif age <= 40: age_buckets["26–40"] += 1
        elif age <= 60: age_buckets["41–60"] += 1
        else:           age_buckets["60+"] += 1
    age_distribution = [{"group": k, "count": v} for k, v in age_buckets.items()]

    # ── 9. Peak hours (all appointments) ──
    hour_counts: dict = defaultdict(int)
    for a in all_appts:
        t = a.get("appointment_time")
        if t:
            try:
                h = int(t.split(":")[0])
                hour_counts[h] += 1
            except (ValueError, IndexError):
                pass
    peak_hours = []
    for h in range(6, 22):
        label = f"{h % 12 or 12}{'am' if h < 12 else 'pm'}"
        peak_hours.append({"hour": label, "count": hour_counts.get(h, 0)})
    # Trim leading/trailing zeros
    while peak_hours and peak_hours[0]["count"] == 0:
        peak_hours.pop(0)
    while peak_hours and peak_hours[-1]["count"] == 0:
        peak_hours.pop()

    # ── 10. Top conditions (diagnosis keyword bucketing) ──
    visits_diag_res = supabase.table("visits").select("diagnosis").execute()
    cond_counts: dict = defaultdict(int)
    CONDITION_KEYWORDS = {
        "Fever / Cold":  ["fever", "cold", "flu", "viral", "cough", "throat", "cold and"],
        "Hypertension":  ["hypertension", "blood pressure", "bp", "htn"],
        "Diabetes":      ["diabetes", "diabetic", "sugar", "dm ", "type 2", "type 1"],
        "Respiratory":   ["asthma", "respiratory", "bronchitis", "wheez", "copd", "breathin"],
        "Skin":          ["skin", "rash", "eczema", "dermatitis", "allerg", "itching"],
        "Gastro":        ["gastro", "stomach", "abdomen", "diarrhea", "vomit", "nausea", "acidity", "gastritis"],
    }
    for v in (visits_diag_res.data or []):
        diag = (v.get("diagnosis") or "").lower()
        if not diag:
            continue
        matched = False
        for cond, keywords in CONDITION_KEYWORDS.items():
            if any(kw in diag for kw in keywords):
                cond_counts[cond] += 1
                matched = True
                break
        if not matched:
            cond_counts["Other"] += 1
    total_diag = sum(cond_counts.values()) or 1
    top_conditions = sorted(
        [{"name": k, "value": round(v / total_diag * 100)} for k, v in cond_counts.items() if v > 0],
        key=lambda x: -x["value"]
    )[:6]

    # ── 11. Patient retention ──
    # Group appointments by patient, sorted by date
    patient_appt_dates: dict = defaultdict(list)
    for a in all_appts:
        pid = a.get("patient_id")
        d   = a.get("appointment_date")
        if pid and d:
            patient_appt_dates[pid].append(d)
    for pid in patient_appt_dates:
        patient_appt_dates[pid].sort()

    ret_30 = ret_90 = ret_ever = 0
    total_patients_with_visits = len(patient_appt_dates)
    for pid, dates in patient_appt_dates.items():
        if len(dates) < 2:
            continue
        first = datetime.strptime(dates[0], "%Y-%m-%d").date()
        second = datetime.strptime(dates[1], "%Y-%m-%d").date()
        gap = (second - first).days
        ret_ever += 1
        if gap <= 90: ret_90 += 1
        if gap <= 30: ret_30 += 1

    def pct(n): return round(n / max(total_patients_with_visits, 1) * 100)
    retention = {"d30": pct(ret_30), "d90": pct(ret_90), "all_time": pct(ret_ever)}

    # ── prev month for KPI change calculations ──
    prev_month_start = month_start_for(months[-2]) if len(months) >= 2 else month_start
    prev_month_end   = month_end_for(months[-2])   if len(months) >= 2 else month_start
    prev_appts = [a for a in all_appts if prev_month_start <= (a.get("appointment_date") or "") <= prev_month_end]
    prev_daily_counts = defaultdict(int)
    for a in prev_appts:
        d = a.get("appointment_date")
        if d:
            prev_daily_counts[d] += 1
    prev_avg_daily = round(sum(prev_daily_counts.values()) / max(len(prev_daily_counts), 1), 1)
    # New patients registered last month vs this month for change %
    new_this_month = sum(1 for pid, fd in patient_first.items() if month_start <= fd <= month_end)
    new_prev_month = sum(1 for pid, fd in patient_first.items() if prev_month_start <= fd <= prev_month_end)

    def change_pct(curr, prev):
        if prev == 0:
            return None
        return round((curr - prev) / prev * 100, 1)

    return {
        "kpis": {
            "total_patients_month": total_patients_month,
            "patients_seen_month": patients_seen_month,
            "total_patients_change": change_pct(new_this_month, new_prev_month),
            "avg_daily_appts": avg_daily,
            "avg_daily_change": change_pct(avg_daily, prev_avg_daily),
            "avg_satisfaction": avg_satisfaction,
            "followup_rate": followup_rate,
        },
        "monthly_trend": monthly_trend,
        "appts_by_doctor": appts_by_doctor,
        "age_distribution": age_distribution,
        "peak_hours": peak_hours,
        "top_conditions": top_conditions,
        "retention": retention,
        "doctor_names": list(doctor_map.values()),
    }


# ── DOCTOR ────────────────────────────────────────────────
@app.get("/doctor/{doctor_id}")
async def get_doctor(doctor_id: str):
    from database import supabase
    result = supabase.table("doctors").select("*").eq("id", doctor_id).single().execute()
    return result.data


@app.patch("/doctor/{doctor_id}")
async def update_doctor(doctor_id: str, request: Request):
    from database import supabase
    body = await request.json()
    result = supabase.table("doctors").update(body).eq("id", doctor_id).execute()
    return result.data[0] if result.data else {}


# ── MULTI-DOCTOR API ──────────────────────────────────
@app.post("/api/auth/login")
async def auth_login(request: Request):
    """PIN-based staff login. Returns JWT token + role info."""
    from auth import login_staff
    body = await request.json()
    username = (body.get("username") or "").strip().lower()
    pin = (body.get("pin") or "").strip()
    if not username or not pin:
        raise HTTPException(status_code=400, detail="username and pin required")
    result = await login_staff(supabase, username, pin)
    if not result:
        raise HTTPException(status_code=401, detail="Invalid username or PIN")
    return result


@app.get("/api/auth/me")
async def auth_me(request: Request):
    """Validate token and return current staff profile."""
    from auth import decode_token
    auth_header = request.headers.get("Authorization", "")
    token = auth_header.removeprefix("Bearer ").strip()
    payload = decode_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return {
        "id": payload["sub"],
        "username": payload["username"],
        "role": payload["role"],
        "doctor_id": payload.get("doctor_id"),
        "clinic_whatsapp": payload["clinic_whatsapp"],
        "name": payload["name"],
    }


@app.get("/api/staff")
async def list_staff(request: Request):
    """List all staff for a clinic. Admin only."""
    from auth import decode_token
    auth_header = request.headers.get("Authorization", "")
    payload = decode_token(auth_header.removeprefix("Bearer ").strip())
    if not payload or payload.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    res = supabase.table("clinic_staff") \
        .select("id, name, username, role, doctor_id, is_active, created_at") \
        .eq("clinic_whatsapp", payload["clinic_whatsapp"]) \
        .order("created_at").execute()
    return {"staff": res.data or []}


@app.post("/api/staff")
async def create_staff(request: Request):
    """Create a staff member. Admin only."""
    from auth import decode_token, hash_pin
    auth_header = request.headers.get("Authorization", "")
    payload = decode_token(auth_header.removeprefix("Bearer ").strip())
    if not payload or payload.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    body = await request.json()
    required = ["name", "username", "pin", "role"]
    if not all(body.get(k) for k in required):
        raise HTTPException(status_code=400, detail=f"Required: {required}")
    valid_roles = {"doctor", "receptionist", "pharmacist", "lab", "admin"}
    if body["role"] not in valid_roles:
        raise HTTPException(status_code=400, detail=f"role must be one of {valid_roles}")
    try:
        res = supabase.table("clinic_staff").insert({
            "clinic_whatsapp": payload["clinic_whatsapp"],
            "doctor_id": body.get("doctor_id") or None,
            "role": body["role"],
            "name": body["name"],
            "username": body["username"].strip().lower(),
            "pin_hash": hash_pin(body["pin"]),
            "is_active": True,
        }).execute()
        return {"staff": res.data[0]}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.patch("/api/staff/{staff_id}")
async def update_staff(staff_id: str, request: Request):
    """Update name, PIN, role, or is_active. Admin only."""
    from auth import decode_token, hash_pin
    auth_header = request.headers.get("Authorization", "")
    payload = decode_token(auth_header.removeprefix("Bearer ").strip())
    if not payload or payload.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    body = await request.json()
    updates = {}
    if "name" in body:
        updates["name"] = body["name"]
    if "pin" in body and body["pin"]:
        updates["pin_hash"] = hash_pin(body["pin"])
    if "role" in body:
        updates["role"] = body["role"]
    if "is_active" in body:
        updates["is_active"] = body["is_active"]
    if not updates:
        raise HTTPException(status_code=400, detail="Nothing to update")
    res = supabase.table("clinic_staff") \
        .update(updates) \
        .eq("id", staff_id) \
        .eq("clinic_whatsapp", payload["clinic_whatsapp"]) \
        .execute()
    return {"staff": res.data[0] if res.data else {}}


@app.get("/api/doctors")
async def list_doctors(clinic_whatsapp: str = None):
    """All active doctors for a clinic WhatsApp number. Used by frontend doctor switcher."""
    from multi_doctor import get_clinic_doctors
    doctors = await get_clinic_doctors(supabase, clinic_whatsapp or "")
    return {"doctors": doctors}


@app.get("/api/me/context")
async def get_user_context(doctor_id: str):
    """Role + doctor context for frontend. Used by useClinicContext hook."""
    from multi_doctor import get_doctor_by_id, is_multi_doctor_enabled
    doctor = await get_doctor_by_id(supabase, doctor_id)
    if not doctor:
        return {"error": "Doctor not found"}, 404
    multi_doctor = await is_multi_doctor_enabled(supabase, doctor_id)
    return {
        "doctor_id": doctor_id,
        "doctor_name": doctor.get("name", ""),
        "specialty": doctor.get("specialty_display") or doctor.get("speciality", ""),
        "clinic_name": doctor.get("clinic_name", ""),
        "whatsapp_number": doctor.get("whatsapp_number", ""),
        "role": "doctor",
        "multi_doctor_enabled": multi_doctor,
    }


# ── VOICE WEBHOOKS ────────────────────────────────────
@app.get("/webhook/voice/followup")
@app.post("/webhook/voice/followup")
async def voice_followup(request: Request):
    """Twilio voice webhook - plays follow-up audio"""
    from followup import handle_voice_followup_webhook
    return await handle_voice_followup_webhook(request)


@app.post("/webhook/voice/followup-response")
async def voice_followup_response(request: Request):
    """Twilio voice webhook - handles keypress"""
    from followup import handle_voice_followup_response
    return await handle_voice_followup_response(request)


# ── FOLLOW-UP TRIGGER ENDPOINTS ───────────────────────
@app.post("/trigger/followup-whatsapp")
async def trigger_followup_whatsapp():
    from followup import send_followup_whatsapp_job
    await send_followup_whatsapp_job()
    return {"status": "Follow-up WhatsApp sent"}


@app.post("/trigger/followup-calls")
async def trigger_followup_calls():
    from followup import make_followup_calls_job
    await make_followup_calls_job()
    return {"status": "Follow-up calls initiated"}



# ── APPOINTMENT SLOTS ─────────────────────────────────────

@app.get("/appointments/slots")
async def get_appointment_slots(doctor_id: str, date: str):
    """Return time slots with booking counts for a given date, respecting availability overrides."""
    from database import supabase
    from datetime import datetime, timedelta
    from routers.availability import get_availability_for_date, get_full_clinic_config

    # Load clinic config
    cfg_full = get_full_clinic_config(doctor_id)
    max_slot_res = supabase.table("clinic_config")\
        .select("config_value")\
        .eq("doctor_id", doctor_id)\
        .eq("config_key", "clinic.max_per_slot")\
        .execute()
    max_slot = int((max_slot_res.data[0]["config_value"] if max_slot_res.data else None) or 3)

    # Respect availability overrides
    av = get_availability_for_date(doctor_id, date)
    dur = av.get("slot_duration_minutes") or cfg_full["duration"]

    def parse_t(s):
        h, m_ = map(int, s.split(":"))
        return datetime(2000, 1, 1, h, m_)

    def gen_slots(start_str, end_str, session: str):
        result_slots = []
        t = parse_t(start_str)
        end = parse_t(end_str)
        while t < end:
            result_slots.append((t.strftime("%H:%M"), session))
            t += timedelta(minutes=dur)
        return result_slots

    all_slots = []
    if not av["is_holiday"]:
        if av["morning"]["enabled"]:
            all_slots += gen_slots(av["morning"]["start"], av["morning"]["end"], "morning")
        if av["evening"]["enabled"]:
            all_slots += gen_slots(av["evening"]["start"], av["evening"]["end"], "evening")

    # Count occupied slots
    appts_res = supabase.table("appointments")\
        .select("appointment_time")\
        .eq("doctor_id", doctor_id)\
        .eq("appointment_date", date)\
        .in_("status", ["Confirmed", "In Progress", "Completed"])\
        .execute()
    booked_counts: dict[str, int] = {}
    for a in (appts_res.data or []):
        t = (a.get("appointment_time") or "")[:5]
        booked_counts[t] = booked_counts.get(t, 0) + 1

    def display_time(t_str):
        h, m_ = map(int, t_str.split(":"))
        suffix = "AM" if h < 12 else "PM"
        h12 = h if h <= 12 else h - 12
        if h12 == 0: h12 = 12
        return f"{h12}:{m_:02d} {suffix}"

    now_ist = datetime.now(IST)
    past_cutoff = now_ist.strftime("%H:%M") if date == now_ist.date().isoformat() else ""

    return {
        "is_holiday": av["is_holiday"],
        "holiday_name": av.get("holiday_name"),
        "morning_enabled": av["morning"]["enabled"],
        "evening_enabled": av["evening"]["enabled"],
        "slots": [
            {
                "time":         slot,
                "display":      display_time(slot),
                "session":      session,
                "booked_count": booked_counts.get(slot, 0),
                "max":          max_slot,
                "past":         bool(past_cutoff) and slot <= past_cutoff,
                "available":    booked_counts.get(slot, 0) == 0 and not (past_cutoff and slot <= past_cutoff),
            }
            for slot, session in all_slots
        ],
    }


@app.get("/appointments/online-slots")
async def get_online_slots(doctor_id: str, date: str):
    """Return online consultation slots for a given date, with morning/evening sessions."""
    from datetime import datetime as _dt, timedelta as _td, date as _date
    doctor_res = supabase.table("doctors")\
        .select("online_consultation_enabled, online_consultation_hours")\
        .eq("id", doctor_id).single().execute()
    if not doctor_res.data:
        return {"enabled": False, "slots": []}
    doc = doctor_res.data
    if not doc.get("online_consultation_enabled"):
        return {"enabled": False, "slots": []}
    hours = doc.get("online_consultation_hours") or []
    day_name = _date.fromisoformat(date).strftime("%A").lower()
    day_entry = next((h for h in hours if h.get("day", "").lower() == day_name), None)
    if not day_entry:
        return {"enabled": True, "day_has_hours": False, "slots": []}

    cfg_res = supabase.table("clinic_config")\
        .select("config_value").eq("doctor_id", doctor_id)\
        .eq("config_key", "clinic.slot_duration").execute()
    dur = int((cfg_res.data[0]["config_value"] if cfg_res.data else None) or 15)

    booked_res = supabase.table("appointments")\
        .select("appointment_time").eq("doctor_id", doctor_id)\
        .eq("appointment_date", date).eq("consultation_type", "online")\
        .in_("status", ["Confirmed", "In Progress", "Completed"]).execute()
    booked_times = {(a["appointment_time"] or "")[:5] for a in (booked_res.data or [])}

    now_ist = datetime.now(IST)
    past_cutoff = now_ist.strftime("%H:%M") if date == now_ist.date().isoformat() else ""

    def disp(t):
        h, m = map(int, t.split(":"))
        h12 = h % 12 or 12
        return f"{h12}:{m:02d} {'AM' if h < 12 else 'PM'}"

    def gen_session_slots(start_str, end_str, session_label):
        result = []
        sh, sm = map(int, start_str.split(":"))
        eh, em = map(int, end_str.split(":"))
        cur = _dt(2000, 1, 1, sh, sm)
        end_t = _dt(2000, 1, 1, eh, em)
        while cur < end_t:
            t = cur.strftime("%H:%M")
            result.append({
                "time": t,
                "display": disp(t),
                "session": session_label,
                "available": t not in booked_times and not (past_cutoff and t <= past_cutoff),
                "past": bool(past_cutoff) and t <= past_cutoff,
            })
            cur += _td(minutes=dur)
        return result

    all_slots = []
    # New format: {day, morning:{enabled,start,end}, evening:{enabled,start,end}}
    if "morning" in day_entry or "evening" in day_entry:
        morning = day_entry.get("morning") or {}
        evening = day_entry.get("evening") or {}
        if morning.get("enabled"):
            all_slots += gen_session_slots(morning["start"], morning["end"], "morning")
        if evening.get("enabled"):
            all_slots += gen_session_slots(evening["start"], evening["end"], "evening")
    else:
        # Legacy format
        all_slots += gen_session_slots(day_entry["start"], day_entry["end"], "online")

    return {
        "enabled": True,
        "day_has_hours": bool(all_slots),
        "slots": all_slots,
    }


@app.get("/appointments/next-token")
async def get_next_token(doctor_id: str, date: str):
    """Return the next token number for a given date."""
    from database import supabase
    res = supabase.table("appointments")\
        .select("token_number")\
        .eq("doctor_id", doctor_id)\
        .eq("appointment_date", date)\
        .execute()
    max_tok = max((r.get("token_number") or 0 for r in (res.data or [])), default=0)
    return {"token": max_tok + 1}


@app.post("/appointments/book")
async def book_appointment(request: Request):
    """Book an appointment, assign token, send WhatsApp confirmation."""
    from database import supabase
    import datetime as dt

    body              = await request.json()
    patient_id        = body["patient_id"]
    doctor_id         = body["doctor_id"]
    appt_date         = body["appointment_date"]
    appt_time         = body.get("appointment_time") or ""
    visit_type        = body.get("visit_type") or "New Visit"
    consultation_type = body.get("consultation_type", "in_clinic")  # "online" | "in_clinic"

    # One active appointment per patient per day
    from database import get_active_appointment, assign_online_token
    existing = get_active_appointment(patient_id, doctor_id, appt_date)
    if existing:
        ex_time = _time_str(existing.get("appointment_time"))
        ex_disp = get_display_token(existing.get("token_number"), ex_time, doctor_id=doctor_id, date_str=appt_date)
        try:
            h = int(ex_time[:2]); h12 = h % 12 or 12
            ex_time_str = f"{h12}:{ex_time[3:5]} {'PM' if h >= 12 else 'AM'}"
        except Exception:
            ex_time_str = ex_time
        raise HTTPException(status_code=400, detail=(
            f"Patient already has an appointment on {appt_date} at {ex_time_str} "
            f"(Token {ex_disp}). To re-schedule, please cancel it and book again."
        ))

    # Today's past time slots can't be booked
    now_ist = datetime.now(IST)
    if appt_time and appt_date == now_ist.date().isoformat() \
            and _time_str(appt_time) <= now_ist.strftime("%H:%M:%S"):
        raise HTTPException(status_code=400, detail="That time slot has already passed. Please pick a later slot.")

    if consultation_type == "online":
        # Online: check only online appointments for this slot
        ob = supabase.table("appointments").select("id")\
            .eq("doctor_id", doctor_id).eq("appointment_date", appt_date)\
            .eq("appointment_time", _time_str(appt_time)).eq("consultation_type", "online")\
            .in_("status", ["Confirmed", "In Progress", "Completed"]).execute()
        if ob.data:
            raise HTTPException(status_code=400, detail="That online slot is already booked")
        token = assign_online_token(doctor_id, appt_date)
        display_tok = f"O{token}"
    else:
        # Slot must be free (Cancelled rows free the slot)
        if appt_time and not is_slot_available(doctor_id, appt_date, appt_time):
            raise HTTPException(status_code=400, detail="Slot already booked")
        token = assign_token_for_slot(doctor_id, appt_date, appt_time)
        display_tok = get_display_token(token, appt_time, doctor_id=doctor_id, date_str=appt_date)

    from database import create_appointment as db_create_appointment, create_online_appointment as db_create_online_appointment
    if consultation_type == "online":
        appt = db_create_online_appointment(patient_id, doctor_id, appt_date, appt_time, booking_source="frontend")
    else:
        appt = db_create_appointment(patient_id, doctor_id, appt_date, appt_time,
                                     token, booking_source="frontend")
    if not appt:
        raise HTTPException(status_code=500, detail="Appointment insert failed")
    appt_id = appt["id"]
    token = appt.get("token_number") or token
    if consultation_type != "online":
        display_tok = get_display_token(token, appt_time, doctor_id=doctor_id, date_str=appt_date)

    # Get patient info
    pat_res = supabase.table("patients").select("name,mobile,patient_code,language").eq("id", patient_id).single().execute()
    patient = pat_res.data or {}
    pat_name  = patient.get("name") or "Patient"
    pat_code  = patient.get("patient_code") or ""
    pat_mob   = patient.get("mobile") or ""
    language  = (patient.get("language") or "English").lower()

    # Format date nicely
    try:
        d = dt.date.fromisoformat(appt_date)
        date_display = d.strftime("%d %b %Y")
    except Exception:
        date_display = appt_date

    # Format time nicely
    def fmt_time(t_str):
        if not t_str: return t_str
        try:
            h, m_ = map(int, t_str[:5].split(":"))
            suffix = "AM" if h < 12 else "PM"
            h12 = h if h <= 12 else h - 12
            if h12 == 0: h12 = 12
            return f"{h12}:{m_:02d} {suffix}"
        except Exception:
            return t_str

    time_display = fmt_time(appt_time)

    if consultation_type == "online":
        if language == "tamil":
            msg = (
                f"✅ ஆன்லைன் சந்திப்பு உறுதிப்படுத்தப்பட்டது! 🎥\n\n"
                f"🏥 TrueCare Family Clinic\n"
                f"👤 {pat_name} ({pat_code})\n"
                f"📅 {date_display}\n"
                f"⏰ {time_display} | Token {display_tok}\n\n"
                f"Video link தனியாக அனுப்பப்படும்.\n"
                f"ரத்து செய்ய CANCEL என்று reply பண்ணுங்கள்."
            )
        else:
            msg = (
                f"✅ Online Consultation Confirmed! 🎥\n\n"
                f"🏥 TrueCare Family Clinic\n"
                f"👤 {pat_name} ({pat_code})\n"
                f"📅 {date_display}\n"
                f"⏰ {time_display} | Token {display_tok}\n\n"
                f"Your video join link will be sent shortly.\n"
                f"Reply CANCEL to cancel."
            )
    elif language == "tamil":
        msg = (
            f"✅ சந்திப்பு உறுதிப்படுத்தப்பட்டது!\n\n"
            f"🏥 TrueCare Family Clinic\n"
            f"👤 {pat_name} ({pat_code})\n"
            f"📅 {date_display}\n"
            f"⏰ {time_display} | Token {display_tok}\n\n"
            f"வரும்போது இந்த token number சொல்லுங்கள்.\n"
            f"ரத்து செய்ய CANCEL என்று reply பண்ணுங்கள்.\n"
            f"MENU — முகப்பு பக்கம்."
        )
    else:
        msg = (
            f"✅ Appointment Confirmed!\n\n"
            f"🏥 TrueCare Family Clinic\n"
            f"👤 {pat_name} ({pat_code})\n"
            f"📅 {date_display}\n"
            f"⏰ {time_display} | Token {display_tok}\n\n"
            f"Please mention your token when you arrive.\n"
            f"Reply CANCEL to cancel. Reply MENU for help."
        )

    # Send WhatsApp
    wa_sent = False
    if pat_mob:
        try:
            await send_meta_text(pat_mob, msg)
            wa_sent = True
        except Exception as e:
            print(f"❌ WhatsApp booking confirmation failed: {e}")

    # ── Online consultation: auto-create video room ───────────────────────────
    if consultation_type == "online":
        try:
            consultation = await create_consultation_for_appointment(
                supabase=supabase,
                appointment_id=appt_id,
                patient_id=patient_id,
                doctor_id=doctor_id,
                appointment_date=appt_date,
                appointment_time=appt_time,
                chief_complaint="",
            )
            if consultation and pat_mob:
                await send_video_link_to_patient(
                    mobile=pat_mob,
                    room_url=consultation["room_url"],
                    appointment_time=appt_time,
                    appointment_date=appt_date,
                    language=language,
                )
                print(f"[Online Consultation] Video link sent to {pat_mob}")
        except Exception as e:
            print(f"[Online consultation auto-create error] {e}")
    # ── End online consultation block ────────────────────────────────────────

    return {
        "appointment_id":    appt_id,
        "token_number":      token,
        "display_token":     display_tok,
        "consultation_type": consultation_type,
        "patient_name":      pat_name,
        "whatsapp_sent":     wa_sent,
    }


@app.post("/test/meta-interactive")
async def test_meta_interactive(request: Request):
    body = await request.json()
    to = body.get("to", "919047099959")
    result = await send_meta_interactive(
        to,
        "Test from PRA! How is Aadhira feeling?\n🏥 TrueCare Family Clinic",
        [
            {"id": "ok__test-followup-123", "title": "Doing well"},
            {"id": "recovering__test-followup-123", "title": "Still recovering"},
            {"id": "appt__test-followup-123", "title": "Needs appointment"}
        ],
        footer="TrueCare Family Clinic"
    )
    return result


# ── Online consultation slot helpers (now in consultation_helpers.py) ─────────
# is_online_consultation_slot / create_consultation_for_appointment imported above

# ══════════════════════════════════════════════════════════════════════════════
# ONLINE VIDEO CONSULTATIONS (JaaS / 8x8)
# ══════════════════════════════════════════════════════════════════════════════

@app.post("/consultations")
async def create_consultation(request: Request):
    body = await request.json()

    appointment_id  = body.get("appointment_id")
    patient_id      = body.get("patient_id")
    doctor_id       = body.get("doctor_id", "8c33abe0-5d2e-4613-9437-c7c375e8d162")
    scheduled_at_str = body.get("scheduled_at")
    chief_complaint  = body.get("chief_complaint", "")

    doctor_res  = supabase.table("doctors").select("name, clinic_name").eq("id", doctor_id).single().execute()
    patient_res = supabase.table("patients").select("name, mobile, language").eq("id", patient_id).single().execute()

    if not doctor_res.data or not patient_res.data:
        raise HTTPException(status_code=404, detail="Doctor or patient not found")

    if scheduled_at_str:
        scheduled_at = datetime.fromisoformat(scheduled_at_str)
    else:
        scheduled_at = datetime.now(IST)

    if scheduled_at.tzinfo is None:
        scheduled_at = IST.localize(scheduled_at)

    room_id  = generate_room_id(doctor_res.data["name"], scheduled_at)
    room_url = get_patient_join_url(room_id)

    row = supabase.table("consultations").insert({
        "appointment_id":    appointment_id,
        "patient_id":        patient_id,
        "doctor_id":         doctor_id,
        "room_id":           room_id,
        "room_url":          room_url,
        "scheduled_at":      scheduled_at.isoformat(),
        "status":            "scheduled",
        "consultation_type": "online",
        "chief_complaint":   chief_complaint,
        "patient_link_sent": False,
    }).execute()

    if appointment_id:
        supabase.table("appointments").update({"consultation_type": "online"}).eq("id", appointment_id).execute()

    return {
        "success":        True,
        "consultation":   row.data[0],
        "room_id":        room_id,
        "patient_join_url": room_url,
    }


@app.post("/consultations/{consultation_id}/send-link")
async def send_consultation_link(consultation_id: str):
    c_res = supabase.table("consultations") \
        .select("*, patients(name, mobile, language), doctors(name, clinic_name)") \
        .eq("id", consultation_id).single().execute()

    if not c_res.data:
        raise HTTPException(status_code=404, detail="Consultation not found")

    c       = c_res.data
    patient = c["patients"]
    doctor  = c["doctors"]

    scheduled_dt = datetime.fromisoformat(c["scheduled_at"].replace("Z", "+00:00")).astimezone(IST)
    formatted_time = scheduled_dt.strftime("%d %b %Y at %I:%M %p")
    lang     = (patient.get("language") or "english").lower()
    room_url = c["room_url"]

    if lang == "tamil":
        message = (
            f"வணக்கம் {patient['name']}! 🙏\n\n"
            f"உங்கள் ஆன்லைன் கன்சல்டேஷன் உறுதி!\n\n"
            f"📅 {formatted_time}\n"
            f"👨‍⚕️ {doctor['name']}\n"
            f"🏥 {doctor['clinic_name']}\n\n"
            f"📱 Join link:\n{room_url}\n\n"
            f"✅ Download தேவையில்லை\n"
            f"✅ Login தேவையில்லை\n"
            f"நேரத்தில் join ஆகுங்கள். நன்றி!"
        )
    elif lang == "hindi":
        message = (
            f"नमस्ते {patient['name']}! 🙏\n\n"
            f"आपकी ऑनलाइन कंसल्टेशन कन्फर्म!\n\n"
            f"📅 {formatted_time}\n"
            f"👨‍⚕️ {doctor['name']}\n"
            f"🏥 {doctor['clinic_name']}\n\n"
            f"📱 Join link:\n{room_url}\n\n"
            f"✅ कोई डाउनलोड नहीं\n"
            f"✅ कोई लॉगिन नहीं\n"
            f"समय पर जॉइन करें। धन्यवाद!"
        )
    else:
        message = (
            f"Hello {patient['name']}! 🙏\n\n"
            f"Your online consultation is confirmed.\n\n"
            f"📅 {formatted_time}\n"
            f"👨‍⚕️ {doctor['name']}\n"
            f"🏥 {doctor['clinic_name']}\n\n"
            f"📱 Click to join:\n{room_url}\n\n"
            f"✅ No download needed\n"
            f"✅ No login required\n"
            f"✅ Just click the link at appointment time\n\n"
            f"Please join on time. Thank you!"
        )

    await send_meta_text(patient["mobile"], message)

    supabase.table("consultations").update({
        "patient_link_sent":    True,
        "patient_link_sent_at": datetime.now(IST).isoformat(),
    }).eq("id", consultation_id).execute()

    return {"success": True}


@app.get("/consultations/today")
async def todays_consultations(doctor_id: str = "8c33abe0-5d2e-4613-9437-c7c375e8d162"):
    today = datetime.now(IST).date().isoformat()
    result = supabase.table("consultations") \
        .select("*, patients(name, mobile, language)") \
        .eq("doctor_id", doctor_id) \
        .gte("scheduled_at", f"{today}T00:00:00+05:30") \
        .lte("scheduled_at", f"{today}T23:59:59+05:30") \
        .order("scheduled_at") \
        .execute()
    return {"consultations": result.data or []}


@app.get("/consultations")
async def list_consultations(
    doctor_id: str = "8c33abe0-5d2e-4613-9437-c7c375e8d162",
    status: str = None,
    date: str = None,
    date_from: str = None,
    date_to: str = None,
):
    q = supabase.table("consultations") \
        .select("*, patients(name, mobile)") \
        .eq("doctor_id", doctor_id) \
        .order("scheduled_at", desc=False)

    if date:
        q = q.gte("scheduled_at", f"{date}T00:00:00+05:30").lte("scheduled_at", f"{date}T23:59:59+05:30")
    if date_from:
        q = q.gte("scheduled_at", f"{date_from}T00:00:00+05:30")
    if date_to:
        q = q.lte("scheduled_at", f"{date_to}T23:59:59+05:30")
    if status:
        status_list = [s.strip() for s in status.split(",")]
        q = q.in_("status", status_list)

    result = q.execute()
    return {"consultations": result.data or []}


@app.get("/consultations/{consultation_id}/doctor-token")
async def get_doctor_token(consultation_id: str):
    c_res = supabase.table("consultations") \
        .select("*, doctors(name), patients(name)") \
        .eq("id", consultation_id).single().execute()

    if not c_res.data:
        raise HTTPException(status_code=404, detail="Consultation not found")

    c = c_res.data
    token = generate_jaas_jwt(
        room_name    = c["room_id"],
        user_name    = c["doctors"]["name"],
        user_email   = "doctor@praclinic.in",
        is_moderator = True,
    )

    # Only flip to in_progress if still scheduled
    supabase.table("consultations").update({
        "status":     "in_progress",
        "started_at": datetime.now(IST).isoformat(),
    }).eq("id", consultation_id).eq("status", "scheduled").execute()

    return {
        "token":        token,
        "room_id":      c["room_id"],
        "app_id":       JAAS_APP_ID,
        "patient_name": c["patients"]["name"],
        "domain":       "8x8.vc",
    }


@app.patch("/consultations/{consultation_id}/complete")
async def complete_consultation(consultation_id: str, request: Request):
    body = await request.json()

    c_res = supabase.table("consultations").select("started_at").eq("id", consultation_id).single().execute()

    duration_minutes = None
    if c_res.data and c_res.data.get("started_at"):
        start = datetime.fromisoformat(c_res.data["started_at"].replace("Z", "+00:00"))
        duration_minutes = max(1, int((datetime.now(IST) - start).total_seconds() / 60))

    supabase.table("consultations").update({
        "status":           "completed",
        "ended_at":         datetime.now(IST).isoformat(),
        "duration_minutes": duration_minutes,
        "doctor_notes":     body.get("doctor_notes", ""),
    }).eq("id", consultation_id).execute()

    return {"success": True, "duration_minutes": duration_minutes}


@app.patch("/consultations/{consultation_id}/cancel")
async def cancel_consultation(consultation_id: str):
    from database import supabase
    c_res = supabase.table("consultations").select("appointment_id").eq("id", consultation_id).single().execute()
    supabase.table("consultations").update({"status": "cancelled"}).eq("id", consultation_id).execute()
    if c_res.data and c_res.data.get("appointment_id"):
        supabase.table("appointments").update({"status": "Cancelled"}).eq("id", c_res.data["appointment_id"]).execute()
    return {"success": True}


@app.patch("/consultations/{consultation_id}/no-show")
async def no_show_consultation(consultation_id: str):
    from database import supabase
    c_res = supabase.table("consultations").select("appointment_id").eq("id", consultation_id).single().execute()
    supabase.table("consultations").update({"status": "missed"}).eq("id", consultation_id).execute()
    if c_res.data and c_res.data.get("appointment_id"):
        supabase.table("appointments").update({"status": "No Show"}).eq("id", c_res.data["appointment_id"]).execute()
    return {"success": True}


@app.patch("/doctors/{doctor_id}/online-settings")
async def update_online_settings(doctor_id: str, request: Request):
    body = await request.json()
    update_data = {}
    if "online_consultation_enabled" in body:
        update_data["online_consultation_enabled"] = body["online_consultation_enabled"]
    if "online_consultation_hours" in body:
        update_data["online_consultation_hours"] = body["online_consultation_hours"]
    if "online_consultation_fee" in body:
        update_data["online_consultation_fee"] = body["online_consultation_fee"]

    supabase.table("doctors").update(update_data).eq("id", doctor_id).execute()
    return {"success": True}


@app.get("/doctors/{doctor_id}/online-settings")
async def get_online_settings(doctor_id: str):
    result = supabase.table("doctors") \
        .select("online_consultation_enabled, online_consultation_hours, online_consultation_fee") \
        .eq("id", doctor_id).single().execute()
    return result.data or {
        "online_consultation_enabled": False,
        "online_consultation_hours":   [],
        "online_consultation_fee":     0,
    }


@app.get("/test/jaas-key-debug")
async def jaas_key_debug():
    """Show key metadata (NOT the key itself) for diagnosing Railway env var issues."""
    raw = JAAS_PRIVATE_KEY_STR
    has_literal_backslash_n = "\\n" in raw
    has_real_newline = "\n" in raw
    normalised = raw.replace("\\n", "\n").strip()
    lines = normalised.splitlines()
    return {
        "app_id": JAAS_APP_ID,
        "api_key_id": JAAS_API_KEY_ID,
        "key_total_chars": len(raw),
        "has_literal_backslash_n": has_literal_backslash_n,
        "has_real_newline": has_real_newline,
        "first_80_chars_repr": repr(raw[:80]),
        "normalised_line_count": len(lines),
        "first_line": lines[0] if lines else "",
        "last_line": lines[-1] if lines else "",
    }


@app.post("/test/create-consultation")
async def test_create_consultation():
    """Quick smoke-test: creates a consultation 30 mins from now for the test patient."""
    from starlette.requests import Request as StarletteRequest
    scheduled = datetime.now(IST) + timedelta(minutes=1)

    class _FakeRequest:
        async def json(self):
            return {
                "patient_id":       "aaaaaaaa-0000-0000-0000-000000000001",
                "doctor_id":        "8c33abe0-5d2e-4613-9437-c7c375e8d162",
                "scheduled_at":     scheduled.isoformat(),
                "chief_complaint":  "Test consultation",
            }

    return await create_consultation(_FakeRequest())

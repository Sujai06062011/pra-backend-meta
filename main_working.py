from fastapi import FastAPI, Request, Form
from fastapi.responses import PlainTextResponse
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse
from dotenv import load_dotenv
import os
from whatsapp_handler import handle_message
import asyncio

load_dotenv()

app = FastAPI(title="PRA - Patient Relationship Assistant")

# Twilio client
twilio_client = Client(
    os.getenv("TWILIO_ACCOUNT_SID"),
    os.getenv("TWILIO_AUTH_TOKEN")
)

TWILIO_FROM = os.getenv("TWILIO_WHATSAPP_FROM")


def send_whatsapp(to_number: str, message: str):
    """Send WhatsApp message via Twilio"""
    twilio_client.messages.create(
        from_=TWILIO_FROM,
        to=f"whatsapp:+{to_number}",
        body=message
    )


@app.get("/")
async def root():
    return {"status": "PRA Backend Running", "version": "1.0.0"}


@app.get("/health")
async def health():
    return {"status": "ok"}


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

        send_whatsapp(from_number, reply)

    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()

    # Return empty TwiML — stops Twilio from sending "OK" as message
    resp = MessagingResponse()
    return PlainTextResponse(str(resp), status_code=200, media_type="application/xml")


@app.get("/webhook/whatsapp")
async def whatsapp_verify(request: Request):
    """Handle Meta webhook verification"""
    params = dict(request.query_params)
    challenge = params.get("hub.challenge", "")
    return PlainTextResponse(challenge)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)

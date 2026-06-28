"""
auth.py — PIN-based staff authentication helpers.
"""
import hashlib
import os
import jwt
from datetime import datetime, timedelta, timezone

JWT_SECRET = os.getenv("JWT_SECRET", "pra-clinic-jwt-secret-change-in-prod")
JWT_ALGORITHM = "HS256"
JWT_EXPIRY_HOURS = 12


def hash_pin(pin: str) -> str:
    return hashlib.sha256(pin.strip().encode()).hexdigest()


def create_token(staff: dict) -> str:
    payload = {
        "sub": staff["id"],
        "username": staff["username"],
        "role": staff["role"],
        "doctor_id": staff.get("doctor_id"),
        "clinic_whatsapp": staff["clinic_whatsapp"],
        "name": staff["name"],
        "exp": datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRY_HOURS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except Exception:
        return None


async def login_staff(supabase, username: str, pin: str) -> dict | None:
    """Return staff record + token if credentials are valid, else None."""
    try:
        pin_hash = hash_pin(pin)
        res = supabase.table("clinic_staff") \
            .select("*") \
            .eq("username", username.strip().lower()) \
            .eq("pin_hash", pin_hash) \
            .eq("is_active", True) \
            .limit(1).execute()
        rows = res.data or []
        if not rows:
            return None
        staff = rows[0]
        token = create_token(staff)
        return {
            "token": token,
            "id": staff["id"],
            "name": staff["name"],
            "username": staff["username"],
            "role": staff["role"],
            "doctor_id": staff.get("doctor_id"),
            "clinic_whatsapp": staff["clinic_whatsapp"],
        }
    except Exception as e:
        print(f"[AUTH] login error: {e}")
        return None

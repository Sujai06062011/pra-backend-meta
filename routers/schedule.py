"""
Clinic Weekly Schedule
Per-day-of-week default hours (middle tier between clinic_config and per-date overrides).
"""

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
from database import supabase

router = APIRouter(prefix="/schedule", tags=["schedule"])

DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


@router.get("")
async def get_weekly_schedule(doctor_id: str = Query(...)):
    """Return per-day schedule for all 7 days, filling gaps from clinic_config."""
    from routers.availability import get_full_clinic_config, _trim
    cfg = get_full_clinic_config(doctor_id)

    res = supabase.table("clinic_schedule") \
        .select("*") \
        .eq("doctor_id", doctor_id) \
        .execute()

    by_day = {r["day_of_week"]: r for r in (res.data or [])}

    result = []
    for day in DAYS:
        row = by_day.get(day)
        if row:
            result.append({
                "day_of_week": day,
                "is_closed": bool(row.get("is_closed", False)),
                "morning_enabled": bool(row.get("morning_enabled", True)),
                "morning_start": _trim(row.get("morning_start")) or cfg["morning_start"],
                "morning_end": _trim(row.get("morning_end")) or cfg["morning_end"],
                "evening_enabled": bool(row.get("evening_enabled", True)),
                "evening_start": _trim(row.get("evening_start")) or cfg["evening_start"],
                "evening_end": _trim(row.get("evening_end")) or cfg["evening_end"],
                "has_override": True,
            })
        else:
            result.append({
                "day_of_week": day,
                "is_closed": False,
                "morning_enabled": True,
                "morning_start": cfg["morning_start"],
                "morning_end": cfg["morning_end"],
                "evening_enabled": True,
                "evening_start": cfg["evening_start"],
                "evening_end": cfg["evening_end"],
                "has_override": False,
            })
    return result


class ScheduleDayPayload(BaseModel):
    doctor_id: str
    day_of_week: str
    is_closed: bool = False
    morning_enabled: bool = True
    morning_start: Optional[str] = None
    morning_end: Optional[str] = None
    evening_enabled: bool = True
    evening_start: Optional[str] = None
    evening_end: Optional[str] = None


@router.post("")
async def set_schedule_day(payload: ScheduleDayPayload):
    """Upsert schedule for one day of the week."""
    if payload.day_of_week not in DAYS:
        return JSONResponse(status_code=400, content={"error": f"Invalid day: {payload.day_of_week}"})

    now = datetime.utcnow().isoformat()
    record = {
        "doctor_id":       payload.doctor_id,
        "day_of_week":     payload.day_of_week,
        "is_closed":       payload.is_closed,
        "morning_enabled": payload.morning_enabled,
        "morning_start":   payload.morning_start,
        "morning_end":     payload.morning_end,
        "evening_enabled": payload.evening_enabled,
        "evening_start":   payload.evening_start,
        "evening_end":     payload.evening_end,
        "updated_at":      now,
    }
    result = supabase.table("clinic_schedule").upsert(
        record, on_conflict="doctor_id,day_of_week"
    ).execute()
    return result.data[0] if result.data else record


@router.delete("/{day_of_week}")
async def delete_schedule_day(day_of_week: str, doctor_id: str = Query(...)):
    """Remove schedule override for a day — falls back to clinic_config defaults."""
    supabase.table("clinic_schedule") \
        .delete() \
        .eq("doctor_id", doctor_id) \
        .eq("day_of_week", day_of_week) \
        .execute()
    return {"deleted": True, "day_of_week": day_of_week}

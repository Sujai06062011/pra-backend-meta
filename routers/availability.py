"""
Clinic Availability Management
Handles per-date overrides: holidays, session blocking, custom time windows.

Logic:
  - No record for a date → clinic fully open using clinic_config defaults
  - Record exists → override applies
  - morning_start/end NULL → use clinic_config defaults
"""

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, date as _date, timedelta
from database import supabase

router = APIRouter(prefix="/availability", tags=["availability"])


# ── Shared helpers (also imported by whatsapp_handler) ────────────────────────

def get_full_clinic_config(doctor_id: str) -> dict:
    """Fetch slot config from clinic_config (start/end for both sessions + duration)."""
    res = supabase.table("clinic_config") \
        .select("config_key,config_value") \
        .eq("doctor_id", doctor_id) \
        .in_("config_key", [
            "clinic.slot_start_morning", "clinic.slot_end_morning",
            "clinic.slot_start_evening", "clinic.slot_end_evening",
            "clinic.slot_duration_minutes",
        ]).execute()
    cfg = {r["config_key"]: r["config_value"] for r in (res.data or [])}
    return {
        "morning_start": cfg.get("clinic.slot_start_morning", "09:30"),
        "morning_end":   cfg.get("clinic.slot_end_morning",   "14:30"),
        "evening_start": cfg.get("clinic.slot_start_evening", "17:00"),
        "evening_end":   cfg.get("clinic.slot_end_evening",   "22:00"),
        "duration":      int(cfg.get("clinic.slot_duration_minutes", "10")),
    }


def _trim(t: Optional[str]) -> Optional[str]:
    """Trim TIME value to HH:MM."""
    return t[:5] if t else None


def _fetch_all_clinic_config(doctor_id: str) -> dict:
    """Fetch ALL clinic_config rows as a flat key→value dict."""
    res = supabase.table("clinic_config") \
        .select("config_key,config_value") \
        .eq("doctor_id", doctor_id) \
        .execute()
    return {r["config_key"]: r["config_value"] for r in (res.data or [])}


def _day_sched_from_cfg(all_cfg: dict, day_name: str, cfg: dict) -> Optional[dict]:
    """
    Extract day-of-week schedule from clinic_config keys.
    Returns None if no schedule keys found for this day (falls through to global defaults).
    """
    p = f"clinic.schedule.{day_name}"
    if f"{p}.enabled" not in all_cfg:
        return None
    enabled = all_cfg[f"{p}.enabled"] == "true"
    return {
        "is_closed": not enabled,
        "morning_enabled": all_cfg.get(f"{p}.morning_enabled", "true") == "true",
        "morning_start": (all_cfg.get(f"{p}.morning_start") or cfg["morning_start"])[:5],
        "morning_end":   (all_cfg.get(f"{p}.morning_end")   or cfg["morning_end"])[:5],
        "evening_enabled": all_cfg.get(f"{p}.evening_enabled", "true") == "true",
        "evening_start": (all_cfg.get(f"{p}.evening_start") or cfg["evening_start"])[:5],
        "evening_end":   (all_cfg.get(f"{p}.evening_end")   or cfg["evening_end"])[:5],
    }


def _resolve_from_schedule(sched: dict, cfg: dict, date_str: str) -> dict:
    """Build availability dict from a clinic_schedule row."""
    if sched.get("is_closed", False):
        return {
            "date": date_str,
            "is_holiday": True,
            "holiday_name": None,
            "has_override": False,
            "morning": {"enabled": False, "start": cfg["morning_start"], "end": cfg["morning_end"]},
            "evening": {"enabled": False, "start": cfg["evening_start"], "end": cfg["evening_end"]},
        }
    return {
        "date": date_str,
        "is_holiday": False,
        "holiday_name": None,
        "has_override": False,
        "morning": {
            "enabled": bool(sched.get("morning_enabled", True)),
            "start": _trim(sched.get("morning_start")) or cfg["morning_start"],
            "end":   _trim(sched.get("morning_end"))   or cfg["morning_end"],
        },
        "evening": {
            "enabled": bool(sched.get("evening_enabled", True)),
            "start": _trim(sched.get("evening_start")) or cfg["evening_start"],
            "end":   _trim(sched.get("evening_end"))   or cfg["evening_end"],
        },
    }


def get_availability_for_date(
    doctor_id: str,
    date_str: str,
    _all_cfg: Optional[dict] = None,
) -> dict:
    """
    Core availability resolver. Three-tier precedence:
      1. clinic_availability (per-date override) — highest
      2. clinic_config schedule keys (per-day-of-week defaults)
      3. clinic_config global defaults — lowest

    Pass _all_cfg to reuse a pre-fetched config dict (avoids extra DB calls in loops).
    """
    all_cfg = _all_cfg if _all_cfg is not None else _fetch_all_clinic_config(doctor_id)
    cfg = {
        "morning_start": (all_cfg.get("clinic.slot_start_morning") or "09:30")[:5],
        "morning_end":   (all_cfg.get("clinic.slot_end_morning")   or "14:30")[:5],
        "evening_start": (all_cfg.get("clinic.slot_start_evening") or "17:00")[:5],
        "evening_end":   (all_cfg.get("clinic.slot_end_evening")   or "22:00")[:5],
        "duration":      int(all_cfg.get("clinic.slot_duration_minutes", "10")),
    }

    # Tier 1: per-date override
    res = supabase.table("clinic_availability") \
        .select("*") \
        .eq("doctor_id", doctor_id) \
        .eq("availability_date", date_str) \
        .execute()
    row = res.data[0] if res.data else None

    if row:
        return {
            "date": date_str,
            "is_holiday": bool(row.get("is_holiday", False)),
            "holiday_name": row.get("holiday_name"),
            "has_override": True,
            "morning": {
                "enabled": bool(row.get("morning_enabled", True)),
                "start": _trim(row.get("morning_start")) or cfg["morning_start"],
                "end":   _trim(row.get("morning_end"))   or cfg["morning_end"],
            },
            "evening": {
                "enabled": bool(row.get("evening_enabled", True)),
                "start": _trim(row.get("evening_start")) or cfg["evening_start"],
                "end":   _trim(row.get("evening_end"))   or cfg["evening_end"],
            },
        }

    # Tier 2: weekly schedule keys in clinic_config
    day_name = _date.fromisoformat(date_str).strftime("%A").lower()
    sched = _day_sched_from_cfg(all_cfg, day_name, cfg)
    if sched:
        return _resolve_from_schedule(sched, cfg, date_str)

    # Tier 3: global clinic_config defaults
    return {
        "date": date_str,
        "is_holiday": False,
        "holiday_name": None,
        "has_override": False,
        "morning": {"enabled": True, "start": cfg["morning_start"], "end": cfg["morning_end"]},
        "evening": {"enabled": True, "start": cfg["evening_start"], "end": cfg["evening_end"]},
    }


def get_next_open_date(doctor_id: str, from_date_str: str) -> Optional[str]:
    """
    Find the first non-holiday date with at least one session enabled,
    starting from from_date_str (exclusive), up to 14 days ahead.
    """
    all_cfg = _fetch_all_clinic_config(doctor_id)
    start = datetime.strptime(from_date_str, "%Y-%m-%d").date()
    for i in range(1, 15):
        candidate = (start + timedelta(days=i)).isoformat()
        av = get_availability_for_date(doctor_id, candidate, _all_cfg=all_cfg)
        if not av["is_holiday"] and (av["morning"]["enabled"] or av["evening"]["enabled"]):
            return candidate
    return None


def _t2m(t: str) -> int:
    """Convert HH:MM to minutes since midnight."""
    h, m = map(int, t[:5].split(":"))
    return h * 60 + m


def fmt12(t: str) -> str:
    """Format HH:MM as 12-hour display string."""
    h, m = map(int, t[:5].split(":"))
    suffix = "PM" if h >= 12 else "AM"
    h12 = h % 12 or 12
    return f"{h12}:{m:02d} {suffix}"


def generate_slots_for_date(doctor_id: str, date_str: str) -> list[dict]:
    """
    Generate available slot list respecting availability overrides.
    Used by WhatsApp handler to offer slots dynamically.
    """
    av = get_availability_for_date(doctor_id, date_str)
    cfg = get_full_clinic_config(doctor_id)
    dur = cfg["duration"]

    if av["is_holiday"]:
        return []

    slots = []

    def gen(start_str: str, end_str: str, session: str):
        from datetime import datetime as _dt, timedelta as _td
        t = _dt(2000, 1, 1, *map(int, start_str.split(":")))
        end = _dt(2000, 1, 1, *map(int, end_str.split(":")))
        while t < end:
            slots.append({"time": t.strftime("%H:%M"), "session": session})
            t += _td(minutes=dur)

    if av["morning"]["enabled"]:
        gen(av["morning"]["start"], av["morning"]["end"], "morning")
    if av["evening"]["enabled"]:
        gen(av["evening"]["start"], av["evening"]["end"], "evening")

    return slots


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.get("/range")
async def get_availability_range(
    start_date: str = Query(...),
    end_date:   str = Query(...),
    doctor_id:  str = Query(...),
):
    """Get availability for a date range (calendar view). Respects 3-tier precedence."""
    all_cfg = _fetch_all_clinic_config(doctor_id)
    cfg = {
        "morning_start": (all_cfg.get("clinic.slot_start_morning") or "09:30")[:5],
        "morning_end":   (all_cfg.get("clinic.slot_end_morning")   or "14:30")[:5],
        "evening_start": (all_cfg.get("clinic.slot_start_evening") or "17:00")[:5],
        "evening_end":   (all_cfg.get("clinic.slot_end_evening")   or "22:00")[:5],
        "duration":      int(all_cfg.get("clinic.slot_duration_minutes", "10")),
    }

    # Fetch per-date overrides for range
    res = supabase.table("clinic_availability") \
        .select("*") \
        .eq("doctor_id", doctor_id) \
        .gte("availability_date", start_date) \
        .lte("availability_date", end_date) \
        .execute()
    overrides = {r["availability_date"]: r for r in (res.data or [])}

    start = datetime.strptime(start_date, "%Y-%m-%d").date()
    end   = datetime.strptime(end_date,   "%Y-%m-%d").date()

    results = []
    current = start
    while current <= end:
        date_str = current.isoformat()
        row = overrides.get(date_str)
        if row:
            results.append({
                "date": date_str,
                "is_holiday": bool(row.get("is_holiday", False)),
                "holiday_name": row.get("holiday_name"),
                "has_override": True,
                "morning": {
                    "enabled": bool(row.get("morning_enabled", True)),
                    "start": _trim(row.get("morning_start")) or cfg["morning_start"],
                    "end":   _trim(row.get("morning_end"))   or cfg["morning_end"],
                },
                "evening": {
                    "enabled": bool(row.get("evening_enabled", True)),
                    "start": _trim(row.get("evening_start")) or cfg["evening_start"],
                    "end":   _trim(row.get("evening_end"))   or cfg["evening_end"],
                },
            })
        else:
            day_name = current.strftime("%A").lower()
            sched = _day_sched_from_cfg(all_cfg, day_name, cfg)
            if sched:
                results.append(_resolve_from_schedule(sched, cfg, date_str))
            else:
                results.append({
                    "date": date_str,
                    "is_holiday": False,
                    "holiday_name": None,
                    "has_override": False,
                    "morning": {"enabled": True, "start": cfg["morning_start"], "end": cfg["morning_end"]},
                    "evening": {"enabled": True, "start": cfg["evening_start"], "end": cfg["evening_end"]},
                })
        current += timedelta(days=1)

    return results


@router.get("")
async def get_availability(date: str = Query(...), doctor_id: str = Query(...)):
    """Get availability for a specific date."""
    return get_availability_for_date(doctor_id, date)


class AvailabilityPayload(BaseModel):
    doctor_id: str
    availability_date: str
    is_holiday: bool = False
    holiday_name: Optional[str] = None
    morning_enabled: bool = True
    morning_start: Optional[str] = None
    morning_end: Optional[str] = None
    evening_enabled: bool = True
    evening_start: Optional[str] = None
    evening_end: Optional[str] = None
    reason: Optional[str] = None
    created_by: Optional[str] = None


@router.post("")
async def set_availability(payload: AvailabilityPayload):
    """Upsert availability for a date with validation."""
    today = _date.today().isoformat()
    if payload.availability_date < today:
        return JSONResponse(status_code=400, content={"error": "Cannot set availability for past dates."})

    cfg = get_full_clinic_config(payload.doctor_id)

    if not payload.is_holiday:
        if payload.morning_enabled:
            ms = payload.morning_start or cfg["morning_start"]
            me = payload.morning_end   or cfg["morning_end"]
            if _t2m(ms) < _t2m(cfg["morning_start"]):
                return JSONResponse(status_code=400, content={
                    "error": f"Morning start time cannot be before clinic hours ({cfg['morning_start']}). "
                             f"Please select a time between {cfg['morning_start']} and {cfg['morning_end']}."
                })
            if _t2m(me) > _t2m(cfg["morning_end"]):
                return JSONResponse(status_code=400, content={
                    "error": f"Morning end time cannot be after clinic hours ({cfg['morning_end']}). "
                             f"Please select a time between {cfg['morning_start']} and {cfg['morning_end']}."
                })
            if _t2m(ms) >= _t2m(me):
                return JSONResponse(status_code=400, content={"error": "Morning start time must be before end time."})

        if payload.evening_enabled:
            es = payload.evening_start or cfg["evening_start"]
            ee = payload.evening_end   or cfg["evening_end"]
            if _t2m(es) < _t2m(cfg["evening_start"]):
                return JSONResponse(status_code=400, content={
                    "error": f"Evening start time cannot be before clinic hours ({cfg['evening_start']}). "
                             f"Please select a time between {cfg['evening_start']} and {cfg['evening_end']}."
                })
            if _t2m(ee) > _t2m(cfg["evening_end"]):
                return JSONResponse(status_code=400, content={
                    "error": f"Evening end time cannot be after clinic hours ({cfg['evening_end']}). "
                             f"Please select a time between {cfg['evening_start']} and {cfg['evening_end']}."
                })
            if _t2m(es) >= _t2m(ee):
                return JSONResponse(status_code=400, content={"error": "Evening start time must be before end time."})

    now = datetime.utcnow().isoformat()
    record = {
        "doctor_id":          payload.doctor_id,
        "availability_date":  payload.availability_date,
        "is_holiday":         payload.is_holiday,
        "holiday_name":       payload.holiday_name,
        "morning_enabled":    payload.morning_enabled,
        "morning_start":      payload.morning_start,
        "morning_end":        payload.morning_end,
        "evening_enabled":    payload.evening_enabled,
        "evening_start":      payload.evening_start,
        "evening_end":        payload.evening_end,
        "reason":             payload.reason,
        "created_by":         payload.created_by,
        "updated_at":         now,
    }

    result = supabase.table("clinic_availability").upsert(
        record, on_conflict="doctor_id,availability_date"
    ).execute()

    return result.data[0] if result.data else record


@router.delete("/{avail_date}")
async def delete_availability(avail_date: str, doctor_id: str = Query(...)):
    """Delete availability override — restores date to full clinic defaults."""
    supabase.table("clinic_availability") \
        .delete() \
        .eq("doctor_id", doctor_id) \
        .eq("availability_date", avail_date) \
        .execute()
    return {"deleted": True, "date": avail_date}


class BlockFromCancelPayload(BaseModel):
    doctor_id: str
    date: str
    block_type: str  # "morning" | "evening" | "full_day"


@router.post("/block-from-cancel")
async def block_from_cancel(payload: BlockFromCancelPayload):
    """Block slots after bulk cancel. Preserves the other session's existing settings."""
    res = supabase.table("clinic_availability") \
        .select("*") \
        .eq("doctor_id", payload.doctor_id) \
        .eq("availability_date", payload.date) \
        .execute()

    existing = res.data[0] if res.data else {}
    now = datetime.utcnow().isoformat()

    record = {
        "doctor_id":         payload.doctor_id,
        "availability_date": payload.date,
        "is_holiday":        existing.get("is_holiday", False),
        "morning_enabled":   existing.get("morning_enabled", True),
        "morning_start":     existing.get("morning_start"),
        "morning_end":       existing.get("morning_end"),
        "evening_enabled":   existing.get("evening_enabled", True),
        "evening_start":     existing.get("evening_start"),
        "evening_end":       existing.get("evening_end"),
        "updated_at":        now,
    }

    if payload.block_type == "morning":
        record["morning_enabled"] = False
    elif payload.block_type == "evening":
        record["evening_enabled"] = False
    elif payload.block_type == "full_day":
        record["is_holiday"] = True
        record["morning_enabled"] = False
        record["evening_enabled"] = False

    result = supabase.table("clinic_availability").upsert(
        record, on_conflict="doctor_id,availability_date"
    ).execute()

    return result.data[0] if result.data else record

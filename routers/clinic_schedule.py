"""
Clinic Weekly Schedule
Reads/writes per-day schedule from clinic_config keys:
  clinic.schedule.{day}.enabled
  clinic.schedule.{day}.morning_enabled / morning_start / morning_end
  clinic.schedule.{day}.evening_enabled / evening_start / evening_end

Global boundary keys (slot_start_morning etc.) act as max-allowed limits.
"""

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Dict
from datetime import datetime
from database import supabase

router = APIRouter(prefix="/clinic", tags=["clinic_schedule"])

DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _t2m(t: str) -> int:
    h, m = map(int, t[:5].split(":"))
    return h * 60 + m


def _all_cfg(doctor_id: str) -> dict:
    res = supabase.table("clinic_config") \
        .select("config_key,config_value") \
        .eq("doctor_id", doctor_id) \
        .execute()
    return {r["config_key"]: r["config_value"] for r in (res.data or [])}


def _build_response(cfg: dict) -> dict:
    ms = cfg.get("clinic.slot_start_morning", "09:30")
    me = cfg.get("clinic.slot_end_morning", "14:30")
    es = cfg.get("clinic.slot_start_evening", "17:00")
    ee = cfg.get("clinic.slot_end_evening", "22:00")

    schedule = {}
    for day in DAYS:
        p = f"clinic.schedule.{day}"
        default_open = "false" if day == "sunday" else "true"
        enabled = cfg.get(f"{p}.enabled", default_open) == "true"
        schedule[day] = {
            "enabled": enabled,
            "morning": {
                "enabled": cfg.get(f"{p}.morning_enabled", "true") == "true",
                "start": (cfg.get(f"{p}.morning_start") or ms)[:5],
                "end":   (cfg.get(f"{p}.morning_end")   or me)[:5],
            },
            "evening": {
                "enabled": cfg.get(f"{p}.evening_enabled", "true") == "true",
                "start": (cfg.get(f"{p}.evening_start") or es)[:5],
                "end":   (cfg.get(f"{p}.evening_end")   or ee)[:5],
            },
        }

    return {
        "slot_duration_minutes": int(cfg.get("clinic.slot_duration_minutes", "10")),
        "max_per_slot": int(cfg.get("clinic.max_per_slot", "3")),
        "boundaries": {"morning_start": ms, "morning_end": me, "evening_start": es, "evening_end": ee},
        "schedule": schedule,
    }


@router.get("/schedule")
async def get_clinic_schedule(doctor_id: str = Query(...)):
    return _build_response(_all_cfg(doctor_id))


@router.get("/schedule/{day}")
async def get_day_schedule(day: str, doctor_id: str = Query(...)):
    if day not in DAYS:
        return JSONResponse(status_code=400, content={"error": f"Invalid day: {day}"})
    resp = _build_response(_all_cfg(doctor_id))
    return {
        "day": day,
        "slot_duration_minutes": resp["slot_duration_minutes"],
        "boundaries": resp["boundaries"],
        **resp["schedule"][day],
    }


# ── PUT payload ────────────────────────────────────────────────────────────────

class SessionIn(BaseModel):
    enabled: bool
    start: str
    end: str

class DayIn(BaseModel):
    enabled: bool
    morning: SessionIn
    evening: SessionIn

class SchedulePutPayload(BaseModel):
    doctor_id: str
    slot_duration_minutes: int
    max_per_slot: int
    schedule: Dict[str, DayIn]


@router.put("/schedule")
async def put_clinic_schedule(payload: SchedulePutPayload):
    cfg = _all_cfg(payload.doctor_id)
    b = {
        "ms": cfg.get("clinic.slot_start_morning", "09:30"),
        "me": cfg.get("clinic.slot_end_morning",   "14:30"),
        "es": cfg.get("clinic.slot_start_evening", "17:00"),
        "ee": cfg.get("clinic.slot_end_evening",   "22:00"),
    }

    if payload.slot_duration_minutes <= 0:
        return JSONResponse(status_code=400, content={"error": "Slot duration must be greater than 0."})
    if payload.max_per_slot <= 0:
        return JSONResponse(status_code=400, content={"error": "Max per slot must be greater than 0."})

    for day, ds in payload.schedule.items():
        if day not in DAYS:
            return JSONResponse(status_code=400, content={"error": f"Invalid day: {day}"})
        if not ds.enabled:
            continue
        if ds.morning.enabled:
            ms2, me2 = ds.morning.start[:5], ds.morning.end[:5]
            if _t2m(ms2) < _t2m(b["ms"]):
                return JSONResponse(status_code=400, content={
                    "error": f"{day.title()} morning start cannot be before clinic boundary {b['ms']}."
                })
            if _t2m(me2) > _t2m(b["me"]):
                return JSONResponse(status_code=400, content={
                    "error": f"{day.title()} morning end cannot be after clinic boundary {b['me']}."
                })
            if _t2m(ms2) >= _t2m(me2):
                return JSONResponse(status_code=400, content={
                    "error": f"{day.title()} morning start must be before end time."
                })
        if ds.evening.enabled:
            es2, ee2 = ds.evening.start[:5], ds.evening.end[:5]
            if _t2m(es2) < _t2m(b["es"]):
                return JSONResponse(status_code=400, content={
                    "error": f"{day.title()} evening start cannot be before clinic boundary {b['es']}."
                })
            if _t2m(ee2) > _t2m(b["ee"]):
                return JSONResponse(status_code=400, content={
                    "error": f"{day.title()} evening end cannot be after clinic boundary {b['ee']}."
                })
            if _t2m(es2) >= _t2m(ee2):
                return JSONResponse(status_code=400, content={
                    "error": f"{day.title()} evening start must be before end time."
                })

    now = datetime.utcnow().isoformat()
    rows = [
        {"doctor_id": payload.doctor_id, "config_key": "clinic.slot_duration_minutes",
         "config_value": str(payload.slot_duration_minutes), "updated_at": now},
        {"doctor_id": payload.doctor_id, "config_key": "clinic.max_per_slot",
         "config_value": str(payload.max_per_slot), "updated_at": now},
    ]
    for day, ds in payload.schedule.items():
        p = f"clinic.schedule.{day}"
        rows += [
            {"doctor_id": payload.doctor_id, "config_key": f"{p}.enabled",
             "config_value": "true" if ds.enabled else "false", "updated_at": now},
            {"doctor_id": payload.doctor_id, "config_key": f"{p}.morning_enabled",
             "config_value": "true" if ds.morning.enabled else "false", "updated_at": now},
            {"doctor_id": payload.doctor_id, "config_key": f"{p}.morning_start",
             "config_value": ds.morning.start[:5], "updated_at": now},
            {"doctor_id": payload.doctor_id, "config_key": f"{p}.morning_end",
             "config_value": ds.morning.end[:5], "updated_at": now},
            {"doctor_id": payload.doctor_id, "config_key": f"{p}.evening_enabled",
             "config_value": "true" if ds.evening.enabled else "false", "updated_at": now},
            {"doctor_id": payload.doctor_id, "config_key": f"{p}.evening_start",
             "config_value": ds.evening.start[:5], "updated_at": now},
            {"doctor_id": payload.doctor_id, "config_key": f"{p}.evening_end",
             "config_value": ds.evening.end[:5], "updated_at": now},
        ]

    supabase.table("clinic_config").upsert(rows, on_conflict="doctor_id,config_key").execute()
    return _build_response(_all_cfg(payload.doctor_id))

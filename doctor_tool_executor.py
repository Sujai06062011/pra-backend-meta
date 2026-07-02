"""
doctor_tool_executor.py — Tool implementations for the Doctor Agent.

Reuses existing database.py functions wherever they exist.
All functions are synchronous wrappers (Supabase client is sync).
"""

from collections import defaultdict
from datetime import datetime, date, timedelta
from database import (
    supabase,
    get_queue_status,
    cancel_appointment,
    get_appointments_for_date,
    get_followups_needing_attention,
    get_unanswered_queries,
)
from analytics import (
    get_stats as _get_stats,
    get_patient_list as _get_patient_list,
    get_patient_detail as _get_patient_detail,
    get_pending_items as _get_pending_items,
    get_appointment_breakdown as _get_appointment_breakdown,
)


async def execute_doctor_tool(tool_name: str, tool_input: dict) -> dict:
    """Dispatch a doctor agent tool call to its implementation."""

    if tool_name == "get_stats":
        return _get_stats(
            doctor_id=tool_input["doctor_id"],
            metric=tool_input["metric"],
            period=tool_input["period"],
            n_days=tool_input.get("n_days"),
            start_date=tool_input.get("start_date"),
            end_date=tool_input.get("end_date"),
            compare_to_previous=tool_input.get("compare_to_previous", False),
        )

    elif tool_name == "get_patient_list":
        return _get_patient_list(
            doctor_id=tool_input["doctor_id"],
            filter_type=tool_input["filter_type"],
            period=tool_input["period"],
            n_days=tool_input.get("n_days"),
            min_visit_count=tool_input.get("min_visit_count", 3),
        )

    elif tool_name == "get_patient_detail":
        return _get_patient_detail(
            doctor_id=tool_input["doctor_id"],
            patient_ref=tool_input["patient_ref"],
            session=tool_input.get("session"),
        )

    elif tool_name == "get_pending_items":
        return _get_pending_items(tool_input["doctor_id"])

    elif tool_name == "get_appointment_breakdown":
        return _get_appointment_breakdown(
            doctor_id=tool_input["doctor_id"],
            group_by=tool_input["group_by"],
            period=tool_input["period"],
            n_days=tool_input.get("n_days"),
            start_date=tool_input.get("start_date"),
            end_date=tool_input.get("end_date"),
        )

    elif tool_name == "get_current_queue_status":
        q = get_queue_status(tool_input["doctor_id"])
        if not q:
            return {"current_token": 0, "total_tokens": 0, "waiting": 0, "active": False}
        waiting = max(0, (q.get("total_tokens") or 0) - (q.get("current_token") or 0))
        return {
            "current_token": q.get("current_token", 0),
            "total_tokens": q.get("total_tokens", 0),
            "waiting": waiting,
            "active": True,
        }

    elif tool_name == "get_pending_followup_replies":
        return {"followups": get_followups_needing_attention(tool_input["doctor_id"])}

    elif tool_name == "get_pending_queries":
        return {"queries": get_unanswered_queries(tool_input["doctor_id"])}

    elif tool_name == "remove_holiday":
        doctor_id = tool_input["doctor_id"]
        date_str = tool_input["date"]
        try:
            supabase.table("doctor_holidays").delete()\
                .eq("doctor_id", doctor_id)\
                .eq("holiday_date", date_str)\
                .execute()
            return {"success": True, "date": date_str,
                    "message": f"Holiday removed for {date_str}. Normal slots are now available."}
        except Exception as e:
            return {"success": False, "error": str(e)}

    elif tool_name == "preview_cancel_all_appointments":
        session = tool_input.get("session", "both")
        appts = get_appointments_for_date(
            tool_input["doctor_id"], tool_input["date"], session
        )
        return {
            "count": len(appts),
            "patients": [a["patient_name"] for a in appts],
            "session": session,
            "date": tool_input["date"],
        }

    elif tool_name == "cancel_all_appointments_confirmed":
        return await _do_cancel_all(tool_input)

    elif tool_name == "preview_mark_holiday":
        appts = get_appointments_for_date(tool_input["doctor_id"], tool_input["date"], "both")
        return {
            "count": len(appts),
            "patients": [a["patient_name"] for a in appts],
            "date": tool_input["date"],
        }

    elif tool_name == "mark_holiday_confirmed":
        return await _do_mark_holiday(tool_input)

    elif tool_name == "get_patient_age_breakdown":
        return _get_patient_age_breakdown(tool_input["doctor_id"])

    elif tool_name == "get_diagnosis_breakdown":
        return _get_diagnosis_breakdown(
            doctor_id=tool_input["doctor_id"],
            period=tool_input.get("period", "all_time"),
            n_days=tool_input.get("n_days"),
            start_date=tool_input.get("start_date"),
            end_date=tool_input.get("end_date"),
            limit=tool_input.get("limit", 10),
        )

    else:
        return {"error": f"Unknown tool: {tool_name}"}


async def _do_cancel_all(tool_input: dict) -> dict:
    """Execute bulk cancellation + WhatsApp notifications to affected patients."""
    from main import send_meta_text

    doctor_id = tool_input["doctor_id"]
    date_str = tool_input["date"]
    session = tool_input.get("session", "both")
    reason = tool_input.get("reason", "")

    # Fetch doctor name for notification
    try:
        doc_res = supabase.table("doctors").select("name").eq("id", doctor_id).single().execute()
        doctor_name = (doc_res.data or {}).get("name", "the doctor")
    except Exception:
        doctor_name = "the doctor"

    appts = get_appointments_for_date(doctor_id, date_str, session)
    cancelled_count = 0
    notified_count = 0

    for appt in appts:
        try:
            cancel_appointment(appt["id"])
            cancelled_count += 1
        except Exception as e:
            print(f"[DOCTOR_AGENT] Cancel failed for appt {appt['id']}: {e}")
            continue

        patient_mobile = appt.get("patient_mobile", "")
        if not patient_mobile:
            continue
        try:
            reason_str = f" due to {reason}" if reason else ""
            msg = (
                f"Dear {appt['patient_name']},\n\n"
                f"Your appointment with {doctor_name} on {date_str} has been cancelled{reason_str}.\n\n"
                f"We apologise for the inconvenience. Please reply MENU to reschedule at your convenience. 🙏"
            )
            await send_meta_text(patient_mobile, msg)
            notified_count += 1
        except Exception as e:
            print(f"[DOCTOR_AGENT] Notify failed for {patient_mobile}: {e}")

    return {
        "cancelled": cancelled_count,
        "notified": notified_count,
        "total": len(appts),
        "date": date_str,
        "session": session,
    }


async def _do_mark_holiday(tool_input: dict) -> dict:
    """Insert holiday record then cancel+notify all appointments for that date."""
    doctor_id = tool_input["doctor_id"]
    date_str = tool_input["date"]
    reason = tool_input.get("reason", "Doctor unavailable")

    # Insert holiday row (ON CONFLICT DO NOTHING equivalent)
    try:
        existing = supabase.table("doctor_holidays").select("id")\
            .eq("doctor_id", doctor_id).eq("holiday_date", date_str).execute()
        if not existing.data:
            supabase.table("doctor_holidays").insert({
                "doctor_id": doctor_id,
                "holiday_date": date_str,
                "reason": reason,
                "is_full_day": True,
            }).execute()
    except Exception as e:
        print(f"[DOCTOR_AGENT] Holiday insert failed: {e}")
        return {"error": f"Could not mark holiday: {e}"}

    # Cancel all appointments + notify patients
    cancel_result = await _do_cancel_all({
        **tool_input,
        "session": "both",
        "reason": reason,
    })
    return {**cancel_result, "holiday_marked": True, "reason": reason}


def _get_patient_age_breakdown(doctor_id: str) -> dict:
    """Return count of unique patients seen by this doctor, grouped into age buckets."""
    try:
        res = supabase.table("appointments")\
            .select("patient_id, patients(age, date_of_birth)")\
            .eq("doctor_id", doctor_id)\
            .not_.is_("patient_id", "null")\
            .execute()
    except Exception as e:
        return {"error": str(e)}

    seen = {}
    for row in (res.data or []):
        pid = row.get("patient_id")
        if not pid or pid in seen:
            continue
        p = row.get("patients") or {}
        age = p.get("age")
        if age is None and p.get("date_of_birth"):
            try:
                dob = datetime.strptime(p["date_of_birth"][:10], "%Y-%m-%d").date()
                today = date.today()
                age = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
            except Exception:
                pass
        seen[pid] = age

    buckets = {"0-5": 0, "6-12": 0, "13-17": 0, "18-30": 0, "31-50": 0, "51-70": 0, "70+": 0, "unknown": 0}
    for age in seen.values():
        if age is None:
            buckets["unknown"] += 1
        elif age <= 5:   buckets["0-5"] += 1
        elif age <= 12:  buckets["6-12"] += 1
        elif age <= 17:  buckets["13-17"] += 1
        elif age <= 30:  buckets["18-30"] += 1
        elif age <= 50:  buckets["31-50"] += 1
        elif age <= 70:  buckets["51-70"] += 1
        else:            buckets["70+"] += 1

    total = sum(v for k, v in buckets.items() if k != "unknown")
    breakdown = [
        {"age_group": k, "count": v, "pct": round(v * 100 / total, 1) if total else 0}
        for k, v in buckets.items() if v > 0
    ]
    breakdown.sort(key=lambda x: -x["count"])
    return {"total_patients": len(seen), "breakdown": breakdown}


def _get_diagnosis_breakdown(doctor_id: str, period: str = "all_time",
                              n_days: int = None, start_date: str = None,
                              end_date: str = None, limit: int = 10) -> dict:
    """Return top diagnoses by visit count for this doctor in the given period."""
    import pytz
    IST = pytz.timezone("Asia/Kolkata")
    today_ist = datetime.now(IST).date()

    # Compute date filter
    date_from = None
    if period == "this_month":
        date_from = today_ist.replace(day=1).isoformat()
    elif period == "last_month":
        first_this = today_ist.replace(day=1)
        last_month_end = first_this - timedelta(days=1)
        date_from = last_month_end.replace(day=1).isoformat()
        end_date = last_month_end.isoformat()
    elif period == "this_week":
        date_from = (today_ist - timedelta(days=today_ist.weekday())).isoformat()
    elif period == "last_n_days" and n_days:
        date_from = (today_ist - timedelta(days=n_days)).isoformat()
    elif period == "this_year":
        date_from = today_ist.replace(month=1, day=1).isoformat()
    elif period == "custom":
        date_from = start_date

    try:
        q = supabase.table("visits").select("diagnosis").eq("doctor_id", doctor_id)
        if date_from:
            q = q.gte("visit_date", date_from)
        if end_date:
            q = q.lte("visit_date", end_date)
        res = q.execute()
    except Exception as e:
        return {"error": str(e)}

    diag_map: dict = defaultdict(int)
    total_with_diagnosis = 0
    for row in (res.data or []):
        d = (row.get("diagnosis") or "").strip()
        if d:
            diag_map[d] += 1
            total_with_diagnosis += 1

    top = sorted([{"diagnosis": k, "count": v} for k, v in diag_map.items()],
                 key=lambda x: -x["count"])[:limit]
    return {
        "total_visits_with_diagnosis": total_with_diagnosis,
        "period": period,
        "top_diagnoses": top,
    }

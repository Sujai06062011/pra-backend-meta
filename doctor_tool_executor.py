"""
doctor_tool_executor.py — Tool implementations for the Doctor Agent.

Reuses existing database.py functions wherever they exist.
All functions are synchronous wrappers (Supabase client is sync).
"""

from datetime import datetime, date
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

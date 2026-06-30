"""
analytics.py — Read-only analytics backend for the Doctor Agent.

All functions filter by doctor_id internally. Claude only selects
from fixed enum vocabularies — no raw query construction from user input.
"""

from datetime import date, timedelta, datetime
from database import (
    supabase,
    get_queue_status,
    get_followups_needing_attention,
    get_unanswered_queries,
)


# ── Period resolution ──────────────────────────────────────────────────────────

def resolve_period_to_dates(period: str, n_days: int = None,
                             start_date: str = None, end_date: str = None):
    """Convert period enum to (start_date, end_date) strings (YYYY-MM-DD, inclusive)."""
    today = date.today()

    if period == "today":
        return today.isoformat(), today.isoformat()
    if period == "yesterday":
        d = today - timedelta(days=1)
        return d.isoformat(), d.isoformat()
    if period == "this_week":
        start = today - timedelta(days=today.weekday())  # Monday
        return start.isoformat(), today.isoformat()
    if period == "last_week":
        this_mon = today - timedelta(days=today.weekday())
        last_mon = this_mon - timedelta(days=7)
        last_sun = this_mon - timedelta(days=1)
        return last_mon.isoformat(), last_sun.isoformat()
    if period == "this_month":
        start = today.replace(day=1)
        return start.isoformat(), today.isoformat()
    if period == "last_month":
        first_this = today.replace(day=1)
        last_prev = first_this - timedelta(days=1)
        first_prev = last_prev.replace(day=1)
        return first_prev.isoformat(), last_prev.isoformat()
    if period == "this_quarter":
        q_start_month = ((today.month - 1) // 3) * 3 + 1
        start = today.replace(month=q_start_month, day=1)
        return start.isoformat(), today.isoformat()
    if period == "last_quarter":
        q_start_month = ((today.month - 1) // 3) * 3 + 1
        this_q_start = today.replace(month=q_start_month, day=1)
        last_q_end = this_q_start - timedelta(days=1)
        last_q_start_month = ((last_q_end.month - 1) // 3) * 3 + 1
        last_q_start = last_q_end.replace(month=last_q_start_month, day=1)
        return last_q_start.isoformat(), last_q_end.isoformat()
    if period == "this_year":
        return today.replace(month=1, day=1).isoformat(), today.isoformat()
    if period == "last_n_days":
        days = n_days or 7
        start = today - timedelta(days=days)
        return start.isoformat(), today.isoformat()
    if period == "custom":
        return start_date, end_date
    if period == "all_time":
        return "2020-01-01", today.isoformat()
    # fallback
    return today.isoformat(), today.isoformat()


def get_previous_period(start: str, end: str):
    """Return the preceding period of the same number of days."""
    s = date.fromisoformat(start)
    e = date.fromisoformat(end)
    length = (e - s).days + 1
    prev_end = s - timedelta(days=1)
    prev_start = prev_end - timedelta(days=length - 1)
    return prev_start.isoformat(), prev_end.isoformat()


# ── Metric calculation ─────────────────────────────────────────────────────────

def calculate_metric(doctor_id: str, metric: str, start: str, end: str) -> dict:
    """
    Dispatch to the right query. Returns {"value": X} (and optionally "note").
    All queries are filtered by doctor_id and the date range.
    """
    today = date.today().isoformat()

    if metric == "appointment_count":
        res = supabase.table("appointments").select("id", count="exact")\
            .eq("doctor_id", doctor_id)\
            .gte("appointment_date", start).lte("appointment_date", end)\
            .neq("status", "Cancelled").execute()
        return {"value": res.count or 0}

    if metric == "patient_count":
        res = supabase.table("appointments").select("patient_id")\
            .eq("doctor_id", doctor_id)\
            .gte("appointment_date", start).lte("appointment_date", end)\
            .neq("status", "Cancelled").execute()
        return {"value": len(set(r["patient_id"] for r in (res.data or [])))}

    if metric in ("new_patient_count", "returning_patient_count"):
        # All patients with an appointment in range
        appt_res = supabase.table("appointments").select("patient_id")\
            .eq("doctor_id", doctor_id)\
            .gte("appointment_date", start).lte("appointment_date", end)\
            .neq("status", "Cancelled").execute()
        pids = list(set(r["patient_id"] for r in (appt_res.data or [])))
        if not pids:
            return {"value": 0}
        # Find first-ever appointment date for each patient with this doctor
        first_res = supabase.table("appointments").select("patient_id, appointment_date")\
            .eq("doctor_id", doctor_id)\
            .in_("patient_id", pids)\
            .neq("status", "Cancelled")\
            .order("appointment_date").execute()
        first_appt = {}
        for r in (first_res.data or []):
            pid = r["patient_id"]
            if pid not in first_appt:
                first_appt[pid] = r["appointment_date"]
        new_count = sum(1 for pid, d in first_appt.items() if start <= d <= end)
        if metric == "new_patient_count":
            return {"value": new_count}
        return {"value": len(pids) - new_count}

    if metric == "cancelled_appointment_count":
        res = supabase.table("appointments").select("id", count="exact")\
            .eq("doctor_id", doctor_id)\
            .gte("appointment_date", start).lte("appointment_date", end)\
            .eq("status", "Cancelled").execute()
        return {"value": res.count or 0}

    if metric in ("no_show_count", "no_show_rate"):
        # No-show = Confirmed but patient never checked in (no visit record today)
        # Approximate: Confirmed appointments in past dates with no linked completed visit
        res = supabase.table("appointments").select("id, patient_id")\
            .eq("doctor_id", doctor_id)\
            .gte("appointment_date", start).lte("appointment_date", end)\
            .lt("appointment_date", today)\
            .eq("status", "Confirmed").execute()
        appt_ids = [r["id"] for r in (res.data or [])]
        if not appt_ids:
            return {"value": 0}
        visit_res = supabase.table("visits").select("appointment_id")\
            .in_("appointment_id", appt_ids).execute()
        visited_ids = {r["appointment_id"] for r in (visit_res.data or [])}
        no_show_count = sum(1 for aid in appt_ids if aid not in visited_ids)
        if metric == "no_show_count":
            return {"value": no_show_count}
        total = len(appt_ids)
        rate = round(no_show_count / total * 100, 1) if total else 0
        return {"value": rate, "unit": "%"}

    if metric == "completed_visit_count":
        res = supabase.table("visits").select("id", count="exact")\
            .eq("doctor_id", doctor_id)\
            .gte("created_at", start)\
            .lte("created_at", f"{end}T23:59:59")\
            .execute()
        return {"value": res.count or 0}

    if metric in ("revenue", "avg_revenue_per_patient"):
        # Only online consultation fee tracked; in-clinic fee not stored per-appointment
        doc_res = supabase.table("doctors").select("online_consultation_fee")\
            .eq("id", doctor_id).execute()
        online_fee = float((doc_res.data or [{}])[0].get("online_consultation_fee") or 0)
        online_res = supabase.table("appointments").select("id", count="exact")\
            .eq("doctor_id", doctor_id)\
            .eq("consultation_type", "online")\
            .gte("appointment_date", start).lte("appointment_date", end)\
            .neq("status", "Cancelled").execute()
        online_count = online_res.count or 0
        revenue = online_count * online_fee
        note = "Only online consultation fees tracked; in-clinic fees not yet recorded in system"
        if metric == "revenue":
            return {"value": revenue, "note": note}
        # avg per patient
        pat_res = supabase.table("appointments").select("patient_id")\
            .eq("doctor_id", doctor_id).eq("consultation_type", "online")\
            .gte("appointment_date", start).lte("appointment_date", end)\
            .neq("status", "Cancelled").execute()
        pat_count = len(set(r["patient_id"] for r in (pat_res.data or [])))
        avg = round(revenue / pat_count, 2) if pat_count else 0
        return {"value": avg, "note": note}

    if metric == "avg_wait_time_minutes":
        from database import get_slot_config
        cfg = get_slot_config()
        return {"value": cfg.get("duration", 15), "note": "Approximated from slot duration config; per-patient timestamps not tracked"}

    if metric == "followup_response_rate":
        sent_res = supabase.table("followups").select("id, reply")\
            .eq("doctor_id", doctor_id)\
            .gte("sent_date", start).lte("sent_date", end).execute()
        sent = sent_res.data or []
        total = len(sent)
        replied = sum(1 for f in sent if f.get("reply") and f["reply"] != "0")
        rate = round(replied / total * 100, 1) if total else 0
        return {"value": rate, "unit": "%", "replied": replied, "total_sent": total}

    if metric in ("online_consultation_count", "in_clinic_consultation_count"):
        ctype = "online" if metric == "online_consultation_count" else None
        q = supabase.table("appointments").select("id", count="exact")\
            .eq("doctor_id", doctor_id)\
            .gte("appointment_date", start).lte("appointment_date", end)\
            .neq("status", "Cancelled")
        if ctype:
            q = q.eq("consultation_type", ctype)
        else:
            q = q.neq("consultation_type", "online")
        res = q.execute()
        return {"value": res.count or 0}

    return {"value": None, "error": f"Unknown metric: {metric}"}


# ── get_appointment_breakdown ─────────────────────────────────────────────────

def get_appointment_breakdown(doctor_id: str, group_by: str, period: str,
                               n_days: int = None, start_date: str = None,
                               end_date: str = None) -> dict:
    """
    Return appointment counts grouped by day_of_week, date, or session.
    group_by: "day_of_week" | "date" | "session"
    """
    start, end = resolve_period_to_dates(period, n_days, start_date, end_date)

    res = supabase.table("appointments").select("appointment_date, appointment_time")\
        .eq("doctor_id", doctor_id)\
        .gte("appointment_date", start).lte("appointment_date", end)\
        .neq("status", "Cancelled").execute()
    rows = res.data or []

    if group_by == "day_of_week":
        from collections import defaultdict
        counts: dict = defaultdict(int)
        for r in rows:
            d = date.fromisoformat(r["appointment_date"])
            day_name = d.strftime("%A")  # Monday, Tuesday, ...
            counts[day_name] += 1
        day_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        breakdown = [{"day": day, "count": counts.get(day, 0)} for day in day_order if day in counts or counts]
        busiest = max(breakdown, key=lambda x: x["count"]) if breakdown else None
        return {
            "group_by": "day_of_week",
            "period": period,
            "date_range": f"{start} to {end}",
            "breakdown": breakdown,
            "busiest": busiest,
        }

    if group_by == "date":
        from collections import defaultdict
        counts: dict = defaultdict(int)
        for r in rows:
            counts[r["appointment_date"]] += 1
        breakdown = sorted([{"date": d, "count": c} for d, c in counts.items()],
                           key=lambda x: x["date"])
        busiest = max(breakdown, key=lambda x: x["count"]) if breakdown else None
        return {
            "group_by": "date",
            "period": period,
            "date_range": f"{start} to {end}",
            "breakdown": breakdown,
            "busiest": busiest,
        }

    if group_by == "session":
        morning = sum(1 for r in rows if (r.get("appointment_time") or "00:00") < "13:00")
        evening = len(rows) - morning
        return {
            "group_by": "session",
            "period": period,
            "date_range": f"{start} to {end}",
            "breakdown": [{"session": "morning", "count": morning},
                          {"session": "evening", "count": evening}],
        }

    return {"error": f"Unknown group_by: {group_by}. Use day_of_week, date, or session."}


# ── get_stats ──────────────────────────────────────────────────────────────────

def get_stats(doctor_id: str, metric: str, period: str,
              n_days: int = None, start_date: str = None,
              end_date: str = None, compare_to_previous: bool = False) -> dict:
    start, end = resolve_period_to_dates(period, n_days, start_date, end_date)
    current = calculate_metric(doctor_id, metric, start, end)

    result = {
        "metric": metric,
        "period": period,
        "date_range": f"{start} to {end}",
        **current,
    }

    if compare_to_previous:
        prev_start, prev_end = get_previous_period(start, end)
        prev = calculate_metric(doctor_id, metric, prev_start, prev_end)
        prev_val = prev.get("value") or 0
        curr_val = current.get("value") or 0
        result["previous_value"] = prev_val
        result["previous_date_range"] = f"{prev_start} to {prev_end}"
        result["change_pct"] = round(
            ((curr_val - prev_val) / prev_val * 100) if prev_val else 0, 1
        )

    return result


# ── get_patient_list ───────────────────────────────────────────────────────────

def _query_frequent_visitors(doctor_id: str, start: str, end: str, min_count: int) -> list:
    res = supabase.table("appointments").select("patient_id")\
        .eq("doctor_id", doctor_id)\
        .gte("appointment_date", start).lte("appointment_date", end)\
        .neq("status", "Cancelled").execute()
    from collections import Counter
    counts = Counter(r["patient_id"] for r in (res.data or []))
    frequent_ids = [pid for pid, c in counts.items() if c >= min_count]
    if not frequent_ids:
        return []
    pat_res = supabase.table("patients").select("id, name, mobile")\
        .in_("id", frequent_ids).execute()
    return [
        {"name": p["name"], "mobile": p["mobile"],
         "visit_count": counts[p["id"]]}
        for p in (pat_res.data or [])
    ]


def _query_no_show_patients(doctor_id: str, start: str, end: str) -> list:
    today = date.today().isoformat()
    res = supabase.table("appointments").select("id, patient_id, appointment_date, patients(name, mobile)")\
        .eq("doctor_id", doctor_id)\
        .gte("appointment_date", start).lte("appointment_date", min(end, today))\
        .lt("appointment_date", today)\
        .eq("status", "Confirmed").execute()
    appts = res.data or []
    if not appts:
        return []
    appt_ids = [a["id"] for a in appts]
    visit_res = supabase.table("visits").select("appointment_id")\
        .in_("appointment_id", appt_ids).execute()
    visited = {r["appointment_id"] for r in (visit_res.data or [])}
    out = []
    for a in appts:
        if a["id"] not in visited:
            pat = a.get("patients") or {}
            out.append({"name": pat.get("name", ""), "mobile": pat.get("mobile", ""),
                        "missed_date": a.get("appointment_date", "")})
    return out


def _query_new_patients(doctor_id: str, start: str, end: str) -> list:
    res = supabase.table("appointments").select("patient_id, appointment_date, patients(name, mobile)")\
        .eq("doctor_id", doctor_id)\
        .gte("appointment_date", start).lte("appointment_date", end)\
        .neq("status", "Cancelled").order("appointment_date").execute()
    first_seen: dict = {}
    for r in (res.data or []):
        pid = r["patient_id"]
        if pid not in first_seen:
            first_seen[pid] = r
    # Only patients whose FIRST ever appointment with this doctor is in this range
    all_first = supabase.table("appointments").select("patient_id, appointment_date")\
        .eq("doctor_id", doctor_id).neq("status", "Cancelled")\
        .in_("patient_id", list(first_seen.keys())).order("appointment_date").execute()
    truly_first: dict = {}
    for r in (all_first.data or []):
        pid = r["patient_id"]
        if pid not in truly_first:
            truly_first[pid] = r["appointment_date"]
    out = []
    for pid, row in first_seen.items():
        if truly_first.get(pid, "") >= start:
            pat = row.get("patients") or {}
            out.append({"name": pat.get("name", ""), "mobile": pat.get("mobile", ""),
                        "first_visit": row.get("appointment_date", "")})
    return out


def _query_pending_lab_reports(doctor_id: str) -> list:
    try:
        res = supabase.table("lab_reports").select("id, patient_id, report_name, created_at, patients(name, mobile)")\
            .eq("doctor_id", doctor_id).eq("status", "pending")\
            .order("created_at", desc=True).limit(30).execute()
        return [
            {"name": (r.get("patients") or {}).get("name", ""),
             "mobile": (r.get("patients") or {}).get("mobile", ""),
             "report": r.get("report_name", ""),
             "since": str(r.get("created_at", ""))[:10]}
            for r in (res.data or [])
        ]
    except Exception:
        return []


def _query_online_patients(doctor_id: str, start: str, end: str) -> list:
    res = supabase.table("appointments").select("patient_id, appointment_date, patients(name, mobile)")\
        .eq("doctor_id", doctor_id).eq("consultation_type", "online")\
        .gte("appointment_date", start).lte("appointment_date", end)\
        .neq("status", "Cancelled").execute()
    seen = {}
    for r in (res.data or []):
        pid = r["patient_id"]
        if pid not in seen:
            pat = r.get("patients") or {}
            seen[pid] = {"name": pat.get("name", ""), "mobile": pat.get("mobile", ""),
                         "date": r.get("appointment_date", "")}
    return list(seen.values())


def _query_no_reply_followups(doctor_id: str, start: str, end: str) -> list:
    try:
        res = supabase.table("followups").select("id, patient_id, sent_date, patients(name, mobile)")\
            .eq("doctor_id", doctor_id)\
            .gte("sent_date", start).lte("sent_date", end)\
            .is_("reply", "null").execute()
        return [
            {"name": (r.get("patients") or {}).get("name", ""),
             "mobile": (r.get("patients") or {}).get("mobile", ""),
             "sent_date": r.get("sent_date", "")}
            for r in (res.data or [])
        ]
    except Exception:
        return []


def get_patient_list(doctor_id: str, filter_type: str, period: str,
                     n_days: int = None, min_visit_count: int = 3) -> dict:
    start, end = resolve_period_to_dates(period, n_days)

    if filter_type == "frequent_visitors":
        patients = _query_frequent_visitors(doctor_id, start, end, min_visit_count)
    elif filter_type == "no_shows":
        patients = _query_no_show_patients(doctor_id, start, end)
    elif filter_type == "new_patients":
        patients = _query_new_patients(doctor_id, start, end)
    elif filter_type == "needs_followup_attention":
        raw = get_followups_needing_attention(doctor_id)
        patients = [{"name": r["patient_name"], "mobile": r["patient_mobile"],
                     "reply_date": r["reply_date"], "reply_code": r["reply_code"]}
                    for r in raw]
    elif filter_type == "pending_lab_reports":
        patients = _query_pending_lab_reports(doctor_id)
    elif filter_type == "pending_queries":
        raw = get_unanswered_queries(doctor_id)
        patients = [{"name": r["patient_name"], "mobile": r["patient_mobile"],
                     "question": r["question"]}
                    for r in raw]
    elif filter_type == "online_consultation_patients":
        patients = _query_online_patients(doctor_id, start, end)
    elif filter_type == "no_reply_to_followup":
        patients = _query_no_reply_followups(doctor_id, start, end)
    else:
        return {"error": f"Unknown filter_type: {filter_type}"}

    return {
        "filter_type": filter_type,
        "period": period,
        "date_range": f"{start} to {end}",
        "count": len(patients),
        "patients": patients,
    }


# ── get_patient_detail ─────────────────────────────────────────────────────────

def _current_session_now() -> str:
    import pytz
    IST = pytz.timezone("Asia/Kolkata")
    hour = datetime.now(IST).hour
    return "evening" if hour >= 13 else "morning"


def _search_patient_by_name(doctor_id: str, name: str) -> list:
    """Search patients scoped to this doctor's history only."""
    res = supabase.table("appointments").select("patient_id")\
        .eq("doctor_id", doctor_id)\
        .neq("status", "Cancelled").execute()
    pids = list(set(r["patient_id"] for r in (res.data or [])))
    if not pids:
        return []
    pat_res = supabase.table("patients").select("id, name, mobile, age, gender")\
        .in_("id", pids)\
        .ilike("name", f"%{name}%").execute()
    return pat_res.data or []


def _get_last_visit(doctor_id: str, patient_id: str) -> dict | None:
    res = supabase.table("visits").select(
        "id, diagnosis, created_at, follow_up_date"
    ).eq("doctor_id", doctor_id).eq("patient_id", patient_id)\
     .order("created_at", desc=True).limit(1).execute()
    if not res.data:
        return None
    v = res.data[0]

    # Fetch prescription medicines via separate query (two-hop join not supported in supabase-py)
    pres_list = []
    try:
        pres_res = supabase.table("prescriptions").select(
            "prescription_medicines(medicine_name, dosage)"
        ).eq("visit_id", v["id"]).limit(1).execute()
        for pres in (pres_res.data or []):
            for med in (pres.get("prescription_medicines") or []):
                name = med.get("medicine_name", "").strip()
                dose = med.get("dosage", "").strip()
                if name:
                    pres_list.append(f"{name} {dose}".strip())
    except Exception:
        pass

    return {
        "date": str(v.get("created_at", ""))[:10],
        "diagnosis": v.get("diagnosis", ""),
        "follow_up_date": v.get("follow_up_date", ""),
        "prescription_summary": ", ".join(pres_list) if pres_list else None,
    }


def _get_visit_count_this_year(doctor_id: str, patient_id: str) -> int:
    year_start = date.today().replace(month=1, day=1).isoformat()
    res = supabase.table("visits").select("id", count="exact")\
        .eq("doctor_id", doctor_id).eq("patient_id", patient_id)\
        .gte("created_at", year_start).execute()
    return res.count or 0


def _get_latest_followup_status(patient_id: str) -> str | None:
    try:
        res = supabase.table("followups").select("reply, sent_date")\
            .eq("patient_id", patient_id)\
            .order("sent_date", desc=True).limit(1).execute()
        if not res.data:
            return None
        reply = res.data[0].get("reply")
        labels = {"1": "Feeling better", "2": "Still recovering",
                  "3": "Needs appointment", "0": "No reply"}
        return labels.get(str(reply), "No reply")
    except Exception:
        return None


def _get_patient_pending_query(doctor_id: str, patient_id: str) -> str | None:
    try:
        res = supabase.table("queries").select("question")\
            .eq("doctor_id", doctor_id).eq("patient_id", patient_id)\
            .eq("status", "Pending")\
            .order("created_at", desc=True).limit(1).execute()
        return res.data[0]["question"] if res.data else None
    except Exception:
        return None


def get_patient_detail(doctor_id: str, patient_ref: str, session: str = None) -> dict:
    today = date.today().isoformat()
    token_label = None

    if patient_ref == "next_in_queue":
        session = session or _current_session_now()
        q = get_queue_status(doctor_id)
        current_token = (q or {}).get("current_token", 0)

        # Find next Confirmed appointment with token > current_token in the right session
        res = supabase.table("appointments").select(
            "id, patient_id, appointment_time, token_number"
        ).eq("doctor_id", doctor_id).eq("appointment_date", today)\
         .eq("status", "Confirmed").neq("consultation_type", "online")\
         .gt("token_number", current_token)\
         .order("token_number").limit(1).execute()

        if not res.data:
            return {"status": "queue_empty", "message": "No patients currently waiting"}

        appt = res.data[0]
        t = str(appt.get("appointment_time", ""))[:5]
        is_eve = t >= "13:00"
        if session == "morning" and is_eve:
            return {"status": "queue_empty", "message": "No morning patients waiting"}
        if session == "evening" and not is_eve:
            return {"status": "queue_empty", "message": "No evening patients waiting"}

        patient_id = appt["patient_id"]
        from database import get_display_token
        token_label = get_display_token(
            appt.get("token_number"), appt.get("appointment_time"),
            doctor_id=doctor_id, date_str=today
        )
    else:
        matches = _search_patient_by_name(doctor_id, patient_ref)
        if not matches:
            return {"status": "not_found", "message": f"No patient found matching '{patient_ref}'"}
        if len(matches) > 1:
            return {
                "status": "ambiguous",
                "message": f"Multiple patients match '{patient_ref}'",
                "matches": [{"name": m["name"], "mobile": m.get("mobile", "")} for m in matches],
            }
        patient_id = matches[0]["id"]

    pat_res = supabase.table("patients").select("name, age, gender, mobile")\
        .eq("id", patient_id).single().execute()
    patient = pat_res.data or {}

    last_visit = _get_last_visit(doctor_id, patient_id)
    visit_count = _get_visit_count_this_year(doctor_id, patient_id)
    followup_status = _get_latest_followup_status(patient_id)
    pending_query = _get_patient_pending_query(doctor_id, patient_id)

    return {
        "status": "found",
        "token": token_label,
        "patient_name": patient.get("name", ""),
        "age": patient.get("age"),
        "gender": patient.get("gender", ""),
        "last_visit_date": last_visit["date"] if last_visit else None,
        "last_diagnosis": last_visit["diagnosis"] if last_visit else None,
        "last_prescription_summary": last_visit["prescription_summary"] if last_visit else None,
        "followup_status": followup_status,
        "visit_count_this_year": visit_count,
        "is_regular": visit_count >= 3,
        "pending_query": pending_query,
    }


# ── get_pending_items ──────────────────────────────────────────────────────────

def get_pending_items(doctor_id: str) -> dict:
    today = date.today()
    week_start = (today - timedelta(days=7)).isoformat()

    lab_reports = _query_pending_lab_reports(doctor_id)
    queries = get_unanswered_queries(doctor_id)

    # followup concerns in last 7 days
    fu_res = supabase.table("followups").select("id")\
        .eq("doctor_id", doctor_id)\
        .in_("reply", ["2", "3"])\
        .gte("reply_date", week_start).execute()
    followup_concerns = len(fu_res.data or [])

    return {
        "pending_lab_reports": len(lab_reports),
        "pending_queries": len(queries),
        "followup_concerns": followup_concerns,
        "total_pending": len(lab_reports) + len(queries) + followup_concerns,
    }

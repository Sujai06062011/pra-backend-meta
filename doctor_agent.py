"""
doctor_agent.py — Claude Agent for doctor-originated WhatsApp messages.

Completely separate from the patient agent (agent.py). Only activated
when the incoming mobile matches a row in the doctors table.

Safety rules enforced here AND in the system prompt:
- Destructive tools (cancel_all, mark_holiday) require exact "YES CANCEL ALL"
- Preview tool must always be called before the confirmed/execute tool
- Never touches patient state machine or patient conversation state
"""

import anthropic
import os
import json
from datetime import datetime

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))
    return _client


DOCTOR_AGENT_TOOLS = [
    {
        "name": "get_stats",
        "description": (
            "Get a single numeric metric for this doctor over a time period. "
            "Replaces the old get_appointments_summary and get_weekly_stats tools. "
            "Available metrics: appointment_count, patient_count, new_patient_count, "
            "returning_patient_count, cancelled_appointment_count, no_show_count, no_show_rate, "
            "completed_visit_count, revenue, avg_revenue_per_patient, avg_wait_time_minutes, "
            "followup_response_rate, online_consultation_count, in_clinic_consultation_count. "
            "Available periods: today, yesterday, this_week, last_week, this_month, last_month, "
            "this_quarter, last_quarter, this_year, last_n_days, custom, all_time. "
            "Set compare_to_previous=true to also return the preceding period value and % change."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "doctor_id": {"type": "string"},
                "metric": {"type": "string"},
                "period": {"type": "string"},
                "n_days": {"type": "integer", "description": "Only for period=last_n_days"},
                "start_date": {"type": "string", "description": "YYYY-MM-DD, only for period=custom"},
                "end_date": {"type": "string", "description": "YYYY-MM-DD, only for period=custom"},
                "compare_to_previous": {"type": "boolean"},
            },
            "required": ["doctor_id", "metric", "period"],
        },
    },
    {
        "name": "get_patient_list",
        "description": (
            "Get a filtered list of patients for this doctor. "
            "filter_types: frequent_visitors, no_shows, new_patients, needs_followup_attention, "
            "pending_lab_reports, pending_queries, online_consultation_patients, no_reply_to_followup. "
            "Returns patient names, mobiles, and filter-specific metadata. Max 30 results. "
            "GOLDEN RULE: READ-ONLY. No confirmation required."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "doctor_id": {"type": "string"},
                "filter_type": {"type": "string"},
                "period": {"type": "string", "description": "Same period enum as get_stats"},
                "n_days": {"type": "integer"},
                "min_visit_count": {"type": "integer",
                                    "description": "Only for frequent_visitors, default 3"},
            },
            "required": ["doctor_id", "filter_type", "period"],
        },
    },
    {
        "name": "get_patient_detail",
        "description": (
            "Get a deep-dive on a single patient scoped to this doctor's history. "
            "Pass patient_ref='next_in_queue' to get the next waiting patient's context card. "
            "Or pass patient_ref='<name search>' (e.g. 'Priya') to find by name. "
            "Returns: last diagnosis, last prescription summary, followup status, "
            "visit count this year, pending query (if any), is_regular flag. "
            "If name is ambiguous, returns status='ambiguous' with match list. "
            "GOLDEN RULE: READ-ONLY. No confirmation required."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "doctor_id": {"type": "string"},
                "patient_ref": {"type": "string",
                                "description": "'next_in_queue' or a name to search"},
                "session": {"type": "string", "enum": ["morning", "evening"],
                            "description": "Relevant only for next_in_queue; auto-detected if omitted"},
            },
            "required": ["doctor_id", "patient_ref"],
        },
    },
    {
        "name": "get_pending_items",
        "description": (
            "Get a summary count of all items needing the doctor's attention: "
            "pending lab reports, unanswered patient queries, followup concerns (last 7 days). "
            "GOLDEN RULE: READ-ONLY. No confirmation required."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"doctor_id": {"type": "string"}},
            "required": ["doctor_id"],
        },
    },
    {
        "name": "get_current_queue_status",
        "description": "Get the live queue status — current token being served, total waiting.",
        "input_schema": {
            "type": "object",
            "properties": {"doctor_id": {"type": "string"}},
            "required": ["doctor_id"],
        },
    },
    {
        "name": "get_pending_followup_replies",
        "description": (
            "Get patients who replied 'still recovering' or 'need to see doctor again' "
            "in the last 7 days and need attention."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"doctor_id": {"type": "string"}},
            "required": ["doctor_id"],
        },
    },
    {
        "name": "get_pending_queries",
        "description": "Get unanswered patient queries (Ask Doctor a Question submissions).",
        "input_schema": {
            "type": "object",
            "properties": {"doctor_id": {"type": "string"}},
            "required": ["doctor_id"],
        },
    },
    {
        "name": "remove_holiday",
        "description": (
            "Remove a holiday for a date — deletes the doctor_holidays row so the date "
            "becomes bookable again with normal slots. No confirmation required; "
            "this is non-destructive (restores availability, doesn't delete patient data)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "doctor_id": {"type": "string"},
                "date": {"type": "string", "description": "YYYY-MM-DD"},
            },
            "required": ["doctor_id", "date"],
        },
    },
    {
        "name": "preview_cancel_all_appointments",
        "description": (
            "Preview impact of cancelling all appointments for a date WITHOUT executing. "
            "ALWAYS call this FIRST before cancel_all_appointments_confirmed. "
            "Shows the doctor exactly what will be affected."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "doctor_id": {"type": "string"},
                "date": {"type": "string", "description": "YYYY-MM-DD"},
                "session": {
                    "type": "string",
                    "enum": ["morning", "evening", "both"],
                    "description": "Which session to preview, default both",
                },
            },
            "required": ["doctor_id", "date"],
        },
    },
    {
        "name": "cancel_all_appointments_confirmed",
        "description": (
            "EXECUTES bulk cancellation and notifies all affected patients via WhatsApp. "
            "ONLY call this after the doctor's message contains the EXACT phrase 'YES CANCEL ALL'. "
            "A plain 'yes', 'ok', 'sure', or 'go ahead' is NOT sufficient."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "doctor_id": {"type": "string"},
                "date": {"type": "string"},
                "session": {"type": "string", "enum": ["morning", "evening", "both"]},
                "reason": {"type": "string", "description": "Reason for cancellation sent to patients"},
            },
            "required": ["doctor_id", "date", "session"],
        },
    },
    {
        "name": "preview_mark_holiday",
        "description": (
            "Preview impact of marking a date as holiday WITHOUT executing. "
            "Shows how many appointments would be affected. "
            "ALWAYS call this before mark_holiday_confirmed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "doctor_id": {"type": "string"},
                "date": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["doctor_id", "date"],
        },
    },
    {
        "name": "mark_holiday_confirmed",
        "description": (
            "EXECUTES marking a date as holiday: inserts into doctor_holidays, "
            "cancels any existing appointments, notifies affected patients. "
            "If preview_mark_holiday returned count=0 (no appointments to cancel), "
            "call this IMMEDIATELY without asking for confirmation — nothing destructive is happening. "
            "If count > 0, ONLY call after doctor replies with the EXACT phrase 'YES CANCEL ALL'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "doctor_id": {"type": "string"},
                "date": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["doctor_id", "date"],
        },
    },
]

DOCTOR_SYSTEM_PROMPT = """You are the WhatsApp assistant for {doctor_name} at {clinic_name}.
You are speaking directly to the DOCTOR, not a patient.

Today: {today_date}
Doctor ID: {doctor_id}

CRITICAL SAFETY RULES — NON-NEGOTIABLE:
1. For cancel_all_appointments_confirmed:
   STEP A: Call preview_cancel_all_appointments first. Show count, patient names, session/date.
   End your message with: "Reply YES CANCEL ALL to confirm, or say no to cancel this action."
   STEP B: ONLY call cancel_all_appointments_confirmed if doctor replies with the EXACT phrase "YES CANCEL ALL".
   A plain "yes", "ok", "sure" is NOT enough. Never skip step A.

2. For mark_holiday_confirmed:
   STEP A: Always call preview_mark_holiday first.
   STEP B (conditional):
   - If preview returns count=0 (no appointments): call mark_holiday_confirmed IMMEDIATELY in the same response.
     Tell the doctor: "✅ [date] marked as holiday. No appointments were affected."
   - If preview returns count>0: show the patient list, end with "Reply YES CANCEL ALL to confirm."
     ONLY call mark_holiday_confirmed after doctor replies with the EXACT phrase "YES CANCEL ALL".

3. Read-only queries (stats, queue, followups, queries) → answer directly, no confirmation needed.

4. Keep replies short and scannable. Doctors are busy. Use numbers, not paragraphs.
   Bad: "You have a total of fifteen appointments scheduled..."
   Good: "📋 Today: 8 morning | 7 evening | 15 total"

5. If the request is ambiguous (e.g. "cancel today" without specifying session), ask which session.
   Only default to "both" if doctor directly confirms with YES CANCEL ALL without specifying.

6. Never make up data. If a tool returns empty or errors, say so honestly.

7. All times are IST. Resolve "today", "tomorrow", "this evening" relative to {today_date}.

ANALYTICS RULES (tools: get_stats, get_patient_list, get_patient_detail, get_pending_items):

8. These are READ-ONLY tools. Never require confirmation for any of them.

9. For get_stats: call once per metric. If the doctor asks for multiple metrics (e.g. "how many patients and how many no-shows this week"), make parallel tool calls — one per metric.

10. For revenue metric: the result will include a "note" field flagging incomplete data (only online consultation fees tracked). Always relay this note to the doctor; do not present revenue as total clinic revenue.

11. For get_patient_detail with patient_ref="next_in_queue": if status="queue_empty", tell the doctor simply "No patients waiting right now." Do not fabricate patient data.

12. For get_patient_detail with a name search: if status="ambiguous", list the matches and ask the doctor to clarify. If status="not_found", say so honestly.

13. When presenting patient lists from get_patient_list, keep it scannable:
    - Show name + key detail (visit count, missed date, question excerpt, etc.)
    - Group by category if multiple filter types were queried
    - Cap display at 10 patients; say "and X more" if there are more"""


def _validate_history(history: list) -> list:
    """Remove tool_result blocks whose tool_use is no longer in history (orphans from trimming)."""
    tool_use_ids = set()
    for msg in history:
        if msg.get("role") == "assistant":
            for block in (msg.get("content") or []):
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tool_use_ids.add(block["id"])

    clean = []
    for msg in history:
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, list):
                filtered = [
                    b for b in content
                    if not (isinstance(b, dict) and b.get("type") == "tool_result"
                            and b.get("tool_use_id") not in tool_use_ids)
                ]
                if not filtered:
                    continue
                msg = {**msg, "content": filtered}
        clean.append(msg)
    return clean


def _serialize_content(content) -> list:
    """Convert Anthropic SDK content blocks to JSON-safe dicts for history persistence."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    out = []
    for block in content:
        if hasattr(block, "type"):
            if block.type == "text":
                out.append({"type": "text", "text": block.text})
            elif block.type == "tool_use":
                out.append({"type": "tool_use", "id": block.id,
                            "name": block.name, "input": block.input})
        elif isinstance(block, dict):
            out.append(block)
    return out


async def run_doctor_agent(
    doctor_id: str,
    message: str,
    doctor_context: dict,
    conversation_history: list,
    tool_executor,
) -> dict:
    """
    Run Claude agent for a doctor-originated message.
    Returns {"reply_text": str, "updated_history": list}.

    tool_executor: async callable(tool_name, tool_input) -> dict
    conversation_history: serialized prior messages (from conversation_state)
    """
    import pytz
    IST = pytz.timezone("Asia/Kolkata")
    today_date = datetime.now(IST).strftime("%Y-%m-%d (%A)")

    system = DOCTOR_SYSTEM_PROMPT.format(
        doctor_name=doctor_context.get("name", "Doctor"),
        clinic_name=doctor_context.get("clinic_name", "Clinic"),
        today_date=today_date,
        doctor_id=doctor_id,
    )

    messages = _validate_history(list(conversation_history)) + [{"role": "user", "content": message}]
    client = _get_client()

    for _ in range(10):
        response = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=system,
            tools=DOCTOR_AGENT_TOOLS,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            reply_text = ""
            for block in response.content:
                if hasattr(block, "type") and block.type == "text":
                    reply_text = block.text.strip()
                    break
            messages.append({"role": "assistant", "content": _serialize_content(response.content)})
            return {
                "reply_text": reply_text,
                "updated_history": messages,
            }

        if response.stop_reason != "tool_use":
            break

        tool_results = []
        for block in response.content:
            if hasattr(block, "type") and block.type == "tool_use":
                print(f"[DOCTOR_AGENT] Tool: {block.name}({json.dumps(block.input)[:200]})")
                try:
                    result = await tool_executor(block.name, block.input)
                except Exception as e:
                    result = {"error": str(e)}
                result_str = json.dumps(result, ensure_ascii=False, default=str)
                print(f"[DOCTOR_AGENT] Result: {result_str[:200]}")
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result_str,
                })

        messages.append({"role": "assistant", "content": _serialize_content(response.content)})
        messages.append({"role": "user", "content": tool_results})

    return {"reply_text": "", "updated_history": messages}

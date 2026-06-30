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
        "name": "get_appointments_summary",
        "description": (
            "Get appointment counts and stats for a doctor for today or a specific date. "
            "Returns counts by session (morning/evening), confirmed vs waiting, new vs returning patients."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "doctor_id": {"type": "string"},
                "date": {"type": "string", "description": "YYYY-MM-DD, defaults to today if omitted"},
            },
            "required": ["doctor_id"],
        },
    },
    {
        "name": "get_weekly_stats",
        "description": (
            "Get summary stats for the past N days: patients seen, average wait time, "
            "follow-up replies needing attention, pending queries."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "doctor_id": {"type": "string"},
                "days": {"type": "integer", "description": "Days to look back, default 7"},
            },
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

7. All times are IST. Resolve "today", "tomorrow", "this evening" relative to {today_date}."""


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

    messages = list(conversation_history) + [{"role": "user", "content": message}]
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

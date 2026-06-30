"""
tool_executor.py — Thin façade re-exporting agent tool implementations.

Keeps agent.py self-contained while providing a clean import point
if other modules need to call individual tools directly.
"""

from agent import (
    _tool_get_patient as get_patient,
    _tool_get_clinic_doctors as get_clinic_doctors,
    _tool_get_available_slots as get_available_slots,
    _tool_book_appointment as book_appointment,
    _tool_get_upcoming_appointments as get_upcoming_appointments,
    _tool_get_queue_status as get_queue_status,
    _tool_cancel_appointment as cancel_appointment,
    _tool_register_patient as register_patient,
    _tool_add_family_member as add_family_member,
    _dispatch_tool as dispatch_tool,
)

__all__ = [
    "get_patient",
    "get_clinic_doctors",
    "get_available_slots",
    "book_appointment",
    "get_upcoming_appointments",
    "get_queue_status",
    "cancel_appointment",
    "register_patient",
    "add_family_member",
    "dispatch_tool",
]

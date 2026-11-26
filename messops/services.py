"""Domain services for the Mess Committee SOP."""
from __future__ import annotations

import datetime as dt

from django.db import transaction
from django.utils import timezone

from .models import (
    CommitteeMember,
    Complaint,
    MenuChangeRequest,
    MenuPreference,
    MonthlyMenuCycle,
    ReviewMeeting,
    month_start,
)


class SopError(ValueError):
    """A SOP action that cannot be applied as requested."""


def next_month(value: dt.date | None = None) -> dt.date:
    base = month_start(value or timezone.localdate())
    year = base.year + (1 if base.month == 12 else 0)
    month = 1 if base.month == 12 else base.month + 1
    return dt.date(year, month, 1)


def committee_composition(members=None) -> dict:
    """Summarise the active committee and whether it matches the SOP composition."""
    if members is None:
        members = list(CommitteeMember.objects.filter(is_active=True).select_related("user"))
    else:
        members = list(members)
    reps = [member for member in members if member.is_representative]
    ug = [member for member in reps if member.is_ug]
    pg = [member for member in reps if member.is_pg]
    return {
        "members": members,
        "representatives": reps,
        "ug": ug,
        "pg": pg,
        "ug_count": len(ug),
        "pg_count": len(pg),
        "is_complete": len(ug) == 2 and len(pg) == 2,
        "chairperson": next(
            (member for member in members if member.is_chairperson), None
        ),
        "operator": next((member for member in members if member.is_operator), None),
    }


def current_cycle() -> MonthlyMenuCycle | None:
    return MonthlyMenuCycle.objects.order_by("-month").first()


def cycle_for_month(month: dt.date | None) -> MonthlyMenuCycle | None:
    if not month:
        return None
    return MonthlyMenuCycle.objects.filter(month=month_start(month)).first()


def open_cycle_for_preferences() -> MonthlyMenuCycle | None:
    return (
        MonthlyMenuCycle.objects.filter(status=MonthlyMenuCycle.Status.CONSULTATION)
        .order_by("-month")
        .first()
    )


def latest_published_cycle() -> MonthlyMenuCycle | None:
    return (
        MonthlyMenuCycle.objects.filter(status=MonthlyMenuCycle.Status.PUBLISHED)
        .order_by("-month")
        .first()
    )


@transaction.atomic
def update_cycle_text(
    cycle: MonthlyMenuCycle,
    *,
    available_items: str | None = None,
    operator_notes: str | None = None,
    student_preferences: str | None = None,
    final_menu: str | None = None,
    approved_by_name: str | None = None,
) -> MonthlyMenuCycle:
    if cycle.is_closed:
        raise SopError("This menu cycle is closed. Create a new cycle instead.")
    if available_items is not None:
        cycle.available_items = available_items.strip()
    if operator_notes is not None:
        cycle.operator_notes = operator_notes.strip()
    if student_preferences is not None:
        cycle.student_preferences = student_preferences.strip()
    if final_menu is not None:
        cycle.final_menu = final_menu.strip()
    if approved_by_name is not None:
        cycle.approved_by_name = approved_by_name.strip()
    cycle.save()
    return cycle


@transaction.atomic
def transition_cycle(
    cycle: MonthlyMenuCycle, action: str, *, actor=None
) -> MonthlyMenuCycle:
    """Apply a SOP workflow step with its preconditions."""
    status = cycle.status
    now = timezone.now()

    if action == "open":
        if status not in {
            MonthlyMenuCycle.Status.OPERATOR_LIST,
            MonthlyMenuCycle.Status.CONSULTATION,
        }:
            raise SopError("Only an unfinalized cycle can open student consultation.")
        if not cycle.available_items.strip():
            raise SopError(
                "Record the operator's exhaustive list of available items first."
            )
        cycle.status = MonthlyMenuCycle.Status.CONSULTATION
    elif action == "finalize":
        if status not in {
            MonthlyMenuCycle.Status.CONSULTATION,
            MonthlyMenuCycle.Status.FINALIZED,
        }:
            raise SopError("Open student consultation before finalizing the menu.")
        if not cycle.final_menu.strip():
            raise SopError("Record the mutually agreed final menu before finalizing.")
        cycle.status = MonthlyMenuCycle.Status.FINALIZED
        cycle.approved_by = actor
        cycle.approved_at = now
    elif action == "publish":
        if status not in {
            MonthlyMenuCycle.Status.FINALIZED,
            MonthlyMenuCycle.Status.PUBLISHED,
        }:
            raise SopError("Finalize the menu before publishing it to students.")
        cycle.status = MonthlyMenuCycle.Status.PUBLISHED
        cycle.published_at = now
    elif action == "close":
        if status in {MonthlyMenuCycle.Status.CLOSED}:
            raise SopError("This cycle is already closed.")
        cycle.status = MonthlyMenuCycle.Status.CLOSED
    else:
        raise SopError("Unknown menu-cycle action.")
    cycle.save()
    return cycle


def save_preference(
    *,
    cycle: MonthlyMenuCycle,
    student,
    preference: str,
    meal=None,
    notes: str = "",
) -> MenuPreference | None:
    if not cycle.accepts_preferences:
        raise SopError("Student consultation is not open for this menu cycle.")
    text = (preference or "").strip()
    if not text:
        raise SopError("Write the dishes you would like on the menu.")
    existing = MenuPreference.objects.filter(cycle=cycle, student=student).first()
    item = existing or MenuPreference(cycle=cycle, student=student)
    item.preference = text[:2000]
    item.meal = meal
    item.notes = (notes or "")[:240]
    item.save()
    return item


def submit_complaint(
    *,
    student,
    category: str,
    description: str,
    service_date: dt.date | None = None,
    meal=None,
) -> Complaint:
    text = (description or "").strip()
    if not text:
        raise SopError("Describe the issue so the committee can act on it.")
    if category not in Complaint.Category.values:
        raise SopError("Choose a valid complaint category.")
    return Complaint.objects.create(
        student=student,
        category=category,
        description=text[:4000],
        service_date=service_date,
        meal=meal,
    )


@transaction.atomic
def update_complaint(
    complaint: Complaint,
    *,
    status: str,
    resolution: str = "",
    committee_note: str = "",
    actor=None,
) -> Complaint:
    if status not in Complaint.Status.values:
        raise SopError("Choose a valid complaint status.")
    complaint.status = status
    complaint.resolution = (resolution or "").strip()[:2000]
    complaint.committee_note = (committee_note or "").strip()[:2000]
    complaint.handled_by = actor
    now = timezone.now()
    if status in {Complaint.Status.RESOLVED, Complaint.Status.CLOSED}:
        complaint.resolved_at = now
    if status in {Complaint.Status.ESCALATED}:
        complaint.escalated_at = now
    complaint.save()
    return complaint


def record_change_request(
    *,
    service_date: dt.date,
    summary: str,
    reason: str,
    meal=None,
    proposed_by=None,
) -> MenuChangeRequest:
    if not (summary or "").strip():
        raise SopError("Summarise the proposed change.")
    if not (reason or "").strip():
        raise SopError("Explain why the change is needed.")
    return MenuChangeRequest.objects.create(
        service_date=service_date,
        meal=meal,
        summary=summary.strip()[:240],
        reason=reason.strip()[:2000],
        proposed_by=proposed_by,
    )


@transaction.atomic
def update_change_request(
    change: MenuChangeRequest, *, status: str, actor=None
) -> MenuChangeRequest:
    if status not in MenuChangeRequest.Status.values:
        raise SopError("Choose a valid change status.")
    now = timezone.now()
    change.status = status
    if status in {
        MenuChangeRequest.Status.AGREED,
        MenuChangeRequest.Status.IMPLEMENTED,
    }:
        if not change.agreed_at:
            change.agreed_at = now
            change.agreed_by = actor
    if status in {MenuChangeRequest.Status.IMPLEMENTED}:
        change.communicated_at = now
    change.save()
    return change


def record_review_meeting(
    *,
    meeting_date: dt.date,
    attendees: str = "",
    agenda: str = "",
    minutes: str = "",
    actions: str = "",
    escalated: bool = False,
    actor=None,
) -> ReviewMeeting:
    return ReviewMeeting.objects.create(
        month=month_start(meeting_date),
        meeting_date=meeting_date,
        attendees=(attendees or "").strip()[:4000],
        agenda=(agenda or ReviewMeeting.DEFAULT_AGENDA).strip()[:4000],
        minutes=(minutes or "").strip()[:8000],
        actions=(actions or "").strip()[:4000],
        escalated=bool(escalated),
        created_by=actor,
    )

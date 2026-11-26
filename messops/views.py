"""Web pages for the Mess Committee SOP."""
from __future__ import annotations

import datetime as dt

from django.conf import settings
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.http import require_POST

from accounts.permissions import perm_required
from menu.models import DailyMenu, MealSlot

from .forms import (
    CommitteeMemberForm,
    ComplaintForm,
    MenuChangeForm,
    MenuPreferenceForm,
    ReviewMeetingForm,
)
from .models import (
    CommitteeMember,
    Complaint,
    MenuChangeRequest,
    MenuPreference,
    MonthlyMenuCycle,
    ReviewMeeting,
    month_start,
)
from .services import (
    SopError,
    committee_composition,
    current_cycle,
    cycle_for_month,
    latest_published_cycle,
    next_month,
    open_cycle_for_preferences,
    record_change_request,
    record_review_meeting,
    save_preference,
    submit_complaint,
    transition_cycle,
    update_change_request,
    update_complaint,
    update_cycle_text,
)


def _month(raw: str | None) -> dt.date | None:
    if not raw:
        return None
    try:
        if len(raw) == 7 and raw[4] == "-":
            year, month = raw.split("-")
            return dt.date(int(year), int(month), 1)
        parsed = parse_date(raw)
        return month_start(parsed) if parsed else None
    except (TypeError, ValueError):
        return None


def _posted_or_none(request, key: str):
    return request.POST[key] if key in request.POST else None


@perm_required("sop.view")
def sop(request):
    """The standalone Standard Operating Procedure page."""
    members = list(
        CommitteeMember.objects.filter(is_active=True)
        .select_related("user")
        .order_by("role", "program", "full_name")
    )
    composition = committee_composition(members)
    return render(
        request,
        "sop.html",
        {
            "title": "Mess Committee SOP",
            "members": members,
            "composition": composition,
            "cycle": current_cycle(),
            "chairperson_name": settings.MESSLAB.get(
                "CHAIRPERSON_NAME", "Chairperson – Hostel Residence"
            ),
            "operator_name": settings.MESSLAB.get("MESS_OPERATOR_NAME", "Mess operator"),
            "weekend_form_url": settings.MESSLAB.get("WEEKEND_WASTE_FORM_URL", ""),
            "active": "sop",
        },
    )


@perm_required("complaint.submit")
def student_complaints(request):
    """Submit a grievance and follow its resolution."""
    form = ComplaintForm()
    if request.method == "POST":
        form = ComplaintForm(request.POST)
        if form.is_valid():
            try:
                complaint = submit_complaint(
                    student=request.user,
                    category=form.cleaned_data["category"],
                    description=form.cleaned_data["description"],
                    service_date=form.cleaned_data.get("service_date"),
                    meal=form.cleaned_data.get("meal"),
                )
            except SopError as exc:
                form.add_error(None, str(exc))
            else:
                messages.success(
                    request,
                    f"Complaint {complaint.reference} recorded. The Mess Committee will review it.",
                )
                return redirect("messops:student-complaints")

    complaints = (
        Complaint.objects.filter(student=request.user)
        .select_related("meal", "handled_by")
        .order_by("-created_at")
    )
    composition = committee_composition()
    return render(
        request,
        "messops/student_complaints.html",
        {
            "form": form,
            "complaints": complaints,
            "composition": composition,
            "active": "complaints",
        },
    )


@perm_required("preference.respond")
def student_preferences(request):
    """Submit menu preferences for the cycle that is open for consultation."""
    cycle = open_cycle_for_preferences()
    existing = (
        MenuPreference.objects.filter(cycle=cycle, student=request.user).first()
        if cycle
        else None
    )
    form = MenuPreferenceForm(instance=existing)
    if request.method == "POST":
        if cycle is None:
            messages.error(
                request,
                "Student consultation is not open right now. Watch this page for the next cycle.",
            )
            return redirect("messops:student-preferences")
        form = MenuPreferenceForm(request.POST, instance=existing)
        if form.is_valid():
            try:
                save_preference(
                    cycle=cycle,
                    student=request.user,
                    preference=form.cleaned_data["preference"],
                    meal=form.cleaned_data.get("meal"),
                    notes=form.cleaned_data.get("notes", ""),
                )
            except SopError as exc:
                form.add_error(None, str(exc))
            else:
                messages.success(
                    request,
                    "Thank you. Your preference is with the Mess Committee for the next menu.",
                )
                return redirect("messops:student-preferences")

    latest = (
        MonthlyMenuCycle.objects.exclude(status=MonthlyMenuCycle.Status.CLOSED)
        .order_by("-month")
        .first()
    )
    return render(
        request,
        "messops/student_preferences.html",
        {
            "cycle": cycle,
            "latest": latest,
            "form": form,
            "existing": existing,
            "active": "preferences",
        },
    )


@perm_required("committee.manage")
def manager_committee(request):
    """Maintain the committee roster and record monthly review meetings."""
    editing = None
    member_pk = request.POST.get("member_id") or request.GET.get("member")
    if member_pk:
        editing = get_object_or_404(CommitteeMember, pk=member_pk)

    member_form = CommitteeMemberForm(instance=editing)
    meeting_form = ReviewMeetingForm()

    if request.method == "POST":
        action = request.POST.get("action", "")
        if action == "member-save":
            member_form = CommitteeMemberForm(request.POST, instance=editing)
            if member_form.is_valid():
                member = member_form.save()
                messages.success(request, f"{member.display_name} saved to the committee.")
                return redirect("messops:manager-committee")
        elif action == "member-deactivate":
            member = get_object_or_404(CommitteeMember, pk=request.POST.get("member_id"))
            member.is_active = False
            member.save(update_fields=["is_active", "updated_at"])
            messages.success(request, f"{member.display_name} marked inactive.")
            return redirect("messops:manager-committee")
        elif action == "meeting-save":
            meeting_form = ReviewMeetingForm(request.POST)
            if meeting_form.is_valid():
                meeting = record_review_meeting(
                    meeting_date=meeting_form.cleaned_data["meeting_date"],
                    attendees=meeting_form.cleaned_data["attendees"],
                    agenda=meeting_form.cleaned_data["agenda"],
                    minutes=meeting_form.cleaned_data["minutes"],
                    actions=meeting_form.cleaned_data["actions"],
                    escalated=meeting_form.cleaned_data["escalated"],
                    actor=request.user,
                )
                messages.success(
                    request, f"Review meeting for {meeting.label} recorded."
                )
                return redirect("messops:manager-committee")

    composition = committee_composition()
    meetings = (
        ReviewMeeting.objects.select_related("created_by")
        .order_by("-meeting_date", "-created_at")[:12]
    )
    return render(
        request,
        "messops/manager_committee.html",
        {
            "member_form": member_form,
            "meeting_form": meeting_form,
            "editing": editing,
            "composition": composition,
            "meetings": meetings,
            "cycle": current_cycle(),
            "active": "committee",
        },
    )


@perm_required("menu.cycle")
def manager_menu_cycle(request):
    """Run the monthly menu-planning cycle from the operator list to publication."""
    selected_month = _month(request.GET.get("month"))
    cycle = cycle_for_month(selected_month) if selected_month else current_cycle()
    change_form = MenuChangeForm()
    if request.method == "POST":
        action = request.POST.get("action", "")
        if action == "create":
            month = _month(request.POST.get("month"))
            if not month:
                messages.error(request, "Choose the month this cycle plans.")
            elif MonthlyMenuCycle.objects.filter(month=month).exists():
                messages.info(request, "A cycle already exists for that month.")
                return redirect(
                    f"{reverse('messops:manager-menu-cycle')}?month={month:%Y-%m}"
                )
            else:
                cycle = MonthlyMenuCycle.objects.create(
                    month=month, created_by=request.user
                )
                messages.success(request, f"{cycle.label} menu cycle created.")
                return redirect(
                    f"{reverse('messops:manager-menu-cycle')}?month={month:%Y-%m}"
                )
        elif cycle is None:
            messages.error(request, "Create a menu cycle first.")
        elif action in {"save", "open", "finalize", "publish"}:
            try:
                update_cycle_text(
                    cycle,
                    available_items=_posted_or_none(request, "available_items"),
                    operator_notes=_posted_or_none(request, "operator_notes"),
                    student_preferences=_posted_or_none(request, "student_preferences"),
                    final_menu=_posted_or_none(request, "final_menu"),
                    approved_by_name=_posted_or_none(request, "approved_by_name"),
                )
                if action != "save":
                    transition_cycle(cycle, action, actor=request.user)
            except SopError as exc:
                messages.error(request, str(exc))
            else:
                if action == "save":
                    messages.success(request, "Menu-cycle details saved.")
                else:
                    messages.success(
                        request, f"{cycle.label}: {cycle.get_status_display()}."
                    )
                return redirect(
                    f"{reverse('messops:manager-menu-cycle')}?month={cycle.month:%Y-%m}"
                )
        elif action == "change-save":
            change_form = MenuChangeForm(request.POST)
            if change_form.is_valid():
                try:
                    record_change_request(
                        service_date=change_form.cleaned_data["service_date"],
                        meal=change_form.cleaned_data.get("meal"),
                        summary=change_form.cleaned_data["summary"],
                        reason=change_form.cleaned_data["reason"],
                        proposed_by=request.user,
                    )
                except SopError as exc:
                    messages.error(request, str(exc))
                else:
                    messages.success(
                        request, "Proposed menu change recorded for mutual discussion."
                    )
                    return redirect("messops:manager-menu-cycle")
            else:
                messages.error(request, "Check the proposed change fields.")
        elif action == "change-status":
            change = get_object_or_404(
                MenuChangeRequest, pk=request.POST.get("change_id")
            )
            try:
                update_change_request(
                    change, status=request.POST.get("status", ""), actor=request.user
                )
            except SopError as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, f"Menu change marked {change.get_status_display()}.")
                return redirect("messops:manager-menu-cycle")

    preferences = (
        MenuPreference.objects.select_related("student__profile", "meal")
        .filter(cycle=cycle)
        .order_by("student__profile__student_code", "-updated_at")
        if cycle
        else MenuPreference.objects.none()
    )
    changes = (
        MenuChangeRequest.objects.select_related("meal", "proposed_by", "agreed_by")
        .order_by("-created_at")[:20]
    )
    return render(
        request,
        "messops/manager_menu_cycle.html",
        {
            "cycle": cycle,
            "cycles": MonthlyMenuCycle.objects.all()[:12],
            "preferences": preferences,
            "preference_count": preferences.count() if cycle else 0,
            "changes": changes,
            "change_form": change_form,
            "next_month": next_month(),
            "month_param": selected_month,
            "statuses": MonthlyMenuCycle.Status.choices,
            "change_statuses": MenuChangeRequest.Status.choices,
            "active": "menu-cycle",
        },
    )


@perm_required("complaint.manage")
def manager_complaints(request):
    """The committee/manager grievance queue with resolution and escalation."""
    qs = Complaint.objects.select_related(
        "student__profile", "meal", "handled_by"
    ).order_by("-created_at")
    status_filter = request.GET.get("status", "")
    category_filter = request.GET.get("category", "")
    if status_filter in Complaint.Status.values:
        qs = qs.filter(status=status_filter)
    if category_filter in Complaint.Category.values:
        qs = qs.filter(category=category_filter)

    return render(
        request,
        "messops/manager_complaints.html",
        {
            "complaints": qs[:100],
            "total": qs.count(),
            "status_filter": status_filter,
            "category_filter": category_filter,
            "statuses": Complaint.Status.choices,
            "categories": Complaint.Category.choices,
            "open_count": Complaint.objects.filter(
                status__in=[Complaint.Status.OPEN, Complaint.Status.ACKNOWLEDGED]
            ).count(),
            "escalated_count": Complaint.objects.filter(
                status=Complaint.Status.ESCALATED
            ).count(),
            "active": "complaints-manage",
        },
    )


@require_POST
@perm_required("complaint.manage")
def manager_complaint_update(request, pk: int):
    complaint = get_object_or_404(Complaint, pk=pk)
    try:
        update_complaint(
            complaint,
            status=request.POST.get("status", complaint.status),
            resolution=request.POST.get("resolution", ""),
            committee_note=request.POST.get("committee_note", ""),
            actor=request.user,
        )
    except SopError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"{complaint.reference} marked {complaint.get_status_display()}.")
    return redirect("messops:manager-complaints")


@perm_required("menu.view")
def menu_board(request):
    """A printable canteen notice-board view of the agreed menu."""
    date = parse_date(request.GET.get("date", "")) or timezone.localdate()
    menus = (
        DailyMenu.objects.filter(date=date)
        .select_related("meal")
        .prefetch_related("items__dish")
        .order_by("meal__order", "meal__start_time")
    )
    rows = [
        {"menu": menu, "items": list(menu.items.select_related("dish"))}
        for menu in menus
    ]
    return render(
        request,
        "messops/menu_board.html",
        {
            "date": date,
            "rows": rows,
            "cycle": latest_published_cycle(),
            "meal_slots": MealSlot.objects.order_by("order"),
            "can_print": True,
            "active": "menu-board",
        },
    )

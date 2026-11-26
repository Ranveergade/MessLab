"""Mess committee and SOP records: monthly planning, preferences, grievances and reviews.

These models implement the official Mess Committee SOP. They sit alongside the
operational dining tables so the committee has one place to plan the monthly
menu, collect student preferences, record complaints and minute its reviews.
"""
from __future__ import annotations

import datetime as dt

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from menu.models import MealSlot


def month_start(value: dt.date) -> dt.date:
    """Normalise any date to the first day of its month."""
    return value.replace(day=1)


class CommitteeMember(models.Model):
    """A member of the official Mess Committee or a linked operator/chairperson."""

    class Program(models.TextChoices):
        UG = "ug", "Undergraduate (UG)"
        PG = "pg", "Postgraduate (PG)"
        NA = "na", "Not applicable"

    class Role(models.TextChoices):
        REPRESENTATIVE = "representative", "Student representative"
        CHAIRPERSON = "chairperson", "Chairperson – Hostel Residence"
        OPERATOR = "operator", "Mess operator"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="committee_roles",
        help_text="Optional MessLab account for this member.",
    )
    full_name = models.CharField(max_length=120, blank=True)
    email = models.EmailField(blank=True)
    program = models.CharField(
        max_length=8, choices=Program.choices, default=Program.UG
    )
    role = models.CharField(
        max_length=20, choices=Role.choices, default=Role.REPRESENTATIVE
    )
    term_start = models.DateField(default=timezone.localdate)
    term_end = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["role", "program", "full_name"]
        indexes = [
            models.Index(fields=["role", "is_active"], name="comm_role_active_idx"),
            models.Index(fields=["program", "is_active"], name="comm_program_active_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.display_name} ({self.get_role_display()})"

    @property
    def is_representative(self) -> bool:
        return self.role in {self.Role.REPRESENTATIVE}

    @property
    def is_chairperson(self) -> bool:
        return self.role in {self.Role.CHAIRPERSON}

    @property
    def is_operator(self) -> bool:
        return self.role in {self.Role.OPERATOR}

    @property
    def is_ug(self) -> bool:
        return self.program in {self.Program.UG}

    @property
    def is_pg(self) -> bool:
        return self.program in {self.Program.PG}

    @property
    def display_name(self) -> str:
        if self.full_name:
            return self.full_name
        if self.user_id:
            return self.user.get_full_name() or self.user.username
        return "Unnamed member"

    @property
    def display_email(self) -> str:
        return self.email or (self.user.email if self.user_id else "")

    def clean(self) -> None:
        if not self.full_name and not self.user_id:
            raise ValidationError(
                {"full_name": "Add a name or link a MessLab account for this member."}
            )
        if self.is_representative:
            if self.program not in {self.Program.UG, self.Program.PG}:
                raise ValidationError(
                    {"program": "Student representatives must be UG or PG."}
                )
            active = CommitteeMember.objects.filter(
                is_active=True,
                role=self.Role.REPRESENTATIVE,
                program=self.program,
            )
            if self.pk:
                active = active.exclude(pk=self.pk)
            if active.count() >= 2:
                raise ValidationError(
                    {
                        "program": (
                            "The SOP allows two active representatives per programme. "
                            "Deactivate an existing representative first."
                        )
                    }
                )

    def save(self, *args, **kwargs):
        if not self.full_name and self.user_id:
            self.full_name = self.user.get_full_name() or self.user.username
        super().save(*args, **kwargs)


class MonthlyMenuCycle(models.Model):
    """One monthly menu-planning cycle, from the operator's list to publication."""

    class Status(models.TextChoices):
        OPERATOR_LIST = "operator_list", "Awaiting operator list"
        CONSULTATION = "consultation", "Student consultation open"
        FINALIZED = "finalized", "Menu mutually finalized"
        PUBLISHED = "published", "Published to students"
        CLOSED = "closed", "Closed"

    month = models.DateField(
        unique=True, help_text="First day of the month this cycle plans."
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.OPERATOR_LIST
    )
    available_items = models.TextField(
        blank=True,
        help_text=(
            "The operator's exhaustive list of breakfast, lunch, snacks and dinner "
            "items available for the month, with regular, special and limited items."
        ),
    )
    operator_notes = models.TextField(blank=True)
    student_preferences = models.TextField(
        blank=True, help_text="Consolidated preferences communicated by the committee."
    )
    final_menu = models.TextField(
        blank=True,
        help_text=(
            "The mutually agreed monthly menu. Mention unlimited, special and "
            "limited items, quantities and frequencies."
        ),
    )
    approved_by_name = models.CharField(
        max_length=120,
        blank=True,
        help_text="Name of the Chairperson – Hostel Residence who approved the menu.",
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="menu_cycles_approved",
        help_text="MessLab account that recorded the approval.",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="menu_cycles_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-month"]
        indexes = [
            models.Index(fields=["status", "-month"], name="cycle_status_month_idx")
        ]
        constraints = [
            models.UniqueConstraint(fields=["month"], name="uniq_monthly_menu_cycle")
        ]

    def __str__(self) -> str:
        return f"{self.label} ({self.get_status_display()})"

    def clean(self) -> None:
        if self.month:
            self.month = month_start(self.month)

    def save(self, *args, **kwargs):
        if self.month:
            self.month = month_start(self.month)
        super().save(*args, **kwargs)

    @property
    def label(self) -> str:
        return self.month.strftime("%B %Y") if self.month else "—"

    @property
    def accepts_preferences(self) -> bool:
        return self.status in {self.Status.CONSULTATION}

    @property
    def is_published(self) -> bool:
        return self.status in {self.Status.PUBLISHED}

    @property
    def is_closed(self) -> bool:
        return self.status in {self.Status.CLOSED}


class MenuPreference(models.Model):
    """One consolidated menu preference submitted by a student for a cycle."""

    cycle = models.ForeignKey(
        MonthlyMenuCycle, on_delete=models.CASCADE, related_name="preferences"
    )
    student = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="menu_preferences",
    )
    meal = models.ForeignKey(
        MealSlot,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="menu_preferences",
        help_text="Optional: a preference for one meal, or leave blank for the whole menu.",
    )
    preference = models.TextField(
        max_length=2000, help_text="Dishes you would like on next month's menu."
    )
    notes = models.CharField(max_length=240, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["cycle", "student"], name="uniq_preference_cycle_student"
            )
        ]
        indexes = [
            models.Index(fields=["cycle", "-updated_at"], name="pref_cycle_updated_idx")
        ]

    def __str__(self) -> str:
        return f"{self.cycle.label} preference from {self.student_id}"


class Complaint(models.Model):
    """A student grievance recorded and tracked under the SOP."""

    class Category(models.TextChoices):
        MENU_DEVIATION = "menu_deviation", "Agreed menu not followed"
        ITEM_UNAVAILABLE = "item_unavailable", "Agreed item not available"
        UNAPPROVED_CHANGE = "unapproved_change", "Menu changed without notice"
        PORTION = "portion", "Portion / limited quantity"
        UNLIMITED = "unlimited", "Unlimited item not available"
        QUALITY = "quality", "Quality, hygiene or food safety"
        OTHER = "other", "Other discrepancy"

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        ACKNOWLEDGED = "acknowledged", "Acknowledged"
        RESOLVED = "resolved", "Resolved"
        ESCALATED = "escalated", "Escalated to Chairperson"
        CLOSED = "closed", "Closed"

    student = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="mess_complaints",
    )
    category = models.CharField(
        max_length=24, choices=Category.choices, default=Category.OTHER
    )
    service_date = models.DateField(
        null=True,
        blank=True,
        help_text="The meal date this complaint refers to, if known.",
    )
    meal = models.ForeignKey(
        MealSlot,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="complaints",
    )
    description = models.TextField(max_length=4000)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.OPEN
    )
    committee_note = models.TextField(blank=True, max_length=2000)
    resolution = models.TextField(blank=True, max_length=2000)
    handled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="complaints_handled",
    )
    escalated_at = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "-created_at"], name="complaint_status_idx"),
            models.Index(fields=["category", "-created_at"], name="complaint_category_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.reference} {self.get_category_display()}"

    @property
    def reference(self) -> str:
        return f"CMP-{self.pk:04d}" if self.pk else "CMP—"

    @property
    def is_open(self) -> bool:
        return self.status in {self.Status.OPEN, self.Status.ACKNOWLEDGED}

    @property
    def is_escalated(self) -> bool:
        return self.status in {self.Status.ESCALATED}


class MenuChangeRequest(models.Model):
    """A proposed change to a finalized menu, recorded before implementation."""

    class Status(models.TextChoices):
        PROPOSED = "proposed", "Proposed"
        AGREED = "agreed", "Mutually agreed"
        REJECTED = "rejected", "Not agreed"
        IMPLEMENTED = "implemented", "Implemented and communicated"

    proposed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="menu_change_requests",
    )
    service_date = models.DateField()
    meal = models.ForeignKey(
        MealSlot,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="menu_change_requests",
    )
    summary = models.CharField(max_length=240)
    reason = models.TextField(max_length=2000, help_text="Why the change is needed.")
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.PROPOSED
    )
    agreed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="menu_changes_agreed",
    )
    agreed_at = models.DateTimeField(null=True, blank=True)
    communicated_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "-created_at"], name="change_status_idx")
        ]

    def __str__(self) -> str:
        return f"{self.service_date} {self.summary}"


class ReviewMeeting(models.Model):
    """Monthly review meeting between the Mess Committee and the operator."""

    DEFAULT_AGENDA = (
        "1. Review adherence to the current month's approved menu.\n"
        "2. Discuss student feedback, complaints and grievances.\n"
        "3. Review food quality, quantity, variety and service standards.\n"
        "4. Identify recurring discrepancies and corrective measures.\n"
        "5. Discuss and mutually finalize the menu for the forthcoming month."
    )

    month = models.DateField(help_text="First day of the month being reviewed.")
    meeting_date = models.DateField(default=timezone.localdate)
    attendees = models.TextField(
        blank=True, help_text="All four committee representatives and the mess operator."
    )
    agenda = models.TextField(blank=True)
    minutes = models.TextField(blank=True)
    actions = models.TextField(
        blank=True, help_text="Agreed corrective actions and owners."
    )
    escalated = models.BooleanField(
        default=False, help_text="Referred to the Chairperson – Hostel Residence."
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="review_meetings_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-meeting_date", "-created_at"]
        indexes = [
            models.Index(fields=["-meeting_date"], name="review_meeting_date_idx")
        ]

    def __str__(self) -> str:
        return f"Review {self.meeting_date:%d %b %Y}"

    def save(self, *args, **kwargs):
        if self.month:
            self.month = month_start(self.month)
        if not self.agenda:
            self.agenda = self.DEFAULT_AGENDA
        super().save(*args, **kwargs)

    @property
    def label(self) -> str:
        return self.month.strftime("%B %Y") if self.month else "—"

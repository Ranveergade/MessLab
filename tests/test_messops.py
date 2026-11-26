"""Mess Committee SOP: composition, menu cycles, preferences, complaints and reviews."""
from __future__ import annotations

import datetime as dt

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from menu.models import MenuItem
from messops.models import (
    CommitteeMember,
    Complaint,
    MenuChangeRequest,
    MenuPreference,
    MonthlyMenuCycle,
    ReviewMeeting,
)
from messops.services import (
    SopError,
    committee_composition,
    record_change_request,
    save_preference,
    submit_complaint,
    transition_cycle,
    update_complaint,
    update_cycle_text,
)
from tests.factories import (
    BASE_DATE,
    make_dish,
    make_manager,
    make_menu,
    make_slots,
    make_student,
)

CYCLE_MONTH = dt.date(2026, 4, 1)


def make_cycle(status: str = MonthlyMenuCycle.Status.OPERATOR_LIST, **kwargs):
    defaults = {"available_items": "Poha, idli, rice, dal, roti, paneer"}
    defaults.update(kwargs)
    return MonthlyMenuCycle.objects.create(
        month=CYCLE_MONTH, status=status, **defaults
    )


def make_rep(name: str, program: str) -> CommitteeMember:
    return CommitteeMember.objects.create(
        full_name=name, program=program, role=CommitteeMember.Role.REPRESENTATIVE
    )


class SopPageTests(TestCase):
    def test_anonymous_visitor_is_redirected_to_login(self):
        response = self.client.get(reverse("messops:sop"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response["Location"])

    def test_student_sees_the_full_sop_with_the_committee_state(self):
        make_rep("Asha UG", CommitteeMember.Program.UG)
        make_rep("Bilal PG", CommitteeMember.Program.PG)
        student = make_student()
        self.client.force_login(student)

        response = self.client.get(reverse("messops:sop"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Standard Operating Procedure")
        self.assertContains(response, "Composition of the Mess Committee")
        self.assertContains(response, "Classification of menu items")
        self.assertContains(response, "Complaints, discrepancies and grievance redressal")
        self.assertContains(response, "Voluntary food-waste reduction initiative")
        self.assertEqual(response.context["composition"]["ug_count"], 1)
        self.assertEqual(response.context["composition"]["pg_count"], 1)
        self.assertFalse(response.context["composition"]["is_complete"])

    def test_sop_page_reports_a_complete_committee(self):
        for program in (
            CommitteeMember.Program.UG,
            CommitteeMember.Program.UG,
            CommitteeMember.Program.PG,
            CommitteeMember.Program.PG,
        ):
            make_rep(f"{program} rep", program)
        self.client.force_login(make_student())

        response = self.client.get(reverse("messops:sop"))

        self.assertTrue(response.context["composition"]["is_complete"])
        self.assertContains(response, "Composition complete")


class CommitteeCompositionTests(TestCase):
    def test_two_representatives_per_programme_are_allowed(self):
        make_rep("UG One", CommitteeMember.Program.UG)
        make_rep("UG Two", CommitteeMember.Program.UG)
        make_rep("PG One", CommitteeMember.Program.PG)
        make_rep("PG Two", CommitteeMember.Program.PG)

        composition = committee_composition()

        self.assertTrue(composition["is_complete"])
        self.assertEqual(composition["ug_count"], 2)
        self.assertEqual(composition["pg_count"], 2)

    def test_a_third_representative_for_one_programme_is_rejected(self):
        make_rep("UG One", CommitteeMember.Program.UG)
        make_rep("UG Two", CommitteeMember.Program.UG)
        extra = CommitteeMember(
            full_name="UG Three",
            program=CommitteeMember.Program.UG,
            role=CommitteeMember.Role.REPRESENTATIVE,
        )
        with self.assertRaises(ValidationError):
            extra.full_clean()

    def test_inactive_representatives_do_not_count_towards_the_limit(self):
        first = make_rep("UG One", CommitteeMember.Program.UG)
        make_rep("UG Two", CommitteeMember.Program.UG)
        first.is_active = False
        first.save(update_fields=["is_active"])

        replacement = CommitteeMember(
            full_name="UG Three",
            program=CommitteeMember.Program.UG,
            role=CommitteeMember.Role.REPRESENTATIVE,
        )
        replacement.full_clean()  # no error

    def test_a_member_needs_a_name_or_a_linked_account(self):
        member = CommitteeMember(program=CommitteeMember.Program.UG)
        with self.assertRaises(ValidationError):
            member.full_clean()

    def test_manager_adds_and_deactivates_a_member_through_the_web(self):
        manager = make_manager()
        self.client.force_login(manager)

        response = self.client.post(
            reverse("messops:manager-committee"),
            {
                "action": "member-save",
                "full_name": "Chitra PG",
                "email": "chitra@example.edu",
                "program": CommitteeMember.Program.PG,
                "role": CommitteeMember.Role.REPRESENTATIVE,
                "term_start": "2026-03-02",
                "is_active": "on",
            },
        )
        self.assertEqual(response.status_code, 302)
        member = CommitteeMember.objects.get(full_name="Chitra PG")
        self.assertTrue(member.is_active)

        response = self.client.post(
            reverse("messops:manager-committee"),
            {"action": "member-deactivate", "member_id": member.pk},
        )
        self.assertEqual(response.status_code, 302)
        member.refresh_from_db()
        self.assertFalse(member.is_active)

    def test_students_cannot_open_the_committee_workspace(self):
        self.client.force_login(make_student())
        response = self.client.get(reverse("messops:manager-committee"))
        self.assertEqual(response.status_code, 403)


class MenuCycleTests(TestCase):
    def test_consultation_requires_the_operator_item_list(self):
        cycle = make_cycle(available_items="")
        with self.assertRaises(SopError):
            transition_cycle(cycle, "open")

    def test_cycle_moves_from_operator_list_to_published(self):
        cycle = make_cycle()
        manager = make_manager()

        transition_cycle(cycle, "open", actor=manager)
        self.assertEqual(cycle.status, MonthlyMenuCycle.Status.CONSULTATION)

        with self.assertRaises(SopError):
            transition_cycle(cycle, "finalize", actor=manager)

        update_cycle_text(cycle, final_menu="Rice, dal, roti — unlimited")
        transition_cycle(cycle, "finalize", actor=manager)
        self.assertEqual(cycle.status, MonthlyMenuCycle.Status.FINALIZED)
        self.assertIsNotNone(cycle.approved_at)
        self.assertEqual(cycle.approved_by, manager)

        transition_cycle(cycle, "publish", actor=manager)
        self.assertEqual(cycle.status, MonthlyMenuCycle.Status.PUBLISHED)
        self.assertIsNotNone(cycle.published_at)

    def test_closed_cycles_cannot_be_edited(self):
        cycle = make_cycle(status=MonthlyMenuCycle.Status.CLOSED)
        with self.assertRaises(SopError):
            update_cycle_text(cycle, final_menu="A new menu")

    def test_manager_runs_the_cycle_through_the_web(self):
        manager = make_manager()
        self.client.force_login(manager)
        url = reverse("messops:manager-menu-cycle")

        self.client.post(
            url,
            {"action": "create", "month": "2026-04"},
        )
        cycle = MonthlyMenuCycle.objects.get(month=CYCLE_MONTH)
        self.assertEqual(cycle.status, MonthlyMenuCycle.Status.OPERATOR_LIST)

        page = self.client.get(url)
        self.assertContains(page, "Operator's available-item list")
        self.assertContains(page, "Proposed changes to the agreed menu")

        self.client.post(
            url,
            {
                "action": "open",
                "available_items": "Poha, idli, rice, dal, roti",
                "operator_notes": "Seasonal vegetables",
            },
        )
        cycle.refresh_from_db()
        self.assertEqual(cycle.status, MonthlyMenuCycle.Status.CONSULTATION)

        self.client.post(
            url,
            {
                "action": "finalize",
                "final_menu": "Rice, dal, roti — unlimited; paneer — special 2×/month",
                "approved_by_name": "Dean of Hostel Residence",
            },
        )
        cycle.refresh_from_db()
        self.assertEqual(cycle.status, MonthlyMenuCycle.Status.FINALIZED)
        self.assertEqual(cycle.approved_by_name, "Dean of Hostel Residence")

        self.client.post(url, {"action": "publish"})
        cycle.refresh_from_db()
        self.assertEqual(cycle.status, MonthlyMenuCycle.Status.PUBLISHED)

    def test_students_cannot_run_the_cycle(self):
        self.client.force_login(make_student())
        response = self.client.get(reverse("messops:manager-menu-cycle"))
        self.assertEqual(response.status_code, 403)


class MenuPreferenceTests(TestCase):
    def test_preference_is_recorded_and_then_updated(self):
        cycle = make_cycle(status=MonthlyMenuCycle.Status.CONSULTATION)
        student = make_student()
        self.client.force_login(student)
        url = reverse("messops:student-preferences")

        response = self.client.post(
            url, {"preference": "More seasonal vegetables and Friday biryani."}
        )
        self.assertEqual(response.status_code, 302)
        preference = MenuPreference.objects.get(cycle=cycle, student=student)
        self.assertIn("biryani", preference.preference)

        page = self.client.get(url)
        self.assertContains(page, "Consultation open")
        self.assertContains(page, "already submitted a preference")

        self.client.post(url, {"preference": "Keep Sunday chole bhature."})
        preference.refresh_from_db()
        self.assertEqual(
            MenuPreference.objects.filter(cycle=cycle, student=student).count(), 1
        )
        self.assertIn("chole", preference.preference)

    def test_preference_is_refused_when_consultation_is_closed(self):
        make_cycle(status=MonthlyMenuCycle.Status.FINALIZED)
        student = make_student()
        self.client.force_login(student)

        response = self.client.post(
            reverse("messops:student-preferences"),
            {"preference": "Please add dosa."},
            follow=True,
        )

        self.assertContains(response, "not open right now")
        self.assertFalse(MenuPreference.objects.exists())

    def test_service_also_guards_against_closed_consultation(self):
        cycle = make_cycle(status=MonthlyMenuCycle.Status.FINALIZED)
        with self.assertRaises(SopError):
            save_preference(
                cycle=cycle, student=make_student(), preference="Something new"
            )


class ComplaintFlowTests(TestCase):
    def test_student_submits_a_complaint_and_the_manager_resolves_it(self):
        slots = make_slots()
        student = make_student()
        manager = make_manager()

        self.client.force_login(student)
        response = self.client.post(
            reverse("messops:student-complaints"),
            {
                "category": Complaint.Category.PORTION,
                "service_date": BASE_DATE.isoformat(),
                "meal": slots["lunch"].pk,
                "description": "The paneer portion was much smaller than the agreed 150 g.",
            },
        )
        self.assertEqual(response.status_code, 302)
        complaint = Complaint.objects.get(student=student)
        self.assertEqual(complaint.status, Complaint.Status.OPEN)
        self.assertEqual(complaint.reference, f"CMP-{complaint.pk:04d}")

        self.client.force_login(manager)
        response = self.client.post(
            reverse("messops:manager-complaint-update", args=[complaint.pk]),
            {
                "status": Complaint.Status.RESOLVED,
                "committee_note": "Raised with the operator at the counter.",
                "resolution": "Operator will serve the agreed 150 g portion.",
            },
        )
        self.assertEqual(response.status_code, 302)
        complaint.refresh_from_db()
        self.assertEqual(complaint.status, Complaint.Status.RESOLVED)
        self.assertIsNotNone(complaint.resolved_at)
        self.assertEqual(complaint.handled_by, manager)

        self.client.force_login(student)
        page = self.client.get(reverse("messops:student-complaints"))
        self.assertContains(page, complaint.reference)
        self.assertContains(page, "Operator will serve the agreed 150 g portion.")

    def test_escalation_is_timestamped(self):
        complaint = submit_complaint(
            student=make_student(),
            category=Complaint.Category.QUALITY,
            description="The dal was cold and had an unusual smell at dinner.",
        )
        update_complaint(complaint, status=Complaint.Status.ESCALATED, actor=make_manager())

        complaint.refresh_from_db()

        self.assertTrue(complaint.is_escalated)
        self.assertIsNotNone(complaint.escalated_at)

    def test_short_descriptions_are_rejected_by_the_form(self):
        self.client.force_login(make_student())
        response = self.client.post(
            reverse("messops:student-complaints"),
            {"category": Complaint.Category.OTHER, "description": "bad"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Complaint.objects.exists())

    def test_students_cannot_open_the_manager_queue(self):
        self.client.force_login(make_student())
        response = self.client.get(reverse("messops:manager-complaints"))
        self.assertEqual(response.status_code, 403)

    def test_manager_queue_filters_by_status(self):
        manager = make_manager()
        student = make_student()
        open_one = submit_complaint(
            student=student,
            category=Complaint.Category.ITEM_UNAVAILABLE,
            description="The agreed fruit was not available at breakfast.",
        )
        resolved = submit_complaint(
            student=student,
            category=Complaint.Category.OTHER,
            description="The serving counter ran out of clean spoons.",
        )
        update_complaint(resolved, status=Complaint.Status.RESOLVED, actor=manager)

        self.client.force_login(manager)
        response = self.client.get(
            reverse("messops:manager-complaints"),
            {"status": Complaint.Status.RESOLVED},
        )

        self.assertContains(response, resolved.reference)
        self.assertNotContains(response, open_one.reference)


class MenuChangeTests(TestCase):
    def test_a_change_must_be_agreed_before_it_is_implemented(self):
        manager = make_manager()
        change = record_change_request(
            service_date=BASE_DATE,
            summary="Replace paneer with soya curry",
            reason="Paneer was not delivered on time.",
            proposed_by=manager,
        )
        self.assertEqual(change.status, MenuChangeRequest.Status.PROPOSED)

        from messops.services import update_change_request

        update_change_request(change, status=MenuChangeRequest.Status.AGREED, actor=manager)
        change.refresh_from_db()
        self.assertIsNotNone(change.agreed_at)
        self.assertIsNone(change.communicated_at)

        update_change_request(
            change, status=MenuChangeRequest.Status.IMPLEMENTED, actor=manager
        )
        change.refresh_from_db()
        self.assertIsNotNone(change.communicated_at)

    def test_change_requires_a_summary_and_a_reason(self):
        with self.assertRaises(SopError):
            record_change_request(
                service_date=BASE_DATE, summary="", reason="Something happened"
            )
        with self.assertRaises(SopError):
            record_change_request(
                service_date=BASE_DATE, summary="Swap a dish", reason="  "
            )

    def test_manager_records_and_agrees_a_change_through_the_web(self):
        manager = make_manager()
        self.client.force_login(manager)
        make_cycle()
        url = reverse("messops:manager-menu-cycle")

        response = self.client.post(
            url,
            {
                "action": "change-save",
                "service_date": BASE_DATE.isoformat(),
                "summary": "Replace paneer with soya curry",
                "reason": "The paneer delivery was delayed.",
            },
        )
        self.assertEqual(response.status_code, 302)
        change = MenuChangeRequest.objects.get()
        self.assertEqual(change.status, MenuChangeRequest.Status.PROPOSED)
        self.assertEqual(change.proposed_by, manager)

        self.client.post(
            url,
            {
                "action": "change-status",
                "change_id": change.pk,
                "status": MenuChangeRequest.Status.AGREED,
            },
        )
        change.refresh_from_db()
        self.assertEqual(change.status, MenuChangeRequest.Status.AGREED)
        self.assertIsNotNone(change.agreed_at)

    def test_change_form_errors_are_shown_to_the_manager(self):
        manager = make_manager()
        self.client.force_login(manager)
        make_cycle()

        response = self.client.post(
            reverse("messops:manager-menu-cycle"),
            {
                "action": "change-save",
                "service_date": BASE_DATE.isoformat(),
                "summary": "",
                "reason": "",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(MenuChangeRequest.objects.exists())
        self.assertTrue(response.context["change_form"].errors)


class MenuBoardAndClassificationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.slots = make_slots()

    def test_serving_note_describes_each_classification(self):
        menu = make_menu(BASE_DATE, self.slots["lunch"], [])
        unlimited = MenuItem.objects.create(
            daily_menu=menu,
            dish=make_dish("Rice"),
            serving_category=MenuItem.ServingCategory.UNLIMITED,
        )
        special = MenuItem.objects.create(
            daily_menu=menu,
            dish=make_dish("Paneer"),
            serving_category=MenuItem.ServingCategory.SPECIAL,
            frequency_per_month=2,
        )
        limited = MenuItem.objects.create(
            daily_menu=menu,
            dish=make_dish("Gulab Jamun"),
            serving_category=MenuItem.ServingCategory.LIMITED,
            portion_grams=120,
            frequency_per_month=4,
        )

        self.assertEqual(unlimited.serving_note, "Unlimited")
        self.assertEqual(special.serving_note, "2×/month")
        self.assertEqual(limited.serving_note, "120 g portion · 4×/month")

    def test_canteen_board_shows_categories_and_serving_notes(self):
        menu = make_menu(BASE_DATE, self.slots["lunch"], [])
        MenuItem.objects.create(
            daily_menu=menu,
            dish=make_dish("Rice"),
            serving_category=MenuItem.ServingCategory.UNLIMITED,
        )
        MenuItem.objects.create(
            daily_menu=menu,
            dish=make_dish("Paneer Butter Masala"),
            serving_category=MenuItem.ServingCategory.SPECIAL,
            frequency_per_month=2,
        )
        MenuItem.objects.create(
            daily_menu=menu,
            dish=make_dish("Gulab Jamun"),
            serving_category=MenuItem.ServingCategory.LIMITED,
            portion_grams=120,
            frequency_per_month=4,
        )
        cycle = make_cycle(
            status=MonthlyMenuCycle.Status.PUBLISHED,
            final_menu="Rice — unlimited; paneer — special 2×/month; gulab jamun — 120 g, 4×/month",
        )
        self.client.force_login(make_student())

        response = self.client.get(
            reverse("messops:menu-board"), {"date": BASE_DATE.isoformat()}
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Rice")
        self.assertContains(response, "Unlimited")
        self.assertContains(response, "2×/month")
        self.assertContains(response, "120 g portion · 4×/month")
        self.assertContains(response, cycle.final_menu)

    def test_board_explains_when_nothing_is_published(self):
        self.client.force_login(make_student())
        response = self.client.get(
            reverse("messops:menu-board"), {"date": "2026-12-25"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No menu is published")

    def test_api_serializer_exposes_the_classification(self):
        from insights.serializers import MenuItemSerializer

        menu = make_menu(BASE_DATE, self.slots["lunch"], [])
        item = MenuItem.objects.create(
            daily_menu=menu,
            dish=make_dish("Kheer"),
            serving_category=MenuItem.ServingCategory.LIMITED,
            portion_grams=100,
            frequency_per_month=3,
        )

        data = MenuItemSerializer(item).data

        self.assertEqual(data["serving_category"], "limited")
        self.assertEqual(data["portion_grams"], 100)
        self.assertEqual(data["frequency_per_month"], 3)


class ReviewMeetingTests(TestCase):
    def test_manager_records_a_monthly_review_meeting(self):
        manager = make_manager()
        self.client.force_login(manager)

        response = self.client.post(
            reverse("messops:manager-committee"),
            {
                "action": "meeting-save",
                "meeting_date": "2026-03-26",
                "attendees": "Two UG reps, two PG reps, mess operator",
                "agenda": ReviewMeeting.DEFAULT_AGENDA,
                "minutes": "Menu followed except one paneer substitution.",
                "actions": "Operator to give 24-hour notice for substitutions.",
                "escalated": "on",
            },
        )

        self.assertEqual(response.status_code, 302)
        meeting = ReviewMeeting.objects.get()
        self.assertEqual(meeting.month, dt.date(2026, 3, 1))
        self.assertTrue(meeting.escalated)
        self.assertEqual(meeting.created_by, manager)

        page = self.client.get(reverse("messops:manager-committee"))
        self.assertContains(page, "26 Mar 2026")
        self.assertContains(page, "24-hour notice")

    def test_default_agenda_is_used_when_none_is_given(self):
        meeting = ReviewMeeting.objects.create(
            month=dt.date(2026, 3, 1), meeting_date=dt.date(2026, 3, 26)
        )
        self.assertEqual(meeting.agenda, ReviewMeeting.DEFAULT_AGENDA)

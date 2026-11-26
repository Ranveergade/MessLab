"""DRF serializers for the public API."""
from __future__ import annotations

from rest_framework import serializers

from accounts.account_admin import target_code
from accounts.forms import PHONE_VALIDATOR
from accounts.models import (
    AccountAudit,
    StudentProfile,
    User,
    validate_default_plan,
)
from accounts.otp import mask_email
from accounts.rbac import permissions_for
from accounts.registration import SELF_REGISTER_ROLES
from dining.models import (
    CheckIn,
    DataImport,
    MealFeedback,
    MealIntent,
    PreparationPlan,
    WasteLog,
)
from menu.models import DailyMenu, Dish, MealSlot, MenuItem


class UserSerializer(serializers.ModelSerializer):
    student_code = serializers.CharField(source="profile.student_code", read_only=True, default="")
    hostel = serializers.CharField(source="profile.hostel", read_only=True, default="")
    diet = serializers.CharField(source="profile.diet", read_only=True, default="")
    role = serializers.CharField(source="effective_role", read_only=True)
    permissions = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = (
            "id",
            "username",
            "first_name",
            "last_name",
            "role",
            "permissions",
            "email",
            "student_code",
            "hostel",
            "diet",
        )

    def get_permissions(self, obj) -> list[str]:
        return sorted(permissions_for(obj))


class OTPRequestSerializer(serializers.Serializer):
    email = serializers.EmailField()


class OTPVerifySerializer(serializers.Serializer):
    email = serializers.EmailField()
    code = serializers.RegexField(r"^\d{6}$")


class LogoutSerializer(serializers.Serializer):
    refresh = serializers.CharField(required=False, allow_blank=True)


class RegistrationSerializer(serializers.Serializer):
    full_name = serializers.CharField(max_length=150)
    email = serializers.EmailField(max_length=254)
    role = serializers.ChoiceField(choices=SELF_REGISTER_ROLES)
    hostel = serializers.ChoiceField(
        choices=StudentProfile.Hostel.choices, required=False, allow_blank=True
    )
    diet = serializers.ChoiceField(
        choices=StudentProfile.Diet.choices,
        required=False,
        default=StudentProfile.Diet.VEG,
    )
    phone = serializers.CharField(
        max_length=20,
        required=False,
        allow_blank=True,
        validators=[PHONE_VALIDATOR],
    )
    consent = serializers.BooleanField(required=False, default=True)

    def validate(self, attrs):
        role = attrs["role"]
        hostel = attrs.get("hostel") or None
        if role == User.Role.HOSTEL_STUDENT and not hostel:
            raise serializers.ValidationError({"hostel": "Choose your hostel."})
        attrs["hostel"] = hostel if role == User.Role.HOSTEL_STUDENT else None
        attrs["phone"] = (attrs.get("phone") or "").strip()
        attrs["email"] = attrs["email"].strip().lower()
        return attrs


class AdminUserSerializer(serializers.ModelSerializer):
    """Account row. Personal data is only included for administrators."""

    role = serializers.SerializerMethodField()
    student_code = serializers.SerializerMethodField()
    email = serializers.SerializerMethodField()
    username = serializers.SerializerMethodField()
    full_name = serializers.SerializerMethodField()
    is_pending = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = (
            "id",
            "student_code",
            "username",
            "full_name",
            "role",
            "email",
            "is_active",
            "is_pending",
            "date_joined",
        )

    def _show_pii(self) -> bool:
        return bool(self.context.get("show_pii"))

    def get_role(self, obj) -> str:
        return obj.effective_role

    def get_student_code(self, obj) -> str:
        return target_code(obj)

    def get_email(self, obj) -> str:
        return obj.email if self._show_pii() else mask_email(obj.email)

    def get_username(self, obj) -> str:
        return obj.username if self._show_pii() else ""

    def get_full_name(self, obj) -> str:
        return obj.get_full_name() if self._show_pii() else ""

    def get_is_pending(self, obj) -> bool:
        return bool(getattr(obj.role_profile, "is_pending", False))


class RoleUpdateSerializer(serializers.Serializer):
    role = serializers.ChoiceField(choices=User.Role.choices)


class ActiveUpdateSerializer(serializers.Serializer):
    active = serializers.BooleanField()


class AccountAuditSerializer(serializers.ModelSerializer):
    actor_code = serializers.SerializerMethodField()
    target_code = serializers.SerializerMethodField()
    action_label = serializers.CharField(source="get_action_display", read_only=True)

    class Meta:
        model = AccountAudit
        fields = ("id", "at", "action", "action_label", "actor_code", "target_code", "detail")

    def get_actor_code(self, obj) -> str:
        return target_code(obj.actor)

    def get_target_code(self, obj) -> str:
        return target_code(obj.target)


class DefaultPlanSerializer(serializers.Serializer):
    default_plan = serializers.JSONField()

    def validate_default_plan(self, value):
        try:
            validate_default_plan(value)
        except Exception as exc:
            raise serializers.ValidationError(str(exc)) from exc
        return value


class MealSlotSerializer(serializers.ModelSerializer):
    peak_label = serializers.CharField(read_only=True)

    class Meta:
        model = MealSlot
        fields = (
            "id",
            "name",
            "start_time",
            "end_time",
            "peak_start",
            "peak_end",
            "peak_label",
            "counters",
            "counter_capacity_per_hour",
            "order",
        )


class DishSerializer(serializers.ModelSerializer):
    class Meta:
        model = Dish
        fields = (
            "id",
            "name",
            "category",
            "diet_type",
            "avg_portion_grams",
            "popularity_baseline",
        )


class MenuItemSerializer(serializers.ModelSerializer):
    dish = DishSerializer(read_only=True)

    class Meta:
        model = MenuItem
        fields = (
            "id",
            "dish",
            "planned_quantity_kg",
            "is_special",
            "serving_category",
            "portion_grams",
            "frequency_per_month",
        )


class DailyMenuSerializer(serializers.ModelSerializer):
    meal = MealSlotSerializer(read_only=True)
    items = MenuItemSerializer(many=True, read_only=True)

    class Meta:
        model = DailyMenu
        fields = ("id", "date", "meal", "notes", "is_special", "items")


class DishWriteSerializer(serializers.ModelSerializer):
    class Meta:
        model = Dish
        fields = (
            "id",
            "name",
            "category",
            "diet_type",
            "avg_portion_grams",
            "popularity_baseline",
            "is_active",
        )

    def validate_name(self, value):
        value = value.strip()
        qs = Dish.objects.filter(name__iexact=value)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError("A dish with that name already exists.")
        return value


class MenuItemWriteSerializer(serializers.Serializer):
    dish = serializers.PrimaryKeyRelatedField(queryset=Dish.objects.all())
    planned_quantity_kg = serializers.DecimalField(
        max_digits=8, decimal_places=2, min_value=0
    )
    is_special = serializers.BooleanField(required=False, default=False)
    serving_category = serializers.ChoiceField(
        choices=MenuItem.ServingCategory.choices,
        required=False,
        default=MenuItem.ServingCategory.UNLIMITED,
    )
    portion_grams = serializers.IntegerField(
        required=False, allow_null=True, min_value=1, max_value=2000
    )
    frequency_per_month = serializers.IntegerField(
        required=False, allow_null=True, min_value=1, max_value=31
    )


class DailyMenuWriteSerializer(serializers.Serializer):
    date = serializers.DateField()
    meal = serializers.SlugRelatedField(
        slug_field="name", queryset=MealSlot.objects.all()
    )
    notes = serializers.CharField(
        max_length=240, required=False, allow_blank=True, default=""
    )
    is_special = serializers.BooleanField(required=False, default=False)
    items = MenuItemWriteSerializer(many=True)

    def validate_items(self, items):
        dish_ids = [item["dish"].pk for item in items]
        if len(dish_ids) != len(set(dish_ids)):
            raise serializers.ValidationError("Each dish can appear only once.")
        return items


class IntentSerializer(serializers.ModelSerializer):
    meal = serializers.SlugRelatedField(slug_field="name", queryset=MealSlot.objects.all())

    class Meta:
        model = MealIntent
        fields = ("id", "date", "meal", "status", "source", "expected_arrival_window", "updated_at")


class IntentUpsertSerializer(serializers.Serializer):
    date = serializers.DateField()
    meal = serializers.SlugRelatedField(slug_field="name", queryset=MealSlot.objects.all())
    status = serializers.ChoiceField(choices=MealIntent.Status.choices)
    expected_arrival_window = serializers.ChoiceField(
        choices=MealIntent.Arrival.choices, required=False, allow_blank=True
    )
    source = serializers.ChoiceField(choices=MealIntent.Source.choices, required=False)


class CheckInCreateSerializer(serializers.Serializer):
    date = serializers.DateField()
    meal = serializers.SlugRelatedField(slug_field="name", queryset=MealSlot.objects.all())
    dish_ids = serializers.ListField(child=serializers.IntegerField(), required=False, default=list)
    clean_plate = serializers.BooleanField(default=True)
    source = serializers.ChoiceField(choices=CheckIn.Source.choices, required=False)


class CheckInSerializer(serializers.ModelSerializer):
    meal = serializers.SlugRelatedField(slug_field="name", read_only=True)
    dishes_taken = DishSerializer(many=True, read_only=True)

    class Meta:
        model = CheckIn
        fields = ("id", "date", "meal", "timestamp", "dishes_taken", "clean_plate", "source")


class MealFeedbackSerializer(serializers.ModelSerializer):
    meal = serializers.SlugRelatedField(slug_field="name", read_only=True)
    student_code = serializers.CharField(
        source="student.profile.student_code", read_only=True, default=""
    )

    class Meta:
        model = MealFeedback
        fields = (
            "id",
            "date",
            "meal",
            "response",
            "suggestion",
            "student_code",
            "created_at",
            "updated_at",
        )


class MealFeedbackWriteSerializer(serializers.Serializer):
    date = serializers.DateField()
    meal = serializers.SlugRelatedField(
        slug_field="name", queryset=MealSlot.objects.all()
    )
    response = serializers.ChoiceField(choices=MealFeedback.Response.choices)
    suggestion = serializers.CharField(
        max_length=240, required=False, allow_blank=True, default=""
    )


class PassResponseSerializer(serializers.Serializer):
    date = serializers.DateField()
    token = serializers.CharField()
    qr_svg = serializers.CharField()


class ScanSerializer(serializers.Serializer):
    token = serializers.CharField(trim_whitespace=True, max_length=2048)
    meal = serializers.SlugRelatedField(
        slug_field="name",
        queryset=MealSlot.objects.all(),
        required=False,
        allow_null=True,
    )


class ManualCheckInSerializer(serializers.Serializer):
    code = serializers.CharField(max_length=40, trim_whitespace=True)
    meal = serializers.SlugRelatedField(
        slug_field="name",
        queryset=MealSlot.objects.all(),
        required=False,
        allow_null=True,
    )


class PreparationPlanSerializer(serializers.ModelSerializer):
    meal = serializers.SlugRelatedField(slug_field="name", read_only=True)
    dish = DishSerializer(read_only=True)

    class Meta:
        model = PreparationPlan
        fields = (
            "id",
            "date",
            "meal",
            "dish",
            "recommended_kg",
            "actual_prepared_kg",
            "baseline_kg",
            "status",
            "approved_by",
            "approved_at",
            "override_reason",
        )


class OverrideSerializer(serializers.Serializer):
    recommended_kg = serializers.DecimalField(
        max_digits=8, decimal_places=2, required=False, min_value=0
    )
    reason = serializers.CharField(max_length=1000)


class WasteLogSerializer(serializers.ModelSerializer):
    meal = serializers.SlugRelatedField(slug_field="name", read_only=True)
    dish = serializers.SlugRelatedField(slug_field="name", read_only=True)

    class Meta:
        model = WasteLog
        fields = (
            "id",
            "date",
            "meal",
            "dish",
            "prepared_kg",
            "consumed_kg",
            "wasted_kg",
            "leftover_reusable",
        )


class WasteLogWriteSerializer(serializers.Serializer):
    date = serializers.DateField()
    meal = serializers.SlugRelatedField(
        slug_field="name", queryset=MealSlot.objects.all()
    )
    dish = serializers.PrimaryKeyRelatedField(queryset=Dish.objects.all())
    prepared_kg = serializers.DecimalField(max_digits=8, decimal_places=2, min_value=0)
    consumed_kg = serializers.DecimalField(max_digits=8, decimal_places=2, min_value=0)
    wasted_kg = serializers.DecimalField(max_digits=8, decimal_places=2, min_value=0)
    leftover_reusable = serializers.BooleanField(default=False)


class CloseDaySerializer(serializers.Serializer):
    date = serializers.DateField()


class ImportSerializer(serializers.Serializer):
    kind = serializers.ChoiceField(choices=DataImport.Kind.choices)
    file = serializers.FileField()

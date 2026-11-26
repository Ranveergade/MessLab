"""Model run metadata shown in the manager's Model Health card."""
from __future__ import annotations

from django.db import models


class ModelRun(models.Model):
    version = models.CharField(max_length=64, unique=True)
    trained_at = models.DateTimeField(auto_now_add=True)
    train_rows = models.PositiveIntegerField(default=0)
    validation_rows = models.PositiveIntegerField(default=0)
    train_start = models.DateField(null=True, blank=True)
    train_end = models.DateField(null=True, blank=True)
    validation_start = models.DateField(null=True, blank=True)
    validation_end = models.DateField(null=True, blank=True)
    participation_mae = models.FloatField(default=0)
    participation_mape = models.FloatField(default=0)
    participation_baseline_mae = models.FloatField(default=0)
    participation_baseline_mape = models.FloatField(default=0)
    dish_mae_kg = models.FloatField(default=0)
    dish_mape = models.FloatField(default=0)
    dish_baseline_mae_kg = models.FloatField(default=0)
    dish_baseline_mape = models.FloatField(default=0)
    participation_algorithm = models.CharField(max_length=64, blank=True)
    dish_algorithm = models.CharField(max_length=64, blank=True)
    metrics = models.JSONField(default=dict, blank=True)
    is_active = models.BooleanField(default=True)
    notes = models.CharField(max_length=240, blank=True)

    class Meta:
        ordering = ["-trained_at"]
        indexes = [models.Index(fields=["-trained_at"], name="modelrun_trained_idx")]

    def __str__(self) -> str:
        return f"Model {self.version}"

    @property
    def participation_improvement_pct(self) -> float:
        if not self.participation_baseline_mae:
            return 0.0
        return (
            (self.participation_baseline_mae - self.participation_mae)
            / self.participation_baseline_mae
            * 100
        )

    @property
    def dish_improvement_pct(self) -> float:
        if not self.dish_baseline_mae_kg:
            return 0.0
        return (self.dish_baseline_mae_kg - self.dish_mae_kg) / self.dish_baseline_mae_kg * 100

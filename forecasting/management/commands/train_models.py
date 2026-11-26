from django.core.management.base import BaseCommand, CommandError

from forecasting.model import InsufficientData
from forecasting.pipeline import train_and_store


class Command(BaseCommand):
    help = "Train the two-stage participation + dish demand models and refresh forecasts."

    def add_arguments(self, parser):
        parser.add_argument("--validation-days", type=int, default=21)
        parser.add_argument("--no-forecast", action="store_true")
        parser.add_argument("--days", type=int, default=8, help="Forecast horizon in days.")

    def handle(self, *args, **options):
        try:
            run = train_and_store(
                validation_days=options["validation_days"],
                generate=not options["no_forecast"],
                days=options["days"],
            )
        except InsufficientData as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(f"Trained model {run.version}"))
        self.stdout.write(
            f"  Participation: {run.participation_algorithm} | "
            f"MAE {run.participation_mae:.1f} students | "
            f"MAPE {run.participation_mape:.1f}% | "
            f"baseline MAE {run.participation_baseline_mae:.1f} | "
            f"improvement {run.participation_improvement_pct:.1f}%"
        )
        self.stdout.write(
            f"  Dish demand:   {run.dish_algorithm} | "
            f"MAE {run.dish_mae_kg:.2f} kg | "
            f"MAPE {run.dish_mape:.1f}% | "
            f"baseline MAE {run.dish_baseline_mae_kg:.2f} kg | "
            f"improvement {run.dish_improvement_pct:.1f}%"
        )
        self.stdout.write(
            f"  Train rows: {run.train_rows} | Validation rows: {run.validation_rows} | "
            f"Validation: {run.validation_start} to {run.validation_end}"
        )
        if not options["no_forecast"]:
            self.stdout.write(self.style.SUCCESS("Forecasts and preparation plans refreshed."))

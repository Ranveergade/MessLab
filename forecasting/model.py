"""Training, evaluation and persistence for the two-stage forecast."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import numpy as np
import pandas as pd
from sklearn.ensemble import GradientBoostingRegressor, RandomForestRegressor

from .features import PARTICIPATION_FEATURES, DISH_FEATURES

RANDOM_STATE = 42


class InsufficientData(Exception):
    """Raised when there is not enough history to train a trustworthy model."""


@dataclass
class FitResult:
    model: object
    medians: dict[str, float]
    metrics: dict[str, float]
    residuals: np.ndarray
    algorithm: str


def _mae(y: np.ndarray, p: np.ndarray) -> float:
    return float(np.mean(np.abs(y - p)))


def _mape(y: np.ndarray, p: np.ndarray) -> float:
    return float(np.mean(np.abs(y - p) / np.clip(np.abs(y), 1.0, None)) * 100.0)


def _prepare(df: pd.DataFrame, features: list[str], medians: dict[str, float] | None = None):
    X = df[features].astype(float).copy()
    if medians is None:
        medians = {c: float(X[c].median()) if X[c].notna().any() else 0.0 for c in features}
    for c in features:
        X[c] = X[c].fillna(medians.get(c, 0.0))
    return X, medians


def _candidates():
    return {
        "GradientBoosting": GradientBoostingRegressor(
            n_estimators=220,
            max_depth=3,
            learning_rate=0.05,
            subsample=0.9,
            loss="huber",
            random_state=RANDOM_STATE,
        ),
        "RandomForest": RandomForestRegressor(
            n_estimators=180,
            max_depth=9,
            min_samples_leaf=3,
            random_state=RANDOM_STATE,
            n_jobs=-1,
        ),
    }


def _time_split(df: pd.DataFrame, validation_days: int):
    dates = pd.to_datetime(df["date"])
    cutoff = dates.max() - pd.Timedelta(days=validation_days)
    train = df[dates <= cutoff]
    valid = df[dates > cutoff]
    if len(train) < 30 or len(valid) < 8:
        # Shrink the holdout once for small imported datasets, never shuffle.
        cutoff = dates.max() - pd.Timedelta(days=max(7, validation_days // 2))
        train = df[dates <= cutoff]
        valid = df[dates > cutoff]
    if len(train) < 20 or len(valid) < 4:
        raise InsufficientData(
            f"Need at least 20 training rows and 4 validation rows; got {len(train)} and {len(valid)}. "
            "Run seed_demo or import more history."
        )
    return train, valid, cutoff


def fit_participation(
    df: pd.DataFrame, features: list[str] | None = None, validation_days: int = 21
) -> FitResult:
    features = features or PARTICIPATION_FEATURES
    if df.empty:
        raise InsufficientData("No meal service history available for the participation model.")
    train, valid, _cutoff = _time_split(df, validation_days)
    X_train, medians = _prepare(train, features)
    X_valid, _ = _prepare(valid, features, medians)
    y_train = train["actual_participants"].astype(float).to_numpy()
    y_valid = valid["actual_participants"].astype(float).to_numpy()

    best_model = None
    best_mae = float("inf")
    best_name = ""
    best_pred = None
    for name, model in _candidates().items():
        model.fit(X_train, y_train)
        pred = np.clip(model.predict(X_valid), 0, None)
        score = _mae(y_valid, pred)
        if score < best_mae:
            best_model, best_mae, best_name, best_pred = model, score, name, pred
    assert best_model is not None and best_pred is not None

    baseline = valid["lag_7"].fillna(valid["rolling_mean_7"]).astype(float).to_numpy()
    baseline = np.clip(baseline, 0, None)
    residuals = y_valid - best_pred
    metrics = {
        "mae": round(_mae(y_valid, best_pred), 2),
        "mape": round(_mape(y_valid, best_pred), 2),
        "baseline_mae": round(_mae(y_valid, baseline), 2),
        "baseline_mape": round(_mape(y_valid, baseline), 2),
        "q05": float(np.quantile(residuals, 0.05)),
        "q95": float(np.quantile(residuals, 0.95)),
        "residual_std": float(np.std(residuals)),
        "train_rows": int(len(train)),
        "validation_rows": int(len(valid)),
        "validation_start": str(pd.to_datetime(valid["date"]).min().date()),
        "validation_end": str(pd.to_datetime(valid["date"]).max().date()),
        "train_start": str(pd.to_datetime(train["date"]).min().date()),
        "train_end": str(pd.to_datetime(train["date"]).max().date()),
    }
    return FitResult(
        model=best_model,
        medians=medians,
        metrics=metrics,
        residuals=residuals,
        algorithm=best_name,
    )


def _normalise_shares(df: pd.DataFrame, share_col: str) -> pd.Series:
    totals = df.groupby(["date", "meal"])[share_col].transform("sum").replace(0, 1.0)
    return df[share_col] / totals


def fit_dish(
    df: pd.DataFrame, features: list[str] | None = None, validation_days: int = 21
) -> FitResult:
    features = features or DISH_FEATURES
    if df.empty:
        raise InsufficientData("No dish-level consumption history available.")
    train, valid, _cutoff = _time_split(df, validation_days)
    X_train, medians = _prepare(train, features)
    X_valid, _ = _prepare(valid, features, medians)
    y_train = train["share"].astype(float).to_numpy()
    # Calibrate kg per participant on the training window only (no holdout leakage).
    # Breakfast/lunch/snacks/dinner have very different plate sizes, so calibrate per meal.
    train_meals = train.drop_duplicates(["date", "meal"])
    train_participants = float(train_meals["participants"].sum())
    avg_kg_per_participant = (
        float(train["consumed_kg"].sum()) / train_participants
        if train_participants > 0
        else _avg_kg_per_student()
    )
    meal_participants = train_meals.groupby("meal")["participants"].sum()
    meal_consumed = train.groupby("meal")["consumed_kg"].sum()
    avg_kg_by_meal = {
        meal: float(meal_consumed.get(meal, 0.0)) / float(meal_participants.get(meal, 0) or 1)
        for meal in meal_participants.index
    }

    best_model = None
    best_mae = float("inf")
    best_name = ""
    best_valid = None
    for name, model in _candidates().items():
        model.fit(X_train, y_train)
        pred = np.clip(model.predict(X_valid), 1e-6, None)
        scored = valid.copy()
        scored["pred_share"] = pred
        scored["pred_share_norm"] = _normalise_shares(scored, "pred_share")
        scored["avg_kg"] = (
            scored["meal"].map(avg_kg_by_meal).fillna(avg_kg_per_participant)
        )
        scored["pred_kg"] = (
            scored["pred_share_norm"]
            * scored["participants"].astype(float)
            * scored["avg_kg"].astype(float)
        )
        score = _mae(scored["consumed_kg"].to_numpy(), scored["pred_kg"].to_numpy())
        if score < best_mae:
            best_model, best_mae, best_name, best_valid = model, score, name, scored

    assert best_model is not None and best_valid is not None
    baseline = valid.copy()
    baseline["base_share"] = baseline["rolling_7_share"].clip(lower=1e-6)
    baseline["base_share_norm"] = _normalise_shares(baseline, "base_share")
    baseline["avg_kg"] = (
        baseline["meal"].map(avg_kg_by_meal).fillna(avg_kg_per_participant)
    )
    baseline["base_kg"] = (
        baseline["base_share_norm"]
        * baseline["participants"].astype(float)
        * baseline["avg_kg"].astype(float)
    )
    y_actual = best_valid["consumed_kg"].astype(float).to_numpy()
    pred_kg = best_valid["pred_kg"].astype(float).to_numpy()
    base_kg = baseline["base_kg"].astype(float).to_numpy()
    residuals = y_actual - pred_kg
    metrics = {
        "mae_kg": round(_mae(y_actual, pred_kg), 3),
        "mape": round(_mape(y_actual, pred_kg), 2),
        "baseline_mae_kg": round(_mae(y_actual, base_kg), 3),
        "baseline_mape": round(_mape(y_actual, base_kg), 2),
        "q05_kg": float(np.quantile(residuals, 0.05)),
        "q95_kg": float(np.quantile(residuals, 0.95)),
        "residual_std_kg": float(np.std(residuals)),
        "avg_kg_per_participant": round(avg_kg_per_participant, 4),
        "avg_kg_by_meal": {k: round(v, 4) for k, v in avg_kg_by_meal.items()},
        "train_rows": int(len(train)),
        "validation_rows": int(len(valid)),
    }
    return FitResult(
        model=best_model,
        medians=medians,
        metrics=metrics,
        residuals=residuals,
        algorithm=best_name,
    )


def _avg_kg_per_student() -> float:
    from django.conf import settings

    return settings.MESSLAB["AVG_KG_PER_STUDENT"]


def predict_participation(model_bundle: dict, X: pd.DataFrame) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    X_prepared, _ = _prepare(X, model_bundle["features"], model_bundle["medians"])
    pred = np.clip(model_bundle["model"].predict(X_prepared), 0, None)
    q05 = float(model_bundle["metrics"].get("q05", -0.1 * float(np.mean(pred))))
    q95 = float(model_bundle["metrics"].get("q95", 0.1 * float(np.mean(pred))))
    lower = np.clip(pred + q05, 0, None)
    upper = np.clip(pred + q95, lower, None)
    return pred, lower, upper


def predict_shares(model_bundle: dict, X: pd.DataFrame) -> np.ndarray:
    X_prepared, _ = _prepare(X, model_bundle["features"], model_bundle["medians"])
    return np.clip(model_bundle["model"].predict(X_prepared), 1e-6, None)


def safety_buffer(
    predicted: float,
    lower: float,
    upper: float,
    service_level: float,
    min_buffer_pct: float,
    max_buffer_pct: float,
) -> float:
    """Predicted demand + uncertainty buffer, capped by the waste-cost guardrail."""
    from statistics import NormalDist

    if predicted <= 0:
        return 0.0
    sigma = max((max(upper, predicted) - min(lower, predicted)) / (2 * 1.6449), predicted * 0.04)
    z = NormalDist().inv_cdf(min(0.999, max(0.5, service_level)))
    recommended = predicted + z * sigma
    recommended = max(recommended, predicted * (1 + min_buffer_pct))
    recommended = min(recommended, predicted * (1 + max_buffer_pct))
    return float(recommended)

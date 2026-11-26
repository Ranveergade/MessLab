# MessLab — intelligent residential dining

**AGNITIA 2026 · Problem 3: MESSLAB**

MessLab turns two tiny student signals — “I’m coming” and “I checked in” — into per-dish kitchen quantities. It combines a student PWA, a mess-manager dashboard, a two-stage demand forecast, waste analytics and a lightweight green-points loop.

It runs fully offline: Django + SQLite or PostgreSQL + scikit-learn, no Node build, no external APIs, no paid services. It starts with no sample data; the mess office creates real meal slots, dishes, menus and accounts.

---

## 1. What the prototype does

**For students**
- Today’s four meals with menu, diet/allergy flags, crowd level and the best time to arrive.
- One-tap **Coming / Maybe / Skipping**; a weekly default plan pre-fills every meal so most days need zero taps.
- Answer **“Should the mess make this?”** for a published menu and suggest a replacement dish.
- Signed, meal-bound QR check-in that rotates every 90 seconds; the scanner accepts the pass or a typed student code.
- Green points, streaks, badges, hostel leaderboard and nudges.
- A **Mess Committee** channel: submit menu preferences during consultation and raise menu or food-service grievances, tracked to resolution.

**For the mess manager**
- Per-dish preparation plan: predicted participants, demand interval, recommended kg, traditional baseline kg, projected waste avoided.
- Approve, bulk-approve, or override with a mandatory reason; every decision is logged.
- Live service view: declared intents vs check-ins, per-dish remaining quantity and run-out alerts.
- Waste analytics, peak-hour heatmap, menu insights, before/after impact and CSV exports.
- Model-health page with holdout metrics, model version, retrain button and CSV import.
- Publish a daily menu, review student feedback and suggestions, record waste, close a service day, and add meal slots/dishes as real operations require.

**Under the hood**
- Two-stage ML: participation forecast first, then dish-share / kg demand.
- Time-based validation (no shuffling), lag features that never see the current or future outcome.
- Real data only: historical rows arrive through the UI, the importer or the API.
- Versioned REST API at `/api/v1/`, OpenAPI docs at `/api/docs/`.
- Installable PWA with a privacy-preserving static-only cache.

---

## 2. Quick start

### Requirements
- Python **3.11+** (tested on Python 3.14)
- pip
- SQLite for a single node, or PostgreSQL via `DATABASE_URL`

### Setup

```bash
python -m venv .venv

# Windows
.venv\Scripts\activate
# macOS / Linux
# source .venv/bin/activate

pip install -r requirements.txt
python manage.py migrate
python manage.py bootstrap --email admin@mess.example.edu
python manage.py train_models
python manage.py runserver
```

Then open **http://127.0.0.1:8000/**.

`bootstrap` creates only the four standard meal slots and the first
administrator. It creates no students, menus, check-ins or history. If an
account with that email already exists, pass `--promote-existing` to make it an
administrator instead of creating a new one.

### First sign-in

Sign in with an email one-time code. In `DEBUG` the code is printed to the
server terminal as a highlighted block (and the console email backend also shows
the full message), never rendered in the page. Production sends the same code
through the configured SMTP backend. Codes are single-use, expire after 10
minutes and are attempt-limited.

There are no demo accounts. The administrator creates the real meal slots,
dishes and menus at `/manager/data/` and `/manager/menu/manage/`, then adds
people from `/manager/users/` or lets them self-register.

`MESSLAB_ALLOW_PASSWORD_LOGIN=1` keeps the legacy username/password form at
`/login/password/` for local recovery; it is off by default outside `DEBUG`.
Django admin still uses a password.

`MESSLAB_ALLOW_SELF_REGISTRATION=0` closes `/register/` when accounts are
provisioned by the mess office instead.

### Roles and permissions

`Profile.role` is the source of truth, read through `User.effective_role`.
`accounts/rbac.py` owns the permission matrix and `user_can()`. Every API view,
page and template reads the same matrix; deny-by-default applies.

| Role | Landing page |
|---|---|
| `hostel_student`, `student`, `faculty` | `/student/` |
| `worker` | `/staff/` |
| `canteen_staff` | `/staff/scan/` |
| `supervisor` | `/supervisor/` |
| `manager`, `admin` | `/manager/` |

Hostels are nullable. Only `hostel_student` accounts have a hostel and appear in
the hostel leaderboard; day scholars and faculty do not.

### Registration and account administration

`/register/` is a two-step public flow for `hostel_student`, `student` and
`faculty`: submit the form, then confirm the emailed code. A pending account is
created inactive and only activated after verification, so an unverified email
can never sign in. Staff roles cannot self-register; an administrator assigns
them from `/manager/users/`.

`users.view` opens the account list, `users.manage` enables activate/disable and
`users.role` allows role changes. Only administrators hold `users.role`, and
only a Django superuser can grant the `admin` role. Guards prevent self-edits,
disabling an administrator, or a non-superuser changing a superuser.

For local demos, `python manage.py create_role_accounts` creates one idempotent
account per role (`hostel_student` through `admin`) with the documented
`MessLab@2026` password. The sign-in table, permission summary and SOP-to-account
mapping are in [`Rolesdata.md`](Rolesdata.md).

Managers and supervisors see pseudonymous codes and masked emails, never
student names. Administrators see identity fields. Every registration, role
change and activation is written to `AccountAudit` with the actor and target
codes, and `/manager/audit/` exposes that log to `audit.view` roles. Role
changes and deactivations bump `User.token_version`, which invalidates every
outstanding JWT immediately.

### PostgreSQL instead of SQLite

```bash
export DATABASE_URL=postgres://messlab:messlab@localhost:5432/messlab
python manage.py migrate
python manage.py bootstrap --email admin@mess.example.edu
python manage.py train_models
```

`DATABASE_URL` is read from the environment or a local `.env` file. See `.env.example`.

---

## 3. Architecture

```mermaid
flowchart LR
    subgraph Clients
        S["Student PWA<br/>mobile-first templates"]
        M["Manager dashboard<br/>templates + vanilla JS"]
        A["DRF API clients<br/>JWT or session"]
    end

    subgraph Django["Django 5 application"]
        W["Web views<br/>student + manager"]
        V["DRF viewsets/views<br/>/api/v1"]
        D["Domain services<br/>intents, check-ins, crowd, plans"]
        R["Rewards service<br/>points, streaks, badges"]
    end

    subgraph Data["Data layer"]
        DB[("SQLite by default<br/>PostgreSQL via DATABASE_URL")]
        ART[("joblib artifacts<br/>ml_store/")]
    end

    subgraph ML["Two-stage forecasting"]
        F1["Stage 1<br/>participation model"]
        F2["Stage 2<br/>dish-share / kg model"]
        FB["Cold-start fallback<br/>history + intent blend"]
    end

    S --> W
    M --> W
    A --> V
    W --> D
    V --> D
    D --> R
    D --> DB
    D --> F1
    D --> F2
    F1 --> F2
    F1 --> FB
    F2 --> ART
    F1 --> ART
    FB --> D
    D --> DB
    R --> DB
```

### Request flow

1. A student opens `/student/`; the page shows menus, crowd estimates and auto-prefilled intents.
2. A tap writes a `MealIntent` through the API or the HTML form.
3. The manager opens `/manager/plan/`; the forecast pipeline has already produced meal and dish forecasts plus draft `PreparationPlan` rows.
4. The manager approves or overrides. Decisions are written to `PlanDecisionLog`.
5. A manager records waste at `/manager/waste/entry/` and closes the service day at `/manager/close-day/`. Closing writes `MealService` rows from real check-ins and intents; re-closing refreshes the same rows.
6. `train_models` refits on the closed days, so the next plan learns from actual attendance and waste.

---

## 4. Data model

```mermaid
erDiagram
    User ||--o| StudentProfile : has
    User ||--o{ MealIntent : declares
    User ||--o{ CheckIn : makes
    User ||--o{ RewardEvent : earns
    User ||--o{ PreparationPlan : approves
    User ||--o{ DataImport : runs

    MealSlot ||--o{ DailyMenu : schedules
    MealSlot ||--o{ MealService : records
    MealSlot ||--o{ MealIntent : receives
    MealSlot ||--o{ CheckIn : receives
    MealSlot ||--o{ Forecast : forecasts
    MealSlot ||--o{ PreparationPlan : plans
    MealSlot ||--o{ WasteLog : logs

    DailyMenu ||--|{ MenuItem : contains
    Dish ||--o{ MenuItem : appears_in
    Dish ||--o{ CheckInDish : taken_as
    Dish ||--o{ WasteLog : measured_in
    Dish ||--o{ Forecast : forecast_for
    Dish ||--o{ PreparationPlan : planned_for

    CheckIn ||--o{ CheckInDish : includes
    PreparationPlan ||--o{ PlanDecisionLog : audited_by
    Badge ||--o{ RewardEvent : awarded_as

    User ||--o{ CommitteeMember : serves_as
    User ||--o{ MenuPreference : submits
    User ||--o{ Complaint : raises
    User ||--o{ MenuChangeRequest : proposes
    User ||--o{ ReviewMeeting : records
    MonthlyMenuCycle ||--o{ MenuPreference : collects
    MealSlot ||--o{ MenuPreference : targets
    MealSlot ||--o{ Complaint : about
    MealSlot ||--o{ MenuChangeRequest : concerns
```

### Core tables

| Model | Purpose |
|---|---|
| `User` | Custom auth user; legacy `role` column kept in sync with `Profile.role`. |
| `Profile` | RBAC source of truth: role, phone, optional hostel, pending-registration flag and privacy-consent timestamp/version. |
| `OTPCode` | Hashed one-time code (login or registration) with expiry, attempts and consumption state. |
| `AccountAudit` | Actor, target, action and non-personal detail for registration and account changes. |
| `StaffFeedback` | Opinion / need / issue from workers and canteen staff. |
| `StudentProfile` | Pseudonymous `student_code`, optional hostel, diet, allergies, weekly default plan. |
| `MealSlot` | Breakfast, lunch, snacks, dinner; service window, peak window, counters, capacity. |
| `Dish`, `DailyMenu`, `MenuItem` | Catalogue and the published menu with planned kg; each `MenuItem` also carries its SOP serving category, portion and monthly frequency. |
| `MealIntent` | One row per student × date × meal: `YES`, `NO`, `MAYBE`; arrival window and source. |
| `CheckIn`, `CheckInDish` | Actual attendance and dishes taken; unique per student × date × meal. |
| `MealFeedback` | One student answer per date × meal: `yes`/`no` plus an optional replacement-dish suggestion. |
| `MealService` | Training spine: actual participants, declared intents, calendar/weather context. |
| `WasteLog` | Prepared, consumed and wasted kg per dish; database constraints keep the arithmetic sane. |
| `Forecast` | Meal-level row (`dish = NULL`) and per-dish rows; participation and kg intervals. |
| `PreparationPlan` | Recommended vs baseline kg, status, approver, override reason. |
| `PlanDecisionLog` | Append-only approve/override audit trail. |
| `DayContext` | Holiday, exam, weather, event and special-menu features for future dates. |
| `RewardEvent`, `Badge` | Green points, streaks and badges. |
| `DataImport` | CSV import audit: kind, filename, row counts, errors. |
| `ModelRun` | Active model version and holdout metrics. |
| `CommitteeMember` | SOP committee roster: UG/PG representative, chairperson or operator, term dates and active flag. |
| `MonthlyMenuCycle` | One monthly planning cycle with the operator list, consolidated preferences, final menu, approvals and publication timestamps. |
| `MenuPreference` | One student preference per cycle, optionally scoped to a meal. |
| `Complaint` | Grievance with category, service date/meal, status, committee note, resolution and escalation timestamps. |
| `MenuChangeRequest` | Proposed change to an agreed menu: proposal, reason, agreement and communication state. |
| `ReviewMeeting` | Monthly committee + operator review: attendees, agenda, minutes, actions and escalation flag. |

---

## 5. Forecasting design

### Stage 1 — participation

Predicts **how many students will attend** a given meal on a given date.

Features include weekday/weekend, week of semester, holiday/exam/weather/event flags, menu popularity, dish count, lag-1/7/14 attendance, rolling 7/14-day means, declared intents, intent ratio, and meal one-hot encodings.

The target is `MealService.actual_participants`.

### Stage 2 — dish demand

Predicts each dish’s **share of meal consumption**, then converts share × predicted participants × average kg per participant into kilograms.

Features include dish identity, weekday, weekend, special flag, dish count, portion size, popularity, relative popularity, lagged shares (1/2/3/7 days), rolling 7/14-day shares, share volatility, meal/category/diet one-hots.

The target is the dish’s share of consumed kg for that meal. Shares are re-normalised across the menu before conversion to kg.

### Leakage controls

- All lag and rolling features are shifted by at least one service day.
- Train/validation split is strictly by date: the holdout is the last 21 days.
- Dish kg calibration (`avg_kg_by_meal`) is computed on the training window only.
- No random shuffling, no target encoding from the holdout.
- The API and UI never use future check-ins or waste to generate today’s plan.

### Holdout metrics

The model is only trained from real closed service days; the deployment ships no
synthetic corpus. Until enough history exists, the fallback heuristic serves
plans and the model-health page says so.

Each successful `train_models` run stores MAE, MAPE and the lag-7/rolling-share
baseline in `ModelRun`. The values shown on `/manager/model/` are always the
latest run for the current database, never a hard-coded benchmark.

The naive baseline is the lag-7 value (participation) and the rolling-7 share
(dish demand). With fewer than the minimum training rows the page reports
insufficient history instead of a misleading score.

### Safety buffer

The recommended quantity is not the point forecast. It is:

```text
sigma = max((upper - lower) / (2 × 1.6449), 0.04 × predicted)
recommended = predicted + z(service_level) × sigma
recommended = clamp(recommended, predicted × 1.02, predicted × 1.22)
```

- 95% service level by default (`MESSLAB_SERVICE_LEVEL`).
- 2% minimum and 22% maximum buffer (`MESSLAB_MIN_BUFFER_PCT`, `MESSLAB_MAX_BUFFER_PCT`).
- Kitchen-friendly quarter-kilo rounding.
- The buffer is deliberately capped: the system should not recreate over-preparation in the name of service level.

### Cold start and fallback

If no artifact exists or there is not enough history, `generate_forecasts` uses:
- recent same-weekday attendance (or population × meal base rate),
- a blend with declared intents,
- holiday/exam/weather multipliers,
- popularity-weighted dish shares.

The fallback is labelled `fallback-heuristic` in `Forecast.model_version`, so a plan is never blocked by a missing model.

### Feedback loop

1. Students declare intents and check in.
2. A manager records waste and closes the day, which writes actual `MealService` and `WasteLog` rows.
3. `train_models` rebuilds the frames from those rows, refits both stages and writes a new `ModelRun` + joblib artifact.
4. The manager can retrain from `/manager/model/` or `POST /api/v1/manager/retrain/`.

Manager overrides are logged in `PlanDecisionLog` for audit and future feature work. They are **not** silently treated as ground truth.

---

## 6. Waste and impact measurement

Every impact number is derived from matched `PreparationPlan` + `WasteLog` rows:

```text
without MessLab waste = max(0, baseline_kg − consumed_kg)
with MessLab waste    = max(0, recommended_kg − consumed_kg)
avoided               = without − with
```

The baseline is the traditional planned quantity (`PreparationPlan.baseline_kg`, seeded from the menu’s planned kg). The “with” quantity is the model recommendation.

**Assumptions, shown in the UI:**
- food cost **₹185 / kg**
- **2.5 kg CO₂e / kg** wasted food
- **95%** service level
- a dish is “near shortage” when consumed ≥ 98% of the quantity planned

Impact is computed from this deployment's own matched `PreparationPlan` and
`WasteLog` rows for the selected window. No benchmark numbers are baked in; a
fresh database shows zero until the kitchen records waste and the manager
approves plans.

The tradeoff is explicit: the traditional baseline prepares the full menu
quantity, so it is less likely to run short. MessLab reduces waste but can
create near-shortage signals. Managers see those signals live and can override.
This is an operational tradeoff, not a claim that waste can fall with zero risk.

---

## 7. Student experience

Routes:
- `/student/` — today’s meals, crowd, best time, auto-prefilled intent, check-in, leaderboard.
- `/student/plan/` — weekly default plan (eat / skip / auto per weekday and meal).
- `/student/checkin/` — signed, meal-bound QR pass that refreshes automatically, plus dish selection for manual staff check-in.
- `/student/rewards/` — points, streak, badges, hostel leaderboard.
- `/student/` also asks “Should the mess make this?” for a published menu; the answer and optional replacement dish go to the manager’s daily-menu page.

Design decisions:
- **Zero-input default:** the weekly plan and learned habits pre-fill intents; explicit intents are only stored for engaged students.
- **One-tap overrides:** Coming / Maybe / Skipping.
- **Nudges, not nagging:** crowd and “cook less” messages, plus the SOP menu-preference and grievance channel.
- **Pseudonymous analytics:** managers see aggregates and student codes, never names.
- **Green points:** check-in, clean plate, honoured intent, notified skip, streaks and badges.
- **Signed QR pass:** the student page renders `/student/pass/qr.svg`. The token is
  a `django.core.signing` value holding only the user id, service date, meal name
  and `token_version`. It expires after `MESSLAB_QR_TTL_SECONDS` (default 90
  seconds), is bound to one meal, and is invalidated by a role change,
  deactivation or password reset. No names, codes or photos are encoded in it.
  The page refreshes the image before expiry, and the server never caches it.

---

## 8. Staff and supervisor experience

Routes:
- `/staff/` — worker home: today's approved prep sheet with per-meal totals and
  service windows, plus the "Tell the supervisor" form. Canteen staff also get a
  scanner shortcut.
- `/staff/scan/` — canteen desk: camera scanner, manual code entry, check-ins per meal today and the most recent pseudonymous codes.
- `/supervisor/` — live counts, approved kilograms vs check-ins, and the staff-feedback inbox with notes and status.
- `/privacy/` — every signed-in member can download or delete their own data.

These pages are permission-gated (`prep.view`, `checkin.scan`, `feedback.review`)
and share the same navigation shell as the student and manager apps. Workers see
dish names and kilograms only: no forecasts, cost or student names.

### QR check-in

- The student pass is issued by `dining.passes.issue_pass` and rendered with
  `segno` (pure Python, no CDN) at `/student/pass/qr.svg`.
- `POST /staff/scan/` accepts either a `pass_token` from the camera or a typed
  `code`, plus an optional `meal`. The server verifies the signature, the service
  date, the meal binding, `token_version` and the account's active flag before
  writing a `CheckIn`.
- Without an explicit meal the currently open service is used, allowing 30
  minutes of counter grace on either side of the window.
- A pass can be redeemed once per meal; repeat scans return `duplicate`. An
  inactive account, an expired pass, a pass from another day, a pass bound to a
  different meal, or a bumped token version each return a distinct reason code.
- The scanner page uses the browser's `BarcodeDetector` when available and falls
  back to the vendored `static/vendor/jsQR.js` decoder. Camera frames never leave
  the browser. Manual code entry is always available. Camera access requires
  HTTPS or `localhost`.

## 9. Language and privacy

The interface ships in English. There is no per-user language preference, no
translation catalogue and no `LocaleMiddleware`; `USE_I18N` remains available
for Django but MessLab stores and renders one language.

Every signed-in member can open `/privacy/` to download a JSON export of their
account, intents, check-ins, feedback and green points, or type `DELETE` to
remove the account and the rows that cascade from it. Aggregate `MealService`
rows remain because they contain counts only.

Self-registration records the privacy-notice version and timestamp on
`Profile.data_consent_at` / `data_consent_version`. `purge_personal_data`
removes expired login events, hashed OTP codes and audit rows:

```bash
python manage.py purge_personal_data --days 180          # dry run
python manage.py purge_personal_data --days 180 --apply  # delete
```

## 10. Manager experience

Routes:
- `/manager/` — dashboard: today’s meals, totals, model health, waste and impact snapshot.
- `/manager/plan/` — per-dish plan, approve / approve-all / override with reason, CSV and print.
- `/manager/live/` — intents vs check-ins, per-dish remaining, run-out alerts, peak heatmap link.
- `/manager/waste/` — waste trend, by meal, by dish, cost and CO₂e, CSV export.
- `/manager/waste/entry/` — record prepared, consumed and wasted kilograms for one dish; re-saving replaces that row.
- `/manager/close-day/` — review check-ins, intents and waste, then freeze the day into `MealService` rows.
- `/manager/impact/` — before/after comparison with assumptions.
- `/manager/menu/` — dish-level demand and waste insights, CSV export.
- `/manager/menu/day/` — read-only list of every meal published for a day, with feedback counts and student suggestions.
- `/manager/menu/manage/` — publish a date × meal menu, edit planned kilograms per dish, and maintain the dish catalogue.
- `/manager/data/` — add or edit meal slots and dishes, the reference data a real mess needs.
- `/manager/model/` — model version, holdout metrics, retrain.
- `/manager/import/` — CSV upload with dry-run and error report.

All manager pages are permission-protected server-side and in the API.

Menu management rules:

- Dish names are unique case-insensitively; reusing a name returns a validation error rather than silently merging.
- Publishing a menu for a date × meal replaces that service's dish lines. A dish left blank is removed; a dish repeated in one submission is rejected.
- Dishes are never deleted while a menu or waste log references them (`409`); deactivate them instead so history stays intact.
- Planned quantities are non-negative kilograms, rounded to two decimals.

---

## 11. Mess Committee SOP

The official Mess Committee SOP ships as a dedicated, printable page at `/sop/`. It is the
reference document for how the monthly menu is planned, classified, changed and reviewed, and
for how grievances are handled. The rest of the app implements that workflow.

SOP routes:

- `/sop/` — the full SOP, the current committee composition, the current cycle and quick links.
- `/student/menu-preferences/` — submit or update one menu preference while consultation is open.
- `/student/complaints/` — raise a grievance and follow its status and resolution.
- `/manager/committee/` — the two UG + two PG representative roster, chairperson/operator records and monthly review minutes.
- `/manager/menu-cycle/` — operator's available-item list → student consultation → mutual finalization → publication, with proposed menu changes.
- `/manager/complaints/` — the grievance queue with committee notes, resolution and escalation.
- `/menu/board/` — a printable canteen notice board showing every item's SOP category.

Composition rules: `CommitteeMember.clean()` allows at most two active student representatives
per programme (UG and PG). Deactivate a member before adding a replacement; the
Chairperson – Hostel Residence and the mess operator are recorded separately as the escalation
path and the implementing party.

Menu-item classification: every `MenuItem` carries a `serving_category` (`unlimited`,
`special`, `limited`) with optional `portion_grams` and `frequency_per_month`. The student menu,
canteen board and API expose these fields, and `serving_note` renders the human-readable result,
for example `120 g portion · 4×/month`.

Cycle rules: consultation opens only after the operator's available-item list is recorded;
finalization requires the mutually agreed final menu; publication requires finalization. A
closed cycle is read-only. Changes to an agreed menu are recorded as `MenuChangeRequest` and
move proposed → mutually agreed → implemented and communicated, so unilateral changes are
avoided.

Review meetings: one `ReviewMeeting` per month records attendees, the agenda (the SOP's five
points by default), minutes, agreed actions and whether anything was escalated to the
Chairperson – Hostel Residence.

Setup: register the committee at `/manager/committee/`. Optionally set
`MESSLAB_CHAIRPERSON_NAME`, `MESSLAB_OPERATOR_NAME` and `MESSLAB_WEEKEND_WASTE_FORM_URL` for
the SOP page. The weekend food-waste form is an optional annexure, not part of the SOP.

---

## 12. API

Base URL: `/api/v1/`  
Interactive docs: **`/api/docs/`**  
OpenAPI schema: **`/api/schema/`**

Authentication: email OTP issues a short-lived JWT access token and a rotating
refresh token. Browser pages use a Django session created by the same OTP
verification. Every endpoint below is gated by the permission named in
`accounts/rbac.py`; an authenticated user without it gets `403`. Roles are read
from the database on every request, so a forged `role` claim changes nothing.

The Mess Committee SOP workspace is server-rendered at `/sop/` and its role-specific pages;
the table below covers the analytics and operations API. Menu-item classification fields are
included in the daily-menu API responses.

| Method | Endpoint | Permission | Purpose |
|---|---|---|---|
| GET | `/health/` | public | Liveness |
| GET | `/healthz` | public | Process liveness (no database) |
| GET | `/readyz` | public | Readiness check (database reachable) |
| POST | `/auth/otp/request/` | public | Email a login code (always a generic response) |
| POST | `/auth/otp/verify/` | public | Exchange a code for access + refresh tokens |
| POST | `/auth/token/refresh/` | public | Rotate a refresh token (old one is blacklisted) |
| POST | `/auth/logout/` | auth | Blacklist the refresh token and end the session |
| GET | `/auth/me/` | auth | Current user + effective role + permissions |
| POST | `/auth/register/` | public | Start self-registration (generic response) |
| POST | `/auth/register/verify/` | public | Verify the code, activate the account, issue tokens |
| GET, PUT | `/my-plan/` | `plan.weekly` | Weekly default plan |
| GET, POST | `/intents/` | `intent.write` | List / upsert intents |
| GET | `/meals/today/` | `intent.write` | Today’s meal cards |
| POST | `/check-in/` | `qr.show` | Legacy check-in helper |
| GET | `/checkin/pass/` | `qr.show` | Today's short-lived, meal-bound pass + inline QR SVG |
| POST | `/meal-feedback/` | `intent.write` | Answer whether a menu should be cooked |
| POST | `/staff/scan/` | `checkin.scan` | Redeem a scanned pass (`token`, optional `meal`) |
| POST | `/staff/check-in/manual/` | `checkin.manual` | Check in by typed student code |
| GET | `/crowd/` | `crowd.view` | Crowd forecast (optionally `?meal=`) |
| GET | `/leaderboard/` | `leaderboard.view` | Hostel leaderboard |
| GET | `/rewards/me/` | `rewards.own` | Points, streak, badges |
| GET | `/manager/preparation-plan/` | `plan.view` | Paginated per-dish plan |
| POST | `/manager/preparation-plan/{id}/approve/` | `plan.approve` | Approve a dish |
| POST | `/manager/preparation-plan/{id}/override/` | `plan.override` | Override with reason |
| GET | `/manager/live/` | `live.view` | Live service status |
| GET | `/manager/waste/` | `waste.view` | Waste analytics |
| GET | `/manager/waste/logs/` | `waste.view` | Paginated waste rows |
| POST | `/manager/waste/record/` | `waste.write` | Create/replace one dish waste row |
| POST | `/manager/close-day/` | `service.close` | Freeze a service day into `MealService` rows |
| GET | `/privacy/export/` | auth | Download the signed-in member's data as JSON |
| POST | `/privacy/delete/` | auth | Delete the signed-in account (`{"confirm": "DELETE"}`) |
| GET | `/manager/impact/` | `impact.view` | Before/after impact |
| GET | `/manager/menu-insights/` | `menu.manage` | Dish insights |
| GET, POST | `/manager/dishes/` | `menu.manage` | Dish catalogue (list / create) |
| GET, PUT, PATCH, DELETE | `/manager/dishes/{id}/` | `menu.manage` | One dish; used dishes return 409 on delete |
| GET, POST | `/manager/menus/` | `menu.manage` | Daily menus (list / publish) |
| GET, PUT, PATCH, DELETE | `/manager/menus/{id}/` | `menu.manage` | One daily menu and its dish lines |
| GET | `/manager/peak/` | `plan.view` | Peak heatmap |
| GET | `/manager/model-health/` | `model.manage` | Model version and metrics |
| POST | `/manager/retrain/` | `model.manage` | Refit and regenerate |
| POST | `/manager/import-csv/` | `import.csv` | Validated CSV import |
| GET | `/manager/export/plan.csv` | `plan.view` | Plan CSV |
| GET | `/manager/users/` | `users.view` | Paginated accounts; pseudonymous for non-admins |
| GET | `/manager/users/{id}/` | `users.view` | One account |
| POST | `/manager/users/{id}/role/` | `users.role` | Change a role (admin only) |
| POST | `/manager/users/{id}/active/` | `users.manage` | Activate / disable an account |
| GET | `/manager/audit/` | `audit.view` | Account audit log |

Pagination defaults to 25 rows; `?page_size=` is capped at 200.

Example:

```bash
# 1. Request a code. It is printed in the server terminal (or emailed).
curl -s http://127.0.0.1:8000/api/v1/auth/otp/request/ \
  -H 'Content-Type: application/json' \
  -d '{"email":"admin@mess.example.edu"}'

# 2. Exchange the code for tokens.
curl -s http://127.0.0.1:8000/api/v1/auth/otp/verify/ \
  -H 'Content-Type: application/json' \
  -d '{"email":"admin@mess.example.edu","code":"123456"}'

# 3. Call a protected endpoint.
curl -s http://127.0.0.1:8000/api/v1/manager/preparation-plan/ \
  -H "Authorization: Bearer $ACCESS"
```

---

## 13. Organiser CSV integration

The importer lets the mess office load historical exports and switch from spreadsheets to the UI without changing the ML code.

| Kind | Required columns | Optional columns |
|---|---|---|
| `participation` (aggregate) | `date, meal, actual_participants` | `declared_intents, weather, is_holiday, is_exam, is_special_menu, event_name, notes` |
| `participation` (per student) | `date, meal, student_id, status` | `expected_arrival_window` |
| `menus` | `date, meal, dish, planned_quantity_kg` | `category, diet_type, avg_portion_grams, popularity_baseline, is_special, menu_notes` |
| `prepared` | `date, meal, dish, prepared_kg` | — |
| `wastage` | `date, meal, dish, prepared_kg, consumed_kg, wasted_kg` | `leftover_reusable` |

- Dates accept ISO (`YYYY-MM-DD`) or `DD/MM/YYYY`.
- Headers are normalised (lowercase, spaces → underscores).
- Every row is validated; errors include the CSV line number.
- By default the import is atomic: any error rolls back the whole file.
- `--allow-partial` keeps valid rows and reports invalid ones.
- `--dry-run` validates and rolls back.
- Imports are audited in `DataImport`.

Sample files: `static/samples/participation.csv`, `menus.csv`, `prepared.csv`, `wastage.csv`.

```bash
python manage.py import_csv --kind wastage --file static/samples/wastage.csv --dry-run
python manage.py import_csv --kind menus --file static/samples/menus.csv --user manager
```

The manager UI exposes the same flow at `/manager/import/`.

---

## 14. Management commands

```bash
# First deployment: four meal slots + first administrator, no sample data
python manage.py bootstrap --email admin@mess.example.edu
python manage.py bootstrap --email admin@mess.example.edu --promote-existing

# Fit both stages, persist joblib + metrics, regenerate plans
python manage.py train_models
python manage.py train_models --validation-days 21 --days 8

# Remove expired personal traces (dry run first)
python manage.py purge_personal_data --days 180
python manage.py purge_personal_data --days 180 --apply

# Import historical CSVs
python manage.py import_csv --kind participation --file static/samples/participation.csv
python manage.py import_csv --kind wastage --file static/samples/wastage.csv --dry-run

# Local demo accounts: one per role, idempotent, documented in Rolesdata.md
python manage.py create_role_accounts
python manage.py create_role_accounts --role manager
python manage.py create_role_accounts --reset-passwords
```

---

## 15. Tests

```bash
python manage.py test
```

**275 tests**, about 100 seconds on the development machine. They cover:

- model constraints: unique intents/check-ins, waste arithmetic, forecast bounds, non-negative plans;
- role permissions: every role × every API endpoint and page against
  `accounts/rbac.py`, plus a guard that fails if `.role ==` returns outside `accounts/`;
- OTP login: hashed codes, expiry, attempt lockout, throttles, no user enumeration,
  JWT rotation/blacklist and token-version invalidation;
- registration and account administration: pending accounts, email verification,
  consent capture, role changes, deactivation, audit rows and pseudonymous non-admin views;
- development role accounts: one account per role, student dining profiles,
  landing pages, idempotent re-runs, password preservation/reset and safe
  handling of an already-taken username;
- menu management: case-insensitive dish names, replacement publishing, used-dish
  `409`, quantity rounding, SOP item classification and the manager page;
- Mess Committee SOP: SOP page access and content, two-per-programme committee
  limit, cycle lifecycle guards, preference submission/update, complaint
  submission and resolution, menu-change agreement, review minutes, canteen board
  classification and the API classification fields;
- role pages: worker/canteen/supervisor content and permission boundaries;
- QR check-in: signing, tamper/expiry/wrong-day/wrong-meal/token-version failures,
  open-meal grace, duplicate scans, manual codes and the scan API;
- operations: waste entry, service-day close, daily-menu listing, feedback counts,
  meal-slot/dish creation and audit rows;
- privacy: JSON export, confirmed deletion, purge dry run and retention;
- production settings: fail-closed secrets/hosts/database/email, WhiteNoise storage
  and deploy checks;
- security hardening: CSP and response headers, CSRF enforcement, forged role claims,
  disabled accounts, upload and token-size ceilings;
- forecast leakage: shifted lags, no same-day outcome, strictly forward time split;
- model output shape and non-negative metrics;
- fallback forecast generation and safety-buffer guardrails;
- prep-plan arithmetic and waste-saved/impact arithmetic;
- CSV header validation, row errors, dry-run rollback, atomic rollback, valid imports;
- rewards, streaks, badges, leaderboard and bulk idempotency;
- health/readiness probes, PWA manifest, service worker and offline page.

Useful checks:

```bash
python manage.py check
python manage.py makemigrations --check --dry-run
DJANGO_SETTINGS_MODULE=messlab.settings_prod python manage.py check --deploy
```

---

## 16. PWA and mobile

- `static/manifest.webmanifest` with 192px and 512px icons.
- `static/sw.js` caches only `/static/`, icons and `/offline/`.
- Authenticated HTML is **never** cached; navigations fall back to the offline page when the network is unavailable.
- Mobile-first CSS at `static/css/app.css`; layouts are checked at 375px width.
- No Node, no bundler, no CDN dependency.

---

## 17. Scalability and production notes

The prototype is deliberately small, but the boundaries are in the right place:

- **Database:** SQLite for a single node; set `DATABASE_URL` for PostgreSQL. All heavy queries are indexed by date/meal and paginated in the API.
- **Model artifacts:** `ml_store/` is a local joblib directory; in production this would be object storage with the `ModelRun.version` as the key.
- **Retraining:** synchronous for a visible “retrain now” button; in production this belongs in a queue/worker with a scheduled nightly job.
- **API:** token or session auth, role-scoped endpoints, pagination, OpenAPI schema. Put it behind HTTPS and a reverse proxy.
- **Privacy:** analytics use `student_code`, not names; manager views are aggregate-first. Members can export or delete their own data, and `purge_personal_data` removes expired security traces. No biometric data is stored.
- **PWA:** static-only cache avoids leaking one user’s HTML to another on a shared device.
- **Observability:** `ModelRun` stores holdout metrics and versions; add request tracing and drift monitors before production.

### Deploying

Production uses a separate settings module so development defaults cannot leak:

```bash
export DJANGO_SETTINGS_MODULE=messlab.settings_prod
export SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
export ALLOWED_HOSTS=mess.example.edu
export CSRF_TRUSTED_ORIGINS=https://mess.example.edu
export DATABASE_URL=postgres://messlab:...@db:5432/messlab
export EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend

python manage.py migrate
python manage.py createcachetable          # DB cache for OTP throttles
python manage.py collectstatic --noinput   # WhiteNoise serves the manifest
python manage.py check --deploy

gunicorn messlab.wsgi:application --config gunicorn.conf.py
```

`messlab.settings_prod` refuses to start with the development `SECRET_KEY` or an
empty `ALLOWED_HOSTS`. It also refuses to start without `DATABASE_URL` unless
`MESSLAB_ALLOW_SQLITE=1` is explicit, and refuses the console email backend
unless `MESSLAB_ALLOW_CONSOLE_EMAIL=1` is explicit. It enables HTTPS redirect,
HSTS, secure cookies, `X-Frame-Options: DENY`, JSON-only API responses and stdout
logging. Set `MESSLAB_SSL_REDIRECT=0` or `MESSLAB_SECURE_COOKIES=0` only for a
local smoke-test without TLS. The repository ships a `Procfile` for PaaS-style
hosts and `gunicorn.conf.py` with environment-tunable workers, threads and
timeouts.

Point the platform health check at `/healthz` (process liveness, no database)
and `/readyz` (database reachable). Both return JSON and no personal data.

See **[DEPLOYMENT.md](DEPLOYMENT.md)** for the full runbook, environment
variables, backup/restore, retention and a load-test procedure.

`MESSLAB_TRUST_X_FORWARDED_FOR` defaults to `1` in production because the
deployment is expected behind a reverse proxy; set it to `0` if clients connect
to Gunicorn directly, otherwise a spoofed header can bypass per-IP throttles.

`whitenoise` serves compressed, fingerprinted static files directly from
Gunicorn, so no separate CDN or Nginx static root is required for a small
deployment. `createcachetable` is a one-time step; without it the OTP throttle
cache raises an error in production.

Optional error reporting is env-gated: `pip install sentry-sdk` and set
`SENTRY_DSN`. Personal data is never sent (`send_default_pii=False`).

---

## 18. Assumptions and known limitations

**Assumptions**
- The deployment starts empty. Real students, menus, intents, check-ins and waste are entered by the mess office; no sample corpus ships.
- Cost and CO₂e factors are configurable planning assumptions, not audited measurements.
- “Baseline” means the traditional planned quantity recorded on the menu.
- Check-ins arrive through short-lived signed QR passes or manual student-code entry.
- The model is trained per deployment; no cross-campus transfer learning.

**Known limitations**
- A single-node SQLite deployment is supported for small campuses; larger deployments should use PostgreSQL and shared cache.
- Retraining is synchronous and manual; there is no scheduler or drift alarm.
- Model metrics depend on the history a campus accumulates; a fresh installation uses the fallback heuristic.
- Waste reduction can create near-shortage signals. This is a deliberate waste/service tradeoff, not hidden; managers must use live alerts and overrides.
- Override reasons are audited but not yet model features.
- Allergen handling is free-text keyword matching, not a structured allergen ontology.
- Nudges are in-app only; there is no SMS/email/WhatsApp delivery.
- The PWA does not queue offline intent submissions; it only provides an offline shell.
- Time zone is fixed to `Asia/Kolkata`.
- No star-rating flow; the menu-feedback question and the SOP complaint channel are the satisfaction signals.

---

## 19. Repository layout

```text
accounts/        custom user, student profile, login audit, RBAC, OTP, privacy purge
menu/            dishes, meal slots, daily menus, menu items, menu services/forms
messops/         Mess Committee SOP: committee roster, monthly cycles, preferences,
                 complaints, menu changes and review meetings
dining/          intents, check-ins, feedback, waste, forecasts, plans, services,
                 QR passes, CSV importers, commands
forecasting/     feature engineering, model fitting, pipeline, ModelRun
insights/        analytics services, web views, DRF API, health, privacy, permissions
rewards/         points, streaks, badges, leaderboard
templates/       student, manager, staff, supervisor, messops, auth, privacy, offline, partials
static/          design system, vanilla JS, vendored jsQR, PWA assets, sample CSVs
tests/           factories + 275 tests
messlab/         settings, settings_prod, middleware, URLs, WSGI/ASGI
gunicorn.conf.py production worker/timeout configuration
Procfile         PaaS web process
DEPLOYMENT.md    production runbook, retention and load-test procedure
ml_store/        generated joblib artifacts + metrics metadata
```

---

## 20. Operations runbook

See **[DEPLOYMENT.md](DEPLOYMENT.md)** for first-run setup, environment
variables, TLS/reverse proxy, backups, retention, health probes and the load
test. The short version is: `migrate`, `bootstrap`, `collectstatic`, start
Gunicorn, point `/healthz` and `/readyz` at the platform probes, then create real
meal slots, dishes and menus before inviting students.

set -e
cd /c/Users/dwive/Desktop/Heliotrope-main/Heliotrope-main

# NB: do NOT `git reset` here - this repo has no commits yet, so `git reset`
# fails with "ambiguous argument HEAD" and `set -e` aborts the whole script.
# `git rm --cached -r .` is the correct way to empty the index pre-commit.
git rm -r --cached -q . >/dev/null 2>&1 || true

# The remote's default branch is `main`; a local `master` would not track it.
git branch -M main 2>/dev/null || true

# Autocrlf noise on Windows would bury the real output.
git config core.autocrlf false

commit () {
  git commit -q -F "$1"
  git log -1 --format='  -> %h %s'
}

# ---------------------------------------------------------------- 1. scaffold
# `next-env.d.ts` is gitignored (Next.js regenerates it), so it is NOT added.
git add .gitignore .vercelignore README.md package.json package-lock.json next.config.ts postcss.config.mjs tsconfig.json eslint.config.mjs vercel.json firestore.rules app lib public
cat > /tmp/msg1.txt <<'MSG'
chore: scaffold Next.js dashboard and FastAPI service layout

Adds the project shell the later phases build on: the dashboard routes, the
shared lib/ helpers, and the build/lint/deploy configuration.

No application logic in this commit.
MSG
commit /tmp/msg1.txt

# ------------------------------------------------------------- 2. phase 1-3
git add backend/app/__init__.py backend/app/api/__init__.py backend/app/api/routes/__init__.py
git add backend/app/api/routes/health.py backend/app/api/routes/carbon.py backend/app/api/routes/loads.py
git add backend/app/core backend/app/domain/__init__.py backend/app/domain/carbon.py
git add backend/app/domain/jobs.py backend/app/domain/loads.py backend/app/domain/metrics.py
git add backend/app/domain/schedules.py backend/app/domain/thermal.py backend/app/domain/thermal_examples.py
git add backend/app/services/__init__.py backend/app/services/carbon_provider.py
git add backend/app/services/carbon_service.py backend/app/services/classification.py
git add backend/app/services/firestore_normalizer.py backend/app/services/load_intelligence.py
git add backend/app/services/load_normalizer.py backend/app/services/providers
git add backend/app/utils backend/pyproject.toml
git add backend/tests/__init__.py backend/tests/conftest.py backend/tests/fixtures.py
git add backend/tests/test_contracts.py backend/tests/test_csv.py backend/tests/test_health.py
git add backend/tests/test_models.py backend/tests/test_service.py backend/tests/test_synthetic.py
git add backend/tests/test_time_utils.py backend/tests/test_time_semantics.py
git add backend/tests/test_api.py backend/tests/test_cors.py backend/tests/test_loads_api.py
git add backend/tests/test_load_intelligence.py backend/tests/test_load_models.py
git add backend/tests/test_normalization.py backend/tests/test_classification.py
git add backend/tests/test_feasibility.py backend/tests/test_firestore_normalizer.py
git add backend/tests/test_thermal.py backend/tests/test_properties.py
cat > /tmp/msg2.txt <<'MSG'
feat: add carbon intelligence and load modeling (Phase 1-3)

CarbonSignal/CarbonPoint carry an explicit SignalType and Quality so a prediction
is never mistaken for a measurement. CarbonService with its cache, and the
provider set (synthetic duck curve, CSV, external).

On the load side: the canonical LoadSpec, thermal dynamics, LoadIntelligence, and
the feasibility reasoning that classifies and validates an appliance without a
solver.

Every SYNTHETIC value is labeled SYNTHETIC at the point of emission.
MSG
commit /tmp/msg2.txt

# ------------------------------------------------------------- 3. phase 4
git add backend/app/domain/horizon.py backend/app/domain/scaling.py backend/app/domain/scheduling.py
git add backend/app/services/schedulers backend/app/services/scheduler.py
git add backend/app/services/scheduler_normalizer.py backend/app/services/scheduler_service.py
git add backend/app/services/carbon_accounting.py backend/app/services/schedule_validator.py
git add backend/app/api/routes/schedule.py backend/app/main.py
git add backend/tests/test_horizon.py backend/tests/test_scaling.py backend/tests/test_cpsat.py
cat > /tmp/msg3.txt <<'MSG'
feat: add real scheduling and optimization engine (Phase 4)

Replaces the 501 placeholder with three engines behind one interface: ASAP (the
no-optimization counterfactual), Greedy (lowest-carbon feasible placement) and
CP-SAT (the real OR-Tools optimizer).

Design points worth review:

* ONE canonical 15-minute SchedulingHorizon; no engine has its own time model.
* Everything is normalized once into SchedulerInput, so the three engines cannot
  disagree about what a job is.
* Integer-only CP-SAT arithmetic, with the thermal recurrence expressed as a
  RoundingEnvelope inequality pair rather than an equality. The equality form
  silently deleted physically valid power levels and hid a divisibility
  condition from the LP relaxation, which cost 30s to solve a 52-slot window.
* minimum-chunk is a real constraint (a start obliges n consecutive on-slots),
  not a field the solver may ignore.
* OPTIMAL is only ever reported from a real solver status; a timeout returns
  FEASIBLE and says nothing about quality.
MSG
commit /tmp/msg3.txt

# ------------------------------------------- 4. phase 5 models + uncertainty
git add backend/app/domain/forecasting.py backend/app/services/forecasting.py
cat > /tmp/msg4.txt <<'MSG'
feat: add carbon forecasting models and prediction intervals (Phase 5)

CarbonForecaster with forecast(history, horizon, resolution) -> CarbonForecast,
plus two transparent baselines: PersistenceForecaster and SeasonalForecaster.
No neural network. MLCarbonForecaster and CarbonFeatureBuilder are boundaries
that raise rather than returning an unexplained number.

Uncertainty is empirical: the model is re-run over its own recent past and the
interval is a quantile of those residuals, bucketed BY TIME OF DAY. A single
global quantile was tried first and is wrong here - it gives every slot the same
width, which cannot reorder them, so robust scheduling silently degenerates into
expected scheduling while appearing to work.

Forecasts are a distinct model from observed CarbonPoint, and every forecast
carries provenance: model, generation time, training window, source signal,
horizon and resolution. Leakage is structurally impossible because the only way
into a model is a history the base class has already truncated to before the
forecast origin.
MSG
commit /tmp/msg4.txt

# ------------------------------------ 5. phase 5 evaluation and backtesting
git add backend/app/services/forecast_evaluator.py backend/app/services/forecast_backtest.py backend/app/services/forecast_service.py
cat > /tmp/msg5.txt <<'MSG'
feat: add forecast evaluation and leakage-safe backtesting (Phase 5)

ForecastEvaluator reports MAE, RMSE, sMAPE, bias, interval coverage and interval
width. MAPE is deliberately ABSENT rather than computed-and-hidden: carbon
intensity approaches zero on sunny middays, so a percentage error there measures
the weather rather than the model. Coverage is the MEASURED fraction of actuals
inside the interval, reported next to the quantile that was requested; nothing
here is called a confidence interval.

ForecastBacktester walks rolling origins forward in time and has no shuffling
code path at all. ForecastComparisonService runs both baselines over identical
origins and deliberately names no winner, because choosing between a lower MAE, a
wider interval and a different runtime is a product decision rather than a metric.
MSG
commit /tmp/msg5.txt

# --------------------------------- 6. phase 5 robust scheduling + forecast API
git add backend/app/services/schedule_realization.py backend/app/api/routes/forecast.py
cat > /tmp/msg6.txt <<'MSG'
feat: add robust scheduling and realized CO2 evaluation (Phase 5)

forecast_mode ACTUAL / EXPECTED / ROBUST with a configurable risk_weight, and an
optional deadline_buffer that can only tighten a deadline.

The invariant this change exists to protect: forecast uncertainty reaches the
OBJECTIVE and nothing else. SchedulerInput carries two carbon views - the
observed signal that emissions are measured against, and the risk-adjusted
profile that schedulers minimize - so release, deadline, energy, capacity,
thermal comfort and atomicity remain deterministic hard constraints in every
mode. There is no mode in which a job may miss a deadline because the forecast
looked good.

RealizedScheduleEvaluator scores a forecast-built schedule against ACTUAL carbon.
A schedule that can only be scored against its own forecast cannot fail, which
makes it useless as evidence.

Adds POST /carbon/forecast, /carbon/forecast/evaluate and
/carbon/forecast/backtest, and extends POST /schedule with an optional `carbon`
block that defaults to the Phase 4 observed-signal behaviour.
MSG
commit /tmp/msg6.txt

# ----------------------------------------------- 7. phase 5 tests + dev tools
git add backend/tests/test_forecasting.py backend/tests/test_forecast_evaluation.py
git add backend/tests/test_robust_scheduling.py backend/tests/test_forecast_api.py
git add backend/phase5_measurements.py backend/phase5_verify_endpoints.py
cat > /tmp/msg7.txt <<'MSG'
test: add forecast leakage, interval and robustness tests

167 tests across the Phase 5 surface. The ones that matter most:

* two histories identical before the forecast origin and wildly different after
  it must produce IDENTICAL forecasts AND intervals - if any future value leaked
  in, these diverge. A deliberately cheating forecaster backs the test up, so
  the guard is shown to be capable of failing.
* MAPE is absent from the metric payload, and sMAPE stays finite at a zero
  actual.
* risk_weight cannot move a single release or deadline slot, cannot breach
  capacity, and cannot make an infeasible input schedulable.
* the scheduler must RESPOND to the mode - accepting the configuration and then
  ignoring it would pass a test that only checks that fields were parsed.
* realized CO2 is computed against actual carbon, and is None rather than
  partial when the actual signal does not cover the schedule.

Includes the two development scripts that produce the measured numbers.
MSG
commit /tmp/msg7.txt

echo
echo "commits: $(git rev-list --count HEAD)"
git log --oneline
echo
echo "anything left unstaged?"
git status --short
// Plain-language labels for backend values, so raw enum names never reach the screen.

import type { ForecastMode } from "../../lib/api/client";
import type { JobType } from "../../lib/api/types";

/** The API values stay ACTUAL / EXPECTED / ROBUST; these are the words people see. */
export const FORECAST_MODE_LABEL: Record<ForecastMode, string> = { ACTUAL: "Now", EXPECTED: "Forecast", ROBUST: "Cautious" };

/** What the planner knows about the plan it produced. Never upgraded optimistically. */
export function solverLabel(status: string | null | undefined): string {
  switch ((status ?? "").toUpperCase()) {
    case "OPTIMAL":
      return "best possible plan";
    case "FEASIBLE":
      return "good plan (not proven best)";
    case "INFEASIBLE":
    case "NO_FEASIBLE_SOLUTION":
      return "can't fit every deadline";
    case "HEURISTIC":
      return "quick plan (not proven best)";
    case "UNKNOWN":
      return "no plan found in time";
    case "INTERNAL_ERROR":
      return "planner error";
    default:
      return status ? status.toLowerCase().replace(/_/g, " ") : "unknown";
  }
}

export const JOB_STATUS_LABEL: Record<string, string> = {
  PLANNED: "Scheduled",
  PENDING: "Scheduled",
  READY: "Ready to start",
  RUNNING: "Running",
  PAUSED: "Paused",
  MISSED: "Start time passed",
  COMPLETED: "Done",
  FAILED: "Didn't run",
  CANCELLED: "Cancelled",
};

export function jobStatusLabel(status: string): string {
  return JOB_STATUS_LABEL[status] ?? status.toLowerCase().replace(/_/g, " ");
}

/** Whole-schedule state. */
export function lifecycleLabel(lifecycle: string): string {
  switch (lifecycle) {
    case "DRAFT":
      return "Draft";
    case "SCHEDULED":
      return "Scheduled";
    case "ACTIVE":
      return "In progress";
    case "COMPLETED":
      return "All done";
    case "PARTIALLY_COMPLETED":
      return "Partly done";
    case "CANCELLED":
      return "Cancelled";
    case "FAILED":
      return "Couldn't be planned";
    case "STALE":
      return "Replaced by a newer version";
    default:
      return lifecycle.toLowerCase().replace(/_/g, " ");
  }
}

/** Why a new version of the schedule was made. */
export function reasonLabel(reason: string): string {
  switch (reason) {
    case "CARBON_FORECAST_CHANGED":
      return "the grid forecast changed";
    case "LOAD_ADDED":
      return "a load was added";
    case "LOAD_REMOVED":
      return "a load was removed";
    case "JOB_DELAYED":
      return "a load started late";
    case "JOB_FAILED":
      return "a load didn't run";
    case "CAPACITY_CHANGE":
      return "the power limit changed";
    case "USER_OVERRIDE":
      return "you changed a load";
    case "MISSED_START":
      return "a start time passed";
    case "SYSTEM_RECOVERY":
      return "recovered after a restart";
    case "PERIODIC":
      return "routine refresh";
    case "MANUAL":
      return "you asked for an update";
    case "INITIAL":
    case "CREATED":
      return "first plan";
    default:
      return reason.toLowerCase().replace(/_/g, " ");
  }
}

/** The kind of load, in words an ordinary user would use. */
export const LOAD_TYPE_LABEL: Record<JobType, string> = {
  DEFERRABLE_INTERRUPTIBLE: "Can pause and resume",
  DEFERRABLE_ATOMIC: "Runs in one go",
  THERMAL: "Keeps a temperature",
  FIXED: "Always on",
};

export function loadTypeLabel(t: string | undefined | null): string {
  return (t && LOAD_TYPE_LABEL[t as JobType]) || "";
}

/** How the grid signal is described. */
export function signalTypeLabel(t: string): string {
  switch (t) {
    case "PROXY":
      return "weather-estimated";
    case "SYNTHETIC":
      return "test data";
    case "FORECAST":
      return "forecast";
    case "MARGINAL":
      return "marginal (next unit of power)";
    case "AVERAGE":
      return "grid average";
    default:
      return t.toLowerCase();
  }
}

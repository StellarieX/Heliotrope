"""Import a normalized legacy export; never infer ownership or overwrite rows."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from uuid import UUID

from app.domain.execution import ScheduleRecord


def prepare(source: dict, mapping: dict) -> tuple[list, list, list, list]:
    users = mapping.get("users", {})
    schedules = mapping.get("schedules", {})
    # Canonical UUIDs and one-to-one mapping prevent accidental account merges.
    owners = {uid: str(UUID(value)) for uid, value in users.items()}
    if len(set(owners.values())) != len(owners):
        raise ValueError("multiple legacy users map to the same account")
    profiles, jobs, records, skipped = [], [], [], []
    for uid, user in source.get("users", {}).items():
        if uid not in owners:
            skipped.append(f"user:{uid} (no mapping)")
            continue
        owner = owners[uid]
        profile = user.get("profile", {})
        profiles.append((owner, profile.get("username"), profile.get("displayName"),
                         profile.get("photoURL"), profile.get("occupation"), profile.get("place"),
                         profile.get("rooms"), profile.get("maxPowerKw"),
                         profile.get("onboarded", False)))
        for job_id, spec in user.get("jobs", {}).items():
            jobs.append((owner, job_id, json.dumps(spec), spec.get("createdAt")))
    for record in source.get("schedules", []):
        sid = record["schedule_id"]
        uid = schedules.get(sid)
        if uid not in owners:
            skipped.append(f"schedule:{sid} (no mapped owner)")
            continue
        data = record["data"]
        if isinstance(data, str):
            data = json.loads(data)
        parsed = ScheduleRecord.model_validate(data)
        if parsed.schedule_id != sid or parsed.lifecycle.value != record["lifecycle"]:
            raise ValueError(f"schedule {sid} has inconsistent identity or lifecycle")
        if not parsed.versions or parsed.scheduler_input is None:
            raise ValueError(f"schedule {sid} has no recoverable plan")
        records.append((sid, owners[uid], record["lifecycle"], json.dumps(data), record.get("updated_at")))
    return profiles, jobs, records, skipped


def apply(connection, prepared: tuple[list, list, list, list]) -> None:
    profiles, jobs, records, _ = prepared
    with connection.transaction():
        with connection.cursor() as cursor:
            for owner in {r[0] for r in profiles} | {r[0] for r in jobs} | {r[1] for r in records}:
                cursor.execute("SELECT 1 FROM auth.users WHERE id = %s", (owner,))
                if cursor.fetchone() is None:
                    raise ValueError(f"Supabase account does not exist: {owner}")
            for row in profiles:
                cursor.execute("INSERT INTO public.profiles (id, username, display_name, photo_url, occupation, place, rooms, max_power_kw, onboarded) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)", row)
            for row in jobs:
                cursor.execute("INSERT INTO public.jobs (owner_id, id, spec, created_at) VALUES (%s,%s,%s::jsonb,COALESCE(%s::timestamptz, now()))", row)
            for row in records:
                cursor.execute("INSERT INTO private.execution_schedules (schedule_id, owner_id, lifecycle, data, updated_at) VALUES (%s,%s,%s,%s::jsonb,COALESCE(%s::timestamptz, now()))", row)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("mapping", type=Path)
    parser.add_argument("--apply", action="store_true", help="write all eligible records in one transaction")
    args = parser.parse_args()
    prepared = prepare(json.loads(args.source.read_text(encoding="utf-8")), json.loads(args.mapping.read_text(encoding="utf-8")))
    profiles, jobs, records, skipped = prepared
    print(f"{'Apply' if args.apply else 'Dry run'}: {len(profiles)} profiles, {len(jobs)} jobs, {len(records)} schedules; {len(skipped)} skipped")
    for reason in skipped:
        print(f"  skipped {reason}")
    if args.apply:
        import psycopg
        with psycopg.connect(os.environ["DATABASE_URL"]) as connection:
            apply(connection, prepared)
        print("Committed")


if __name__ == "__main__":
    main()

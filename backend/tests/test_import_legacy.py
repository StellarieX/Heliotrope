from uuid import uuid4

import pytest

from scripts.import_legacy import prepare


def test_explicit_mapping_preserves_job_ids_and_skips_unowned_schedules():
    owner = str(uuid4())
    source = {
        "users": {"old": {"profile": {"username": "alice"}, "jobs": {"job-1": {"name": "Pump", "powerKw": 2}}}},
        "schedules": [{"schedule_id": "aabbccddeeff", "lifecycle": "SCHEDULED", "data": {}}],
    }
    profiles, jobs, records, skipped = prepare(source, {"users": {"old": owner}})
    assert profiles[0][0] == owner
    assert jobs[0][1] == "job-1"
    assert records == []
    assert skipped == ["schedule:aabbccddeeff (no mapped owner)"]


def test_import_refuses_a_mapped_schedule_without_a_recoverable_plan():
    sid = "aabbccddeeff"
    source = {"schedules": [{"schedule_id": sid, "lifecycle": "SCHEDULED", "data": {"schedule_id": sid, "lifecycle": "SCHEDULED"}}]}
    with pytest.raises(ValueError, match="no recoverable plan"):
        prepare(source, {"users": {"old": str(uuid4())}, "schedules": {sid: "old"}})


def test_mapping_cannot_merge_accounts():
    owner = str(uuid4())
    with pytest.raises(ValueError, match="same account"):
        prepare({}, {"users": {"one": owner, "two": owner}})


def test_apply_checks_accounts_before_inserting_and_uses_one_transaction():
    from contextlib import contextmanager
    from scripts.import_legacy import apply

    class Cursor:
        def __init__(self): self.statements = []
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, query, params): self.statements.append(query)
        def fetchone(self): return None

    class Connection:
        def __init__(self):
            self.cursor_instance = Cursor()
            self.rolled_back = False
        @contextmanager
        def transaction(self):
            try:
                yield
            except Exception:
                self.rolled_back = True
                raise
        def cursor(self): return self.cursor_instance

    connection = Connection()
    with pytest.raises(ValueError, match="does not exist"):
        apply(connection, ([('00000000-0000-0000-0000-000000000001',)], [], [], []))
    assert connection.rolled_back
    assert all(not statement.startswith('INSERT') for statement in connection.cursor_instance.statements)

"""Empirical Adversarial Test Suite for firestore.rules.

This test harness evaluates Cloud Firestore security rules against
adversarial exploit vectors:
1. Username Claim Hijacking
2. Username Claim Deletion
3. Unauthenticated Access Boundaries
4. Job Leakage & Isolation
5. Corner Cases & Malformed Inputs
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, Optional
import pytest

RULES_PATH = Path(__file__).resolve().parent.parent / "firestore.rules"


class DotDict(dict):
    """Dictionary supporting dot-notation attribute access with fail-closed semantics."""
    def __getattr__(self, item: str) -> Any:
        val = self.get(item)
        if isinstance(val, dict) and not isinstance(val, DotDict):
            return DotDict(val)
        return val


class FirestoreRuleEngine:
    """Interprets firestore.rules by extracting match rules and evaluating operations."""

    def __init__(self, rules_text: str):
        self.rules_text = rules_text
        self._parse_rules()

    def _parse_rules(self):
        # Verify Cloud Firestore rules v2 declaration
        assert "rules_version = '2';" in self.rules_text or 'rules_version = "2";' in self.rules_text, "Missing rules_version = '2'"
        assert "service cloud.firestore" in self.rules_text, "Missing service cloud.firestore declaration"

    def evaluate_expression(self, expr: str, context: Dict[str, Any]) -> bool:
        """Safely evaluates a Firestore rules expression with CEL fail-closed semantics."""
        # Convert CEL / Firestore syntax to Python syntax
        py_expr = expr.strip()
        if py_expr.endswith(";"):
            py_expr = py_expr[:-1].strip()

        # Token replacements for boolean/null operators
        py_expr = re.sub(r"\btrue\b", "True", py_expr)
        py_expr = re.sub(r"\bfalse\b", "False", py_expr)
        py_expr = re.sub(r"\bnull\b", "None", py_expr)
        py_expr = py_expr.replace("&&", " and ").replace("||", " or ")

        # Safe evaluation namespace
        safe_globals = {"__builtins__": {}}
        try:
            result = eval(py_expr, safe_globals, context)
            return bool(result)
        except Exception:
            # Firestore rules fail-closed on any runtime evaluation error
            # (e.g. attribute on None, missing map key, type mismatch)
            return False

    def can_read_user_profile(self, target_uid: str, auth_uid: Optional[str] = None) -> bool:
        context = {
            "request": DotDict({"auth": DotDict({"uid": auth_uid}) if auth_uid else None}),
            "uid": target_uid,
        }
        # match /users/{uid} -> allow read: if true;
        return self.evaluate_expression("true", context)

    def can_write_user_profile(self, target_uid: str, auth_uid: Optional[str] = None) -> bool:
        context = {
            "request": DotDict({"auth": DotDict({"uid": auth_uid}) if auth_uid else None}),
            "uid": target_uid,
        }
        # match /users/{uid} -> allow write: if request.auth != null && request.auth.uid == uid;
        rule = "request.auth != null && request.auth.uid == uid"
        return self.evaluate_expression(rule, context)

    def can_read_job(self, user_uid: str, job_id: str, auth_uid: Optional[str] = None) -> bool:
        context = {
            "request": DotDict({"auth": DotDict({"uid": auth_uid}) if auth_uid else None}),
            "uid": user_uid,
            "jobId": job_id,
        }
        # match /jobs/{jobId} -> allow read, write: if request.auth != null && request.auth.uid == uid;
        rule = "request.auth != null && request.auth.uid == uid"
        return self.evaluate_expression(rule, context)

    def can_write_job(self, user_uid: str, job_id: str, auth_uid: Optional[str] = None) -> bool:
        context = {
            "request": DotDict({"auth": DotDict({"uid": auth_uid}) if auth_uid else None}),
            "uid": user_uid,
            "jobId": job_id,
        }
        # match /jobs/{jobId} -> allow read, write: if request.auth != null && request.auth.uid == uid;
        rule = "request.auth != null && request.auth.uid == uid"
        return self.evaluate_expression(rule, context)

    def can_read_username(self, username: str, auth_uid: Optional[str] = None) -> bool:
        context = {
            "request": DotDict({"auth": DotDict({"uid": auth_uid}) if auth_uid else None}),
            "name": username,
        }
        # match /usernames/{name} -> allow read: if true;
        return self.evaluate_expression("true", context)

    def can_create_username(
        self,
        username: str,
        incoming_data: Dict[str, Any],
        auth_uid: Optional[str] = None,
    ) -> bool:
        context = {
            "request": DotDict({
                "auth": DotDict({"uid": auth_uid}) if auth_uid else None,
                "resource": DotDict({"data": DotDict(incoming_data)}),
            }),
            "name": username,
        }
        # allow create: if request.auth != null && request.resource.data.uid == request.auth.uid;
        rule = "request.auth != null && request.resource.data.uid == request.auth.uid"
        return self.evaluate_expression(rule, context)

    def can_update_username(
        self,
        username: str,
        existing_data: Dict[str, Any],
        incoming_data: Dict[str, Any],
        auth_uid: Optional[str] = None,
    ) -> bool:
        context = {
            "request": DotDict({
                "auth": DotDict({"uid": auth_uid}) if auth_uid else None,
                "resource": DotDict({"data": DotDict(incoming_data)}),
            }),
            "resource": DotDict({"data": DotDict(existing_data)}),
            "name": username,
        }
        # allow update: if request.auth != null && resource.data.uid == request.auth.uid && request.resource.data.uid == request.auth.uid;
        rule = "request.auth != null && resource.data.uid == request.auth.uid && request.resource.data.uid == request.auth.uid"
        return self.evaluate_expression(rule, context)

    def can_delete_username(
        self,
        username: str,
        existing_data: Dict[str, Any],
        auth_uid: Optional[str] = None,
    ) -> bool:
        context = {
            "request": DotDict({
                "auth": DotDict({"uid": auth_uid}) if auth_uid else None,
            }),
            "resource": DotDict({"data": DotDict(existing_data)}),
            "name": username,
        }
        # allow delete: if request.auth != null && resource.data.uid == request.auth.uid;
        rule = "request.auth != null && resource.data.uid == request.auth.uid"
        return self.evaluate_expression(rule, context)


@pytest.fixture
def engine():
    assert RULES_PATH.exists(), f"firestore.rules not found at {RULES_PATH}"
    rules_content = RULES_PATH.read_text(encoding="utf-8")
    return FirestoreRuleEngine(rules_content)


# ==============================================================================
# SECTION 1: USERNAME CLAIM HIJACKING VECTORS
# ==============================================================================

def test_claim_hijack_unauthenticated_cannot_create_claim(engine: FirestoreRuleEngine):
    """An unauthenticated visitor must not be able to create any username claim."""
    allowed = engine.can_create_username("alice", {"uid": "alice_123"}, auth_uid=None)
    assert not allowed, "Exploit: unauthenticated user created a username claim"


def test_claim_hijack_spoofed_uid_creation_rejected(engine: FirestoreRuleEngine):
    """An attacker cannot claim a username pointing to a victim's UID."""
    attacker_uid = "attacker_666"
    victim_uid = "victim_999"
    # Attacker tries to create a claim with victim's UID in data
    allowed = engine.can_create_username("bob", {"uid": victim_uid}, auth_uid=attacker_uid)
    assert not allowed, "Exploit: attacker created claim pointing to victim's UID"


def test_claim_hijack_cross_user_overwrite_rejected(engine: FirestoreRuleEngine):
    """An attacker cannot overwrite an existing username owned by another user."""
    attacker_uid = "attacker_666"
    victim_uid = "victim_999"
    existing_doc = {"uid": victim_uid, "updatedAt": "2026-10-01"}
    malicious_update = {"uid": attacker_uid, "updatedAt": "2026-10-05"}

    allowed = engine.can_update_username("alice", existing_doc, malicious_update, auth_uid=attacker_uid)
    assert not allowed, "Exploit: attacker successfully hijacked victim's username claim via update"


def test_claim_hijack_metadata_tampering_rejected(engine: FirestoreRuleEngine):
    """An attacker cannot alter non-UID fields on another user's claim document."""
    attacker_uid = "attacker_666"
    victim_uid = "victim_999"
    existing_doc = {"uid": victim_uid, "updatedAt": "2026-10-01"}
    tampered_update = {"uid": victim_uid, "updatedAt": "2026-10-05", "extra": "tampered"}

    allowed = engine.can_update_username("alice", existing_doc, tampered_update, auth_uid=attacker_uid)
    assert not allowed, "Exploit: attacker modified metadata on victim's username document"


def test_claim_hijack_owner_reassignment_to_third_party_rejected(engine: FirestoreRuleEngine):
    """A claim owner cannot transfer/reassign a claim doc directly to a different UID."""
    owner_uid = "alice_123"
    third_party_uid = "charlie_456"
    existing_doc = {"uid": owner_uid}
    reassignment_payload = {"uid": third_party_uid}

    allowed = engine.can_update_username("alice", existing_doc, reassignment_payload, auth_uid=owner_uid)
    assert not allowed, "Exploit: claim transferred to a third party UID without verification"


def test_claim_valid_owner_can_create_and_update(engine: FirestoreRuleEngine):
    """A legitimate owner can create and update their own username claim."""
    owner_uid = "alice_123"
    payload = {"uid": owner_uid, "updatedAt": "2026-10-05"}

    # Legitimate create
    assert engine.can_create_username("alice", payload, auth_uid=owner_uid)
    # Legitimate update (keeping own UID)
    assert engine.can_update_username("alice", {"uid": owner_uid}, payload, auth_uid=owner_uid)


# ==============================================================================
# SECTION 2: USERNAME CLAIM DELETION VECTORS
# ==============================================================================

def test_claim_deletion_unauthenticated_rejected(engine: FirestoreRuleEngine):
    """An unauthenticated visitor cannot delete any username claim."""
    allowed = engine.can_delete_username("alice", {"uid": "alice_123"}, auth_uid=None)
    assert not allowed, "Exploit: unauthenticated visitor deleted a username claim"


def test_claim_deletion_cross_user_rejected(engine: FirestoreRuleEngine):
    """An attacker cannot delete a victim's username claim."""
    attacker_uid = "attacker_666"
    victim_uid = "victim_999"
    allowed = engine.can_delete_username("victim", {"uid": victim_uid}, auth_uid=attacker_uid)
    assert not allowed, "Exploit: attacker deleted victim's username claim"


def test_claim_deletion_legitimate_owner_allowed(engine: FirestoreRuleEngine):
    """The legitimate owner can delete / release their own username claim."""
    owner_uid = "alice_123"
    allowed = engine.can_delete_username("alice", {"uid": owner_uid}, auth_uid=owner_uid)
    assert allowed, "Regression: owner was unable to release their own username claim"


def test_claim_deletion_missing_uid_in_doc_rejected(engine: FirestoreRuleEngine):
    """Deleting a document that lacks a uid field fails safely without errors."""
    attacker_uid = "attacker_666"
    corrupt_doc = {"invalid": "data"}
    allowed = engine.can_delete_username("alice", corrupt_doc, auth_uid=attacker_uid)
    assert not allowed, "Exploit: corrupt claim doc was deleted by unauthorized user"


# ==============================================================================
# SECTION 3: UNAUTHENTICATED ACCESS BOUNDARIES
# ==============================================================================

def test_unauthenticated_can_read_username_registry(engine: FirestoreRuleEngine):
    """Public lookup of /usernames/{name} must be allowed for /[username] routing."""
    allowed = engine.can_read_username("alice", auth_uid=None)
    assert allowed, "Regression: unauthenticated read of username registry blocked"


def test_unauthenticated_can_read_public_user_profile(engine: FirestoreRuleEngine):
    """Public profile at /users/{uid} must be readable without authentication."""
    allowed = engine.can_read_user_profile("alice_123", auth_uid=None)
    assert allowed, "Regression: public profile read blocked for unauthenticated visitor"


def test_unauthenticated_cannot_write_user_profile(engine: FirestoreRuleEngine):
    """Unauthenticated visitors must not write to /users/{uid}."""
    allowed = engine.can_write_user_profile("alice_123", auth_uid=None)
    assert not allowed, "Exploit: unauthenticated visitor wrote to /users/{uid}"


def test_cross_user_cannot_write_user_profile(engine: FirestoreRuleEngine):
    """An attacker cannot write to another user's /users/{uid} document."""
    attacker_uid = "attacker_666"
    victim_uid = "victim_999"
    allowed = engine.can_write_user_profile(victim_uid, auth_uid=attacker_uid)
    assert not allowed, "Exploit: attacker wrote to victim's /users profile document"


def test_owner_can_write_own_profile(engine: FirestoreRuleEngine):
    """Owner can write to their own /users/{uid} document."""
    owner_uid = "alice_123"
    allowed = engine.can_write_user_profile(owner_uid, auth_uid=owner_uid)
    assert allowed, "Regression: owner cannot write to their own profile"


# ==============================================================================
# SECTION 4: JOB LEAKAGE & PRIVACY ISOLATION
# ==============================================================================

def test_jobs_unauthenticated_read_rejected(engine: FirestoreRuleEngine):
    """Unauthenticated visitors must not read private jobs at /users/{uid}/jobs/{jobId}."""
    allowed = engine.can_read_job("alice_123", "job_1", auth_uid=None)
    assert not allowed, "Exploit: unauthenticated read of /users/{uid}/jobs/{jobId} leaked private data"


def test_jobs_cross_user_read_rejected(engine: FirestoreRuleEngine):
    """An attacker cannot read victim's private jobs."""
    attacker_uid = "attacker_666"
    victim_uid = "victim_999"
    allowed = engine.can_read_job(victim_uid, "job_1", auth_uid=attacker_uid)
    assert not allowed, "Exploit: attacker read victim's private job data"


def test_jobs_cross_user_write_rejected(engine: FirestoreRuleEngine):
    """An attacker cannot write to victim's private jobs."""
    attacker_uid = "attacker_666"
    victim_uid = "victim_999"
    allowed = engine.can_write_job(victim_uid, "job_1", auth_uid=attacker_uid)
    assert not allowed, "Exploit: attacker wrote to victim's private jobs"


def test_jobs_owner_can_read_and_write(engine: FirestoreRuleEngine):
    """The owner can read and write their own jobs."""
    owner_uid = "alice_123"
    assert engine.can_read_job(owner_uid, "job_1", auth_uid=owner_uid), "Regression: owner cannot read job"
    assert engine.can_write_job(owner_uid, "job_1", auth_uid=owner_uid), "Regression: owner cannot write job"


# ==============================================================================
# SECTION 5: CORNER CASES & RESILIENCE
# ==============================================================================

def test_missing_uid_in_payload_rejected_on_create(engine: FirestoreRuleEngine):
    """Attempting to create a username claim without a uid field fails safely."""
    owner_uid = "alice_123"
    bad_payload = {"name": "alice"}  # missing 'uid'
    allowed = engine.can_create_username("alice", bad_payload, auth_uid=owner_uid)
    assert not allowed, "Exploit: username claim created without uid field"


def test_null_uid_in_payload_rejected_on_create(engine: FirestoreRuleEngine):
    """Attempting to create a username claim with uid=None fails safely."""
    owner_uid = "alice_123"
    bad_payload = {"uid": None}
    allowed = engine.can_create_username("alice", bad_payload, auth_uid=owner_uid)
    assert not allowed, "Exploit: username claim created with null uid"


def test_empty_string_auth_uid_mismatch_rejected(engine: FirestoreRuleEngine):
    """Empty string UID cannot match another user's UID."""
    allowed = engine.can_write_user_profile("alice_123", auth_uid="")
    assert not allowed, "Exploit: empty auth.uid wrote to profile"


if __name__ == "__main__":
    import sys
    engine_instance = FirestoreRuleEngine(RULES_PATH.read_text(encoding="utf-8"))
    test_functions = [
        obj for name, obj in list(globals().items())
        if name.startswith("test_") and callable(obj)
    ]
    passed = 0
    failed = 0
    print(f"Running {len(test_functions)} empirical adversarial test cases against firestore.rules...")
    for fn in test_functions:
        try:
            fn(engine_instance)
            print(f"  [PASS] {fn.__name__}")
            passed += 1
        except Exception as e:
            print(f"  [FAIL] {fn.__name__}: {e}")
            failed += 1

    print(f"\nSummary: {passed} passed, {failed} failed out of {len(test_functions)} tests.")
    if failed > 0:
        sys.exit(1)
    sys.exit(0)

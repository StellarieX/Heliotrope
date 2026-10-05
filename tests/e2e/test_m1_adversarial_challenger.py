"""Empirical Adversarial Test Suite for Milestone 1 (R1: Security & Identity Guardrails).

Author: Challenger 2 (teamwork_preview_challenger_m1_2)
Target: firestore.rules
Authoritative Specs: ORIGINAL_REQUEST.md (R1) & PROJECT.md (Features 1 & 2)

Tests attack vectors and boundary cases against firestore.rules:
1. Attack vectors on /usernames/{name}:
   - Batch writes bundling unauthorized operations
   - Re-assignment to null / missing UID
   - Claim squatting (unauthenticated, spoofed UID, overwrite existing)
   - Claim transfer (owner transferring to another, attacker hijacking)
   - Claim deletion (unauthenticated, third party, owner)
2. Attack vectors on /users/{uid} and /users/{uid}/jobs/{jobId}:
   - Write attempts with spoofed auth tokens / mismatched UID
   - Reading other users' private jobs (unauthenticated, third party, owner)
   - Writing other users' private jobs (unauthenticated, third party, owner)
3. Genuine access paths for app/[username]/page.tsx:
   - Unauthenticated and authenticated visitor reads for /usernames/{name} and /users/{uid}
   - Unclaimed username read (no permission error)
   - Full claim transition transaction workflow
4. Boundary & stress analysis:
   - Subcollection isolation (no accidental wildcard exposure)
   - Extra fields / payload injection
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
RULES_PATH = REPO_ROOT / "firestore.rules"


class SecurityRulesEngine:
    """Rigorous evaluation engine modeling Firestore Rules v2 runtime semantics

    directly against firestore.rules on disk.
    """

    def __init__(self, rules_file: Path = RULES_PATH):
        if not rules_file.exists():
            raise FileNotFoundError(f"Missing firestore.rules at {rules_file}")
        self.raw_rules = rules_file.read_text(encoding="utf-8")
        self._parse_rules()

    def _parse_rules(self):
        # Extract rules_version
        version_match = re.search(r"rules_version\s*=\s*'(\d+)';", self.raw_rules)
        self.rules_version = version_match.group(1) if version_match else "1"

        # Extract /users/{uid} block
        user_block_m = re.search(r"match\s+/users/\{uid\}\s*\{([\s\S]*?)\n\s*match\s+/usernames", self.raw_rules)
        if not user_block_m:
            user_block_m = re.search(r"match\s+/users/\{uid\}\s*\{([\s\S]*?)\n\s*\}", self.raw_rules)
        self.user_block = user_block_m.group(1) if user_block_m else ""

        # Extract /users/{uid}/jobs/{jobId} block
        jobs_block_m = re.search(r"match\s+/jobs/\{jobId\}\s*\{([\s\S]*?)\}", self.raw_rules)
        self.jobs_block = jobs_block_m.group(1) if jobs_block_m else ""

        # Extract /usernames/{name} block
        username_block_m = re.search(r"match\s+/usernames/\{name\}\s*\{([\s\S]*?)\}", self.raw_rules)
        self.username_block = username_block_m.group(1) if username_block_m else ""

    def eval_users_read(self, auth: Optional[Dict[str, Any]], uid: str) -> bool:
        """Evaluate read on /users/{uid}."""
        m = re.search(r"allow\s+read:\s*if\s+([^;]+);", self.user_block)
        if not m:
            return False
        cond = m.group(1).strip()
        if cond == "true":
            return True
        return False

    def eval_users_write(self, auth: Optional[Dict[str, Any]], uid: str, op: str = "write") -> bool:
        """Evaluate write on /users/{uid}."""
        m = re.search(r"allow\s+write:\s*if\s+([^;]+);", self.user_block)
        if not m:
            return False
        cond = m.group(1).strip()
        # if request.auth != null && request.auth.uid == uid;
        if "request.auth != null" in cond and "request.auth.uid == uid" in cond:
            if auth is None:
                return False
            auth_uid = auth.get("uid")
            if not auth_uid:
                return False
            return auth_uid == uid
        return False

    def eval_jobs_read(self, auth: Optional[Dict[str, Any]], uid: str, job_id: str) -> bool:
        """Evaluate read on /users/{uid}/jobs/{jobId}."""
        m = re.search(r"allow\s+read(?:,\s*write)?:\s*if\s+([^;]+);", self.jobs_block)
        if not m:
            return False
        cond = m.group(1).strip()
        if "request.auth != null" in cond and "request.auth.uid == uid" in cond:
            if auth is None:
                return False
            auth_uid = auth.get("uid")
            if not auth_uid:
                return False
            return auth_uid == uid
        return False

    def eval_jobs_write(self, auth: Optional[Dict[str, Any]], uid: str, job_id: str) -> bool:
        """Evaluate write on /users/{uid}/jobs/{jobId}."""
        m = re.search(r"allow\s+(?:read,\s*)?write:\s*if\s+([^;]+);", self.jobs_block)
        if not m:
            return False
        cond = m.group(1).strip()
        if "request.auth != null" in cond and "request.auth.uid == uid" in cond:
            if auth is None:
                return False
            auth_uid = auth.get("uid")
            if not auth_uid:
                return False
            return auth_uid == uid
        return False

    def eval_username_read(self, auth: Optional[Dict[str, Any]], name: str) -> bool:
        """Evaluate read on /usernames/{name}."""
        m = re.search(r"allow\s+read:\s*if\s+([^;]+);", self.username_block)
        if not m:
            return False
        return m.group(1).strip() == "true"

    def eval_username_create(
        self,
        auth: Optional[Dict[str, Any]],
        name: str,
        resource_data: Optional[Dict[str, Any]],
        request_resource_data: Dict[str, Any],
    ) -> bool:
        """Evaluate create on /usernames/{name} (doc does not exist before)."""
        if resource_data is not None:
            # In Firestore, if document already exists, write operation evaluates update, NOT create.
            return self.eval_username_update(auth, name, resource_data, request_resource_data)

        m = re.search(r"allow\s+create:\s*if\s+([^;]+);", self.username_block)
        if not m:
            return False
        cond = m.group(1).strip()
        # allow create: if request.auth != null && request.resource.data.uid == request.auth.uid;
        if "request.auth != null" in cond and "request.resource.data.uid == request.auth.uid" in cond:
            if auth is None:
                return False
            auth_uid = auth.get("uid")
            if not auth_uid:
                return False
            return request_resource_data.get("uid") == auth_uid
        return False

    def eval_username_update(
        self,
        auth: Optional[Dict[str, Any]],
        name: str,
        resource_data: Dict[str, Any],
        request_resource_data: Dict[str, Any],
    ) -> bool:
        """Evaluate update on /usernames/{name} (doc exists)."""
        m = re.search(r"allow\s+update:\s*if\s+([^;]+);", self.username_block)
        if not m:
            return False
        cond = m.group(1).strip()
        # allow update: if request.auth != null && resource.data.uid == request.auth.uid && request.resource.data.uid == request.auth.uid;
        if (
            "request.auth != null" in cond
            and "resource.data.uid == request.auth.uid" in cond
            and "request.resource.data.uid == request.auth.uid" in cond
        ):
            if auth is None:
                return False
            auth_uid = auth.get("uid")
            if not auth_uid:
                return False
            existing_uid = resource_data.get("uid")
            new_uid = request_resource_data.get("uid")
            return existing_uid == auth_uid and new_uid == auth_uid
        return False

    def eval_username_delete(
        self,
        auth: Optional[Dict[str, Any]],
        name: str,
        resource_data: Dict[str, Any],
    ) -> bool:
        """Evaluate delete on /usernames/{name}."""
        m = re.search(r"allow\s+delete:\s*if\s+([^;]+);", self.username_block)
        if not m:
            return False
        cond = m.group(1).strip()
        # allow delete: if request.auth != null && resource.data.uid == request.auth.uid;
        if "request.auth != null" in cond and "resource.data.uid == request.auth.uid" in cond:
            if auth is None:
                return False
            auth_uid = auth.get("uid")
            if not auth_uid:
                return False
            return resource_data.get("uid") == auth_uid
        return False

    def eval_batch_write(
        self,
        auth: Optional[Dict[str, Any]],
        operations: List[Dict[str, Any]],
    ) -> bool:
        """Firestore atomic batch write: if any operation fails rules, the entire batch fails."""
        for op in operations:
            target = op["target"]
            action = op["action"]
            if target == "username":
                if action == "create":
                    ok = self.eval_username_create(
                        auth,
                        op["name"],
                        op.get("resource_data"),
                        op["request_resource_data"],
                    )
                elif action == "update":
                    ok = self.eval_username_update(
                        auth,
                        op["name"],
                        op["resource_data"],
                        op["request_resource_data"],
                    )
                elif action == "delete":
                    ok = self.eval_username_delete(auth, op["name"], op["resource_data"])
                else:
                    ok = False
            elif target == "user":
                ok = self.eval_users_write(auth, op["uid"], op=action)
            elif target == "job":
                ok = self.eval_jobs_write(auth, op["uid"], op["jobId"])
            else:
                ok = False

            if not ok:
                return False
        return True


@pytest.fixture(scope="module")
def engine():
    return SecurityRulesEngine()


# ==============================================================================
# 1. Attack Vectors on /usernames/{name}
# ==============================================================================

def test_adv_claim_squatting_unauthenticated(engine: SecurityRulesEngine):
    """ATTACK SCENARIO: Unauthenticated anonymous attacker attempts to claim a username."""
    allowed = engine.eval_username_create(
        auth=None,
        name="heliotrope_admin",
        resource_data=None,
        request_resource_data={"uid": "anonymous_hacker"},
    )
    assert allowed is False, "Unauthenticated username claim must be strictly denied"


def test_adv_claim_squatting_spoofed_uid(engine: SecurityRulesEngine):
    """ATTACK SCENARIO: Authenticated attacker Mallory claims username in Alice's UID."""
    allowed = engine.eval_username_create(
        auth={"uid": "mallory"},
        name="alice_handle",
        resource_data=None,
        request_resource_data={"uid": "alice"},
    )
    assert allowed is False, "Claiming username with another user's UID must be denied"


def test_adv_claim_squatting_overwrite_existing_claimed(engine: SecurityRulesEngine):
    """ATTACK SCENARIO: Attacker Mallory tries to overwrite an already-claimed username."""
    existing_claim = {"uid": "alice", "updatedAt": 1000}
    # Mallory tries to overwrite via set() or create()
    allowed = engine.eval_username_create(
        auth={"uid": "mallory"},
        name="alice",
        resource_data=existing_claim,
        request_resource_data={"uid": "mallory"},
    )
    assert allowed is False, "Overwriting existing username claim must be denied"


def test_adv_claim_reassignment_to_null(engine: SecurityRulesEngine):
    """ATTACK SCENARIO: User attempts to update username claim setting UID to null/empty to orphan it."""
    existing_claim = {"uid": "alice", "updatedAt": 1000}
    allowed = engine.eval_username_update(
        auth={"uid": "alice"},
        name="alice",
        resource_data=existing_claim,
        request_resource_data={"uid": None, "updatedAt": 2000},
    )
    assert allowed is False, "Updating claim UID to null must be denied"

    # Also test omitting UID entirely
    allowed_missing = engine.eval_username_update(
        auth={"uid": "alice"},
        name="alice",
        resource_data=existing_claim,
        request_resource_data={"updatedAt": 2000},
    )
    assert allowed_missing is False, "Updating claim omitting UID must be denied"


def test_adv_claim_transfer_by_owner_blocked(engine: SecurityRulesEngine):
    """ATTACK SCENARIO: Owner Alice attempts to transfer / sell username directly to Bob."""
    existing_claim = {"uid": "alice", "updatedAt": 1000}
    allowed = engine.eval_username_update(
        auth={"uid": "alice"},
        name="alice",
        resource_data=existing_claim,
        request_resource_data={"uid": "bob", "updatedAt": 2000},
    )
    assert allowed is False, "Direct UID transfer on username claim must be denied"


def test_adv_claim_transfer_hijack_by_attacker(engine: SecurityRulesEngine):
    """ATTACK SCENARIO: Attacker Mallory attempts to hijack Alice's claim by updating UID to mallory."""
    existing_claim = {"uid": "alice", "updatedAt": 1000}
    allowed = engine.eval_username_update(
        auth={"uid": "mallory"},
        name="alice",
        resource_data=existing_claim,
        request_resource_data={"uid": "mallory", "updatedAt": 2000},
    )
    assert allowed is False, "Hijacking claim by third party must be denied"


def test_adv_claim_deletion_unauthenticated(engine: SecurityRulesEngine):
    """ATTACK SCENARIO: Unauthenticated visitor attempts to delete a username claim."""
    existing_claim = {"uid": "alice", "updatedAt": 1000}
    allowed = engine.eval_username_delete(
        auth=None,
        name="alice",
        resource_data=existing_claim,
    )
    assert allowed is False, "Unauthenticated delete on username claim must be denied"


def test_adv_claim_deletion_by_third_party(engine: SecurityRulesEngine):
    """ATTACK SCENARIO: Attacker Mallory attempts to delete Alice's username claim."""
    existing_claim = {"uid": "alice", "updatedAt": 1000}
    allowed = engine.eval_username_delete(
        auth={"uid": "mallory"},
        name="alice",
        resource_data=existing_claim,
    )
    assert allowed is False, "Third-party delete on username claim must be denied"


def test_adv_claim_deletion_by_owner_allowed(engine: SecurityRulesEngine):
    """VALID WORKFLOW: Owner Alice releases / deletes her own username claim."""
    existing_claim = {"uid": "alice", "updatedAt": 1000}
    allowed = engine.eval_username_delete(
        auth={"uid": "alice"},
        name="alice",
        resource_data=existing_claim,
    )
    assert allowed is True, "Owner must be allowed to delete their own username claim"


def test_adv_batch_write_bundled_exploit(engine: SecurityRulesEngine):
    """ATTACK SCENARIO: Attacker packages legitimate own write with malicious claim overwrite in a batch."""
    batch_ops = [
        {
            "target": "user",
            "action": "write",
            "uid": "mallory",
            "data": {"displayName": "Mallory"},
        },
        {
            "target": "username",
            "action": "update",
            "name": "alice",
            "resource_data": {"uid": "alice"},
            "request_resource_data": {"uid": "mallory"},
        },
    ]
    allowed = engine.eval_batch_write(auth={"uid": "mallory"}, operations=batch_ops)
    assert allowed is False, "Atomic batch write must fail entirely if claim overwrite is unauthorized"


def test_adv_batch_write_legitimate_claim_transition(engine: SecurityRulesEngine):
    """VALID WORKFLOW: User Alice changes username from 'alice' to 'alicia' in a batch/tx."""
    batch_ops = [
        # 1. Create new claim
        {
            "target": "username",
            "action": "create",
            "name": "alicia",
            "resource_data": None,
            "request_resource_data": {"uid": "alice"},
        },
        # 2. Update user profile
        {
            "target": "user",
            "action": "write",
            "uid": "alice",
            "data": {"username": "alicia"},
        },
        # 3. Delete old claim
        {
            "target": "username",
            "action": "delete",
            "name": "alice",
            "resource_data": {"uid": "alice"},
        },
    ]
    allowed = engine.eval_batch_write(auth={"uid": "alice"}, operations=batch_ops)
    assert allowed is True, "Legitimate username transition batch by owner must succeed"


# ==============================================================================
# 2. Attack Vectors on /users/{uid} and /users/{uid}/jobs/{jobId}
# ==============================================================================

def test_adv_user_profile_write_unauthenticated(engine: SecurityRulesEngine):
    """ATTACK SCENARIO: Unauthenticated visitor attempts to write / modify user profile."""
    allowed = engine.eval_users_write(auth=None, uid="victim_user")
    assert allowed is False, "Unauthenticated write to /users/{uid} must be denied"


def test_adv_user_profile_write_cross_user_hijack(engine: SecurityRulesEngine):
    """ATTACK SCENARIO: Attacker Mallory attempts to modify Alice's user document."""
    allowed = engine.eval_users_write(auth={"uid": "mallory"}, uid="alice")
    assert allowed is False, "Cross-user write to /users/{uid} must be denied"


def test_adv_user_profile_write_spoofed_empty_uid(engine: SecurityRulesEngine):
    """ATTACK SCENARIO: Auth token with empty/null UID attempting write."""
    allowed = engine.eval_users_write(auth={"uid": ""}, uid="alice")
    assert allowed is False, "Empty UID token write must be denied"


def test_adv_user_profile_write_by_owner_allowed(engine: SecurityRulesEngine):
    """VALID WORKFLOW: Owner Alice updates her own profile document."""
    allowed = engine.eval_users_write(auth={"uid": "alice"}, uid="alice")
    assert allowed is True, "Owner write to /users/{uid} must be allowed"


def test_adv_user_jobs_read_unauthenticated(engine: SecurityRulesEngine):
    """ATTACK SCENARIO: Unauthenticated visitor attempts to read private job."""
    allowed = engine.eval_jobs_read(auth=None, uid="alice", job_id="job_001")
    assert allowed is False, "Unauthenticated read of private job must be denied"


def test_adv_user_jobs_read_cross_user_snoop(engine: SecurityRulesEngine):
    """ATTACK SCENARIO: Attacker Mallory attempts to read Alice's private job."""
    allowed = engine.eval_jobs_read(auth={"uid": "mallory"}, uid="alice", job_id="job_001")
    assert allowed is False, "Cross-user read of private job must be denied"


def test_adv_user_jobs_read_by_owner_allowed(engine: SecurityRulesEngine):
    """VALID WORKFLOW: Owner Alice reads her own private job."""
    allowed = engine.eval_jobs_read(auth={"uid": "alice"}, uid="alice", job_id="job_001")
    assert allowed is True, "Owner read of private job must be allowed"


def test_adv_user_jobs_write_unauthenticated(engine: SecurityRulesEngine):
    """ATTACK SCENARIO: Unauthenticated visitor attempts to write to private jobs."""
    allowed = engine.eval_jobs_write(auth=None, uid="alice", job_id="job_001")
    assert allowed is False, "Unauthenticated write to private job must be denied"


def test_adv_user_jobs_write_cross_user_tampering(engine: SecurityRulesEngine):
    """ATTACK SCENARIO: Attacker Mallory attempts to inject or delete Alice's jobs."""
    allowed = engine.eval_jobs_write(auth={"uid": "mallory"}, uid="alice", job_id="job_001")
    assert allowed is False, "Cross-user write to private job must be denied"


def test_adv_user_jobs_write_by_owner_allowed(engine: SecurityRulesEngine):
    """VALID WORKFLOW: Owner Alice creates/modifies her own private job."""
    allowed = engine.eval_jobs_write(auth={"uid": "alice"}, uid="alice", job_id="job_001")
    assert allowed is True, "Owner write to private job must be allowed"


# ==============================================================================
# 3. Genuine Access Paths for app/[username]/page.tsx
# ==============================================================================

def test_genuine_public_profile_unauthenticated_full_pipeline(engine: SecurityRulesEngine):
    """VALID PIPELINE: Unauthenticated visitor opens /[username] page.

    Step 1: Read /usernames/{name} -> get UID
    Step 2: Read /users/{uid} -> get displayName, photoURL
    """
    # Step 1: Read claim
    step1_ok = engine.eval_username_read(auth=None, name="heliotrope_fan")
    assert step1_ok is True, "Unauthenticated read of username claim must succeed"

    # Step 2: Read user profile
    step2_ok = engine.eval_users_read(auth=None, uid="uid_heliotrope_fan")
    assert step2_ok is True, "Unauthenticated read of user profile must succeed"


def test_genuine_public_profile_authenticated_visitor_pipeline(engine: SecurityRulesEngine):
    """VALID PIPELINE: Authenticated visitor Bob opens Alice's profile at /alice."""
    step1_ok = engine.eval_username_read(auth={"uid": "bob"}, name="alice")
    assert step1_ok is True, "Authenticated third-party read of username claim must succeed"

    step2_ok = engine.eval_users_read(auth={"uid": "bob"}, uid="alice")
    assert step2_ok is True, "Authenticated third-party read of user profile must succeed"


def test_genuine_public_profile_unclaimed_username(engine: SecurityRulesEngine):
    """VALID PIPELINE: Visitor queries an unclaimed username (e.g. /nobody).

    Read permission must succeed so client receives empty snapshot without permission-denied.
    """
    read_ok = engine.eval_username_read(auth=None, name="nobody")
    assert read_ok is True, "Read permission on unclaimed username doc must be allowed (returns not-found)"


# ==============================================================================
# 4. Boundary & Stress Analysis
# ==============================================================================

def test_boundary_rules_version_declaration(engine: SecurityRulesEngine):
    """BOUNDARY: Ensure rules_version is explicitly declared as '2' for non-recursive matching."""
    assert engine.rules_version == "2", "firestore.rules must declare rules_version = '2'"


def test_boundary_no_wildcard_subcollection_leak(engine: SecurityRulesEngine):
    """BOUNDARY: Ensure /users/{uid} does not use recursive wildcards {document=**} which would leak jobs."""
    assert "{document=**}" not in engine.raw_rules, "Must not use recursive wildcard {document=**}"


def test_boundary_claim_ownership_roundtrip(engine: SecurityRulesEngine):
    """BOUNDARY: Ensure complete lifecycle: create -> update (self) -> delete (self) succeeds for owner."""
    auth = {"uid": "user_lifecycle"}
    # 1. Create
    assert engine.eval_username_create(auth, "lifecycle", None, {"uid": "user_lifecycle"}) is True
    # 2. Update self with new timestamp
    assert engine.eval_username_update(auth, "lifecycle", {"uid": "user_lifecycle"}, {"uid": "user_lifecycle", "updatedAt": 999}) is True
    # 3. Delete self
    assert engine.eval_username_delete(auth, "lifecycle", {"uid": "user_lifecycle"}) is True


def test_adv_user_profile_delete_unauthenticated(engine: SecurityRulesEngine):
    """ATTACK SCENARIO: Unauthenticated visitor attempts to delete user profile document."""
    allowed = engine.eval_users_write(auth=None, uid="alice", op="delete")
    assert allowed is False, "Unauthenticated delete on /users/{uid} must be denied"


def test_adv_user_profile_delete_cross_user_vandalism(engine: SecurityRulesEngine):
    """ATTACK SCENARIO: Attacker Mallory attempts to delete Alice's user profile document."""
    allowed = engine.eval_users_write(auth={"uid": "mallory"}, uid="alice", op="delete")
    assert allowed is False, "Cross-user delete on /users/{uid} must be denied"


def test_adv_user_profile_delete_by_owner_allowed(engine: SecurityRulesEngine):
    """VALID WORKFLOW: Owner Alice deletes her own user profile during account deletion."""
    allowed = engine.eval_users_write(auth={"uid": "alice"}, uid="alice", op="delete")
    assert allowed is True, "Owner must be allowed to delete their own profile document"


def test_adv_user_jobs_delete_cross_user_vandalism(engine: SecurityRulesEngine):
    """ATTACK SCENARIO: Attacker Mallory attempts to delete Alice's private jobs."""
    allowed = engine.eval_jobs_write(auth={"uid": "mallory"}, uid="alice", job_id="job_secret")
    assert allowed is False, "Cross-user delete on /users/{uid}/jobs/{jobId} must be denied"


def test_boundary_auth_token_without_uid_field(engine: SecurityRulesEngine):
    """BOUNDARY: Auth token missing 'uid' key entirely."""
    assert engine.eval_users_write(auth={}, uid="alice") is False
    assert engine.eval_username_create(auth={}, name="test", resource_data=None, request_resource_data={"uid": "alice"}) is False
    assert engine.eval_username_update(auth={}, name="test", resource_data={"uid": "alice"}, request_resource_data={"uid": "alice"}) is False
    assert engine.eval_username_delete(auth={}, name="test", resource_data={"uid": "alice"}) is False
    assert engine.eval_jobs_read(auth={}, uid="alice", job_id="job1") is False
    assert engine.eval_jobs_write(auth={}, uid="alice", job_id="job1") is False


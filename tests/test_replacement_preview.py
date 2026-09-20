"""Replacement preview tests: signatures, change diffing, ordering, validation.

Run with: python tests/test_replacement_preview.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.replacement import verdict_signature  # noqa: E402
from app.store import store  # noqa: E402

client = TestClient(app)


def setup_function():
    store.clear()


def create_policy(policy):
    r = client.post("/api/v1/policies", json=policy)
    assert r.status_code == 201, r.text
    return r.json()["id"]


APPROVE_POLICY = {
    "rules": [
        {
            "id": "approve",
            "priority": 10,
            "conclusion": "approve",
            "expr": {
                "all": [
                    {"field": "income", "op": "ge", "value": 50000},
                    {"field": "debt_ratio", "op": "lt", "value": 0.4},
                ]
            },
        },
        {
            "id": "reject",
            "priority": 10,
            "conclusion": "reject",
            "expr": {"field": "debt_ratio", "op": "ge", "value": 0.6},
        },
    ]
}

# Raise the income threshold: an 80k applicant falls through to no_match.
CANDIDATE_POLICY = {
    "rules": [
        {
            "id": "approve",
            "priority": 10,
            "conclusion": "approve",
            "expr": {
                "all": [
                    {"field": "income", "op": "ge", "value": 90000},
                    {"field": "debt_ratio", "op": "lt", "value": 0.4},
                ]
            },
        },
        {
            "id": "reject",
            "priority": 10,
            "conclusion": "reject",
            "expr": {"field": "debt_ratio", "op": "ge", "value": 0.6},
        },
    ]
}


def preview(pid, rules, cases):
    return client.post(
        f"/api/v1/policies/{pid}/replacement-preview",
        json={"rules": rules, "cases": cases},
    )


def test_only_cases_with_different_signatures_are_listed():
    pid = create_policy(APPROVE_POLICY)
    r = preview(pid, CANDIDATE_POLICY["rules"], [
        {"case_id": "c-approve",
         "facts": {"income": 80000, "debt_ratio": 0.2}},
        {"case_id": "c-reject",
         "facts": {"income": 10000, "debt_ratio": 0.8}},
        {"case_id": "c-unknown", "facts": {}},
        {"case_id": "c-still-approves",
         "facts": {"income": 95000, "debt_ratio": 0.2}},
        {"case_id": "c-no-match",
         "facts": {"income": 10000, "debt_ratio": 0.5}},
    ])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["policy_id"] == pid
    assert body["case_count"] == 5
    assert body["change_count"] == 1

    change = body["changes"][0]
    assert change["case_id"] == "c-approve"
    assert change["old_signature"] == {
        "status": "decided", "conclusion": "approve",
        "decided_priority": 10,
    }
    assert change["new_signature"] == {
        "status": "no_match", "conclusion": None, "decided_priority": None,
    }

    assert body["transition_counts"] == [{
        "from": {"status": "decided", "conclusion": "approve",
                 "decided_priority": 10},
        "to": {"status": "no_match", "conclusion": None,
               "decided_priority": None},
        "count": 1,
    }]


def test_unknown_minimal_field_sets_participate_in_signature():
    pid = create_policy({"rules": [
        {"id": "r", "priority": 1, "conclusion": "yes",
         "expr": {"field": "x", "op": "eq", "value": 1}},
    ]})
    candidate = [
        {"id": "r", "priority": 1, "conclusion": "yes",
         "expr": {"field": "y", "op": "eq", "value": 1}},
    ]
    # Same status/conclusion/priority, but the unknown gap moves x -> y.
    r = preview(pid, candidate, [{"case_id": "k", "facts": {}}])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["change_count"] == 1
    change = body["changes"][0]
    assert change["old_signature"] == {
        "status": "unknown", "conclusion": None,
        "decided_priority": 1,
        "minimal_missing_field_sets": [["x"]],
    }
    assert change["new_signature"]["minimal_missing_field_sets"] == [["y"]]


def test_conflict_signature_carries_conflicting_conclusions():
    pid = create_policy({"rules": [
        {"id": "a", "priority": 1, "conclusion": "yes",
         "expr": {"field": "v", "op": "ge", "value": 10}},
        {"id": "b", "priority": 1, "conclusion": "no",
         "expr": {"field": "v", "op": "ne", "value": 5}},
    ]})
    candidate = [
        {"id": "a", "priority": 1, "conclusion": "yes",
         "expr": {"field": "v", "op": "ge", "value": 10}},
    ]
    r = preview(pid, candidate, [
        {"case_id": "c-conflict", "facts": {"v": 10}},
        {"case_id": "c-nomatch", "facts": {"v": 5}},
    ])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["case_count"] == 2 and body["change_count"] == 1
    change = body["changes"][0]
    assert change["case_id"] == "c-conflict"
    assert change["old_signature"] == {
        "status": "conflict", "conclusion": None, "decided_priority": 1,
        "conflicting_conclusions": ["no", "yes"],
    }
    assert change["new_signature"] == {
        "status": "decided", "conclusion": "yes", "decided_priority": 1,
    }
    assert body["transition_counts"][0]["count"] == 1


def test_changes_and_transition_counts_are_deterministically_sorted():
    pid = create_policy({"rules": [
        {"id": "allow", "priority": 10, "conclusion": "allow",
         "expr": {"field": "x", "op": "eq", "value": 1}},
        {"id": "review", "priority": 1, "conclusion": "review",
         "expr": {"field": "y", "op": "eq", "value": 1}},
    ]})
    candidate = [
        {"id": "deny", "priority": 10, "conclusion": "deny",
         "expr": {"field": "x", "op": "eq", "value": 1}},
    ]
    cases = [
        # decided review @1 -> no_match
        {"case_id": "b", "facts": {"x": 2, "y": 1}},
        # decided allow @10 -> decided deny @10
        {"case_id": "a", "facts": {"x": 1}},
        {"case_id": "a2", "facts": {"x": 1}},
        # unchanged: no_match and unknown
        {"case_id": "c", "facts": {"x": 2, "y": 2}},
        {"case_id": "d", "facts": {}},
    ]
    r = preview(pid, candidate, cases)
    assert r.status_code == 200, r.text
    body = r.json()

    # Changed cases sorted by case_id, regardless of submission order.
    assert [c["case_id"] for c in body["changes"]] == ["a", "a2", "b"]

    transitions = body["transition_counts"]
    assert len(transitions) == 2
    # Canonical JSON order: old conclusion "allow" < "review".
    assert transitions[0] == {
        "from": {"status": "decided", "conclusion": "allow",
                 "decided_priority": 10},
        "to": {"status": "decided", "conclusion": "deny",
               "decided_priority": 10},
        "count": 2,
    }
    assert transitions[1] == {
        "from": {"status": "decided", "conclusion": "review",
                 "decided_priority": 1},
        "to": {"status": "no_match", "conclusion": None,
               "decided_priority": None},
        "count": 1,
    }

    # Output is stable across repeated calls and reordered input.
    r2 = preview(pid, candidate, list(reversed(cases)))
    assert r2.json() == body


def test_identical_policy_produces_no_changes():
    pid = create_policy(APPROVE_POLICY)
    r = preview(pid, APPROVE_POLICY["rules"], [
        {"case_id": "a", "facts": {"income": 80000, "debt_ratio": 0.2}},
        {"case_id": "b", "facts": {}},
    ])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["change_count"] == 0
    assert body["changes"] == []
    assert body["transition_counts"] == []


def test_preview_never_writes_to_storage():
    pid = create_policy(APPROVE_POLICY)
    before = client.get(f"/api/v1/policies/{pid}").json()
    policy_count = len(client.get("/api/v1/policies").json()["policies"])

    r = preview(pid, CANDIDATE_POLICY["rules"], [
        {"case_id": "a", "facts": {"income": 80000, "debt_ratio": 0.2}},
    ])
    assert r.status_code == 200
    after = client.get(f"/api/v1/policies/{pid}").json()
    assert after == before
    assert len(client.get("/api/v1/policies").json()["policies"]) == policy_count


def test_invalid_candidate_rules_return_422_without_writing():
    pid = create_policy(APPROVE_POLICY)
    before = client.get(f"/api/v1/policies/{pid}").json()

    r = preview(pid, [{"priority": 1}], [
        {"case_id": "a", "facts": {}},
    ])
    assert r.status_code == 422
    out = r.json()
    assert out["detail"] == "Policy structure validation failed"
    assert any("conclusion" in e for e in out["errors"])

    after = client.get(f"/api/v1/policies/{pid}").json()
    assert after == before


def test_invalid_rules_and_duplicate_case_ids_report_both_errors():
    pid = create_policy(APPROVE_POLICY)
    r = preview(pid, [{}], [
        {"case_id": "a", "facts": {}},
        {"case_id": "a", "facts": {}},
    ])
    assert r.status_code == 422
    errors = r.json()["errors"]
    assert any("rules[0]" in e for e in errors)
    assert any("duplicate case id" in e for e in errors)
    # The failed preview must not have touched storage.
    assert client.get(f"/api/v1/policies/{pid}").json()["rule_count"] == 2


def test_validation_error_bodies():
    pid = create_policy(APPROVE_POLICY)
    valid_rules = APPROVE_POLICY["rules"]
    bad_bodies = [
        {},
        [1, 2],
        {"cases": [{"case_id": "a"}]},
        {"rules": valid_rules},
        {"rules": valid_rules, "cases": []},
        {"rules": valid_rules, "cases": "nope"},
        {"rules": valid_rules, "cases": [1]},
        {"rules": valid_rules, "cases": [{"facts": {}}]},
        {"rules": valid_rules, "cases": [{"case_id": "", "facts": {}}]},
        {"rules": valid_rules, "cases": [{"case_id": "a", "facts": []}]},
        {"rules": [], "cases": [{"case_id": "a", "facts": {}}]},
    ]
    for body in bad_bodies:
        r = client.post(
            f"/api/v1/policies/{pid}/replacement-preview", json=body
        )
        assert r.status_code == 422, body
        out = r.json()
        assert out["detail"] == "Policy structure validation failed"
        assert isinstance(out["errors"], list) and out["errors"]


def test_malformed_json_is_400():
    pid = create_policy(APPROVE_POLICY)
    r = client.post(
        f"/api/v1/policies/{pid}/replacement-preview",
        content="{not json",
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 400


def test_unknown_policy_returns_404():
    r = preview("nope", APPROVE_POLICY["rules"], [
        {"case_id": "a", "facts": {}},
    ])
    assert r.status_code == 404


def test_verdict_signature_helper_shape():
    pid = create_policy({"rules": [
        {"id": "r", "priority": 1, "conclusion": "yes",
         "expr": {"any": [
             {"field": "a", "op": "eq", "value": 1},
             {"field": "b", "op": "eq", "value": 1},
         ]}},
    ]})
    from app.engine import adjudicate

    record = client.get(f"/api/v1/policies/{pid}").json()
    unknown_sig = verdict_signature(adjudicate(record, {}))
    assert unknown_sig["status"] == "unknown"
    assert unknown_sig["minimal_missing_field_sets"] == [["a"], ["b"]]
    assert "conflicting_conclusions" not in unknown_sig

    decided_sig = verdict_signature(adjudicate(record, {"a": 1}))
    assert decided_sig == {
        "status": "decided", "conclusion": "yes", "decided_priority": 1,
    }
    assert "minimal_missing_field_sets" not in decided_sig


def _run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for test in tests:
        setup_function()
        try:
            test()
            print(f"PASS {test.__name__}")
        except Exception:
            failures += 1
            import traceback
            traceback.print_exc()
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return failures


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)

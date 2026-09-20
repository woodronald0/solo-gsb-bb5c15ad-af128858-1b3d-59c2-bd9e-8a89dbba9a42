"""Policy replacement impact preview.

Before replacing a stored policy, a caller submits the *candidate* rules
together with a set of uniquely identified cases.  Every case is adjudicated
twice — once against the current policy and once against the candidate policy,
which is never persisted.

Each verdict is reduced to a compact **business signature**: the verdict
``status``, its ``conclusion`` and ``decided_priority``, augmented only with
the key sets that distinguish the interesting outcomes:

* ``conflict`` carries the sorted ``conflicting_conclusions``;
* ``unknown`` carries the inclusion-minimal ``minimal_missing_field_sets``.

Only cases whose old and new signatures differ are reported as ``changes``;
``transition_counts`` aggregates the cases by their ``old -> new`` signature
pair.  Cases and transition keys are deterministically sorted, so identical
requests always produce byte-identical responses.
"""
from __future__ import annotations

import json
from collections import Counter
from typing import Any

from .engine import adjudicate


def canonical_json(value: Any) -> str:
    """Deterministic JSON encoding used to order and key signatures."""
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )


def verdict_signature(verdict: dict) -> dict:
    """Reduce an adjudication verdict to its business-result signature."""
    signature: dict[str, Any] = {
        "status": verdict["status"],
        "conclusion": verdict.get("conclusion"),
        "decided_priority": verdict.get("decided_priority"),
    }
    if verdict["status"] == "conflict":
        signature["conflicting_conclusions"] = sorted(
            verdict.get("conflicting_conclusions", [])
        )
    elif verdict["status"] == "unknown":
        signature["minimal_missing_field_sets"] = sorted(
            (sorted(field_set)
             for field_set in verdict.get("minimal_missing_field_sets", [])),
            key=lambda fs: (len(fs), fs),
        )
    return signature


def _signature_key(signature: dict) -> str:
    return canonical_json(signature)


def replacement_preview(
    current_policy: dict,
    candidate_rules: list[dict],
    cases: list[dict],
) -> dict:
    """Adjudicate every case under both policies and diff their signatures.

    Returns a dict with ``case_count``, ``change_count``, the ``changes``
    list (sorted by ``case_id``; entries without a signature change are
    omitted) and ``transition_counts`` (aggregated ``old -> new`` counts,
    keys ordered by the canonical JSON of both signatures).
    """
    candidate_policy = {"rules": candidate_rules}

    results: list[tuple[str, dict, dict]] = []
    for case in cases:
        old_signature = verdict_signature(
            adjudicate(current_policy, case["facts"])
        )
        new_signature = verdict_signature(
            adjudicate(candidate_policy, case["facts"])
        )
        results.append((case["case_id"], old_signature, new_signature))

    results.sort(key=lambda item: item[0])

    changes: list[dict] = []
    transition_counts: Counter = Counter()
    for case_id, old_signature, new_signature in results:
        if old_signature != new_signature:
            changes.append(
                {
                    "case_id": case_id,
                    "old_signature": old_signature,
                    "new_signature": new_signature,
                }
            )
            transition_counts[
                (_signature_key(old_signature), _signature_key(new_signature))
            ] += 1

    ordered_transitions = [
        {
            "from": json.loads(old_key),
            "to": json.loads(new_key),
            "count": count,
        }
        for (old_key, new_key), count in sorted(transition_counts.items())
    ]

    return {
        "case_count": len(results),
        "change_count": len(changes),
        "changes": changes,
        "transition_counts": ordered_transitions,
    }

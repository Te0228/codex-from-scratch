"""Offline statistics prototype; no provider, transport, or live cache simulation.

Input usage is the raw Responses-style usage object. A production adapter must
first establish that input_tokens includes cached_tokens for its provider.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import math
import random
from statistics import fmean
from typing import Any


@dataclass(frozen=True)
class Attempt:
    attempt_id: str
    task_id: str
    phase: str  # prewarm or measure; retries are distinct attempts, not phases
    status: str  # completed, failed, cancelled
    usage: dict[str, Any] | None
    evidence_kind: str = "fixture"
    semantics: str = "responses_input_includes_cached_v1"


def _nonnegative_int(value: Any) -> bool:
    return type(value) is int and value >= 0  # bool must not masquerade as a count


def qualify(attempt: Attempt) -> tuple[tuple[int, int] | None, str | None]:
    """Return (input, cached) together, or an auditable exclusion reason."""
    if attempt.semantics != "responses_input_includes_cached_v1":
        return None, "unverified_provider_semantics"
    usage = attempt.usage
    if usage is None:
        return None, "raw_usage_unavailable"
    if not isinstance(usage, dict):
        return None, "malformed_usage"
    if "input_tokens" not in usage:
        return None, "input_missing"
    details = usage.get("input_tokens_details")
    if details is None:
        return None, "cached_details_missing"
    if not isinstance(details, dict):
        return None, "malformed_cached_details"
    if "cached_tokens" not in details:
        return None, "cached_tokens_missing"
    total, cached = usage["input_tokens"], details["cached_tokens"]
    if not _nonnegative_int(total) or not _nonnegative_int(cached):
        return None, "invalid_count"
    if cached > total:
        return None, "cached_exceeds_input"
    return (total, cached), None


def summarize(attempts: list[Attempt]) -> dict[str, Any]:
    """Summary of the explicitly supplied cohort, including failures/prewarm.

    Replayed identical ledger records are deduplicated; an actual retry has a
    new attempt_id and remains billable. Conflicting duplicates are rejected.
    """
    unique: dict[str, Attempt] = {}
    for a in attempts:
        if not a.attempt_id or not a.task_id:
            raise ValueError("attempt and task IDs are required")
        if a.phase not in {"prewarm", "measure"}:
            raise ValueError("invalid phase")
        if a.status not in {"completed", "failed", "cancelled"}:
            raise ValueError("invalid status")
        if a.evidence_kind not in {"fixture", "live", "replay", "unknown"}:
            raise ValueError("invalid evidence kind")
        if a.attempt_id in unique and unique[a.attempt_id] != a:
            raise ValueError(f"conflicting attempt: {a.attempt_id}")
        unique[a.attempt_id] = a
    kinds = {a.evidence_kind for a in unique.values()}
    if len(kinds) > 1:
        raise ValueError("different evidence kinds cannot share a cohort")
    qualified: list[tuple[int, int]] = []
    exclusions: dict[str, str] = {}
    for a in unique.values():
        values, reason = qualify(a)
        if values is None:
            exclusions[a.attempt_id] = reason
        else:
            qualified.append(values)
    total = sum(v[0] for v in qualified)
    cached = sum(v[1] for v in qualified)
    n = len(qualified)
    # Keep the known input sum over all raw observations separate from the
    # denominator above. Unknown cache usage must not dilute the cache ratio.
    known_inputs = [a.usage["input_tokens"] for a in unique.values()
                    if isinstance(a.usage, dict)
                    and _nonnegative_int(a.usage.get("input_tokens"))
                    and a.semantics == "responses_input_includes_cached_v1"]
    return {
        "evidence_kind": next(iter(kinds), None),
        "attempts": len(unique),
        "duplicate_records": len(attempts) - len(unique),
        "phase_counts": dict(Counter(a.phase for a in unique.values())),
        "status_counts": dict(Counter(a.status for a in unique.values())),
        "eligible_attempts": n,
        "usage_coverage": n / len(unique) if unique else None,
        "eligible_input_tokens": total,
        "eligible_cached_tokens": cached,
        "cached_token_fraction": cached / total if total else None,
        "positive_cached_attempt_fraction": sum(c > 0 for _, c in qualified) / n if n else None,
        "excluded": exclusions,
        "known_input_tokens_all_attempts": sum(known_inputs),
        "known_input_coverage": len(known_inputs) / len(unique) if unique else None,
        "total_cost": None,  # no prices, output/cache-write accounting, or bill evidence
    }


def paired_mean_interval(
    pairs: list[tuple[str, float, float]], *, seed: int = 28, resamples: int = 5000
) -> dict[str, Any]:
    """Percentile paired bootstrap CI, treatment minus baseline.

    One independent task/block per pair; caller must aggregate its retries and
    repeated calls first, retain incomplete pairs separately, and declare the
    metric/unit. This does not validate a causal experimental design.
    """
    if len(pairs) < 2 or resamples < 100:
        raise ValueError("need >=2 independent pairs and >=100 resamples")
    if len({p[0] for p in pairs}) != len(pairs):
        raise ValueError("repeated task IDs would create pseudoreplication")
    for _, baseline, treatment in pairs:
        if any(type(x) not in {int, float} or not math.isfinite(x) for x in (baseline, treatment)):
            raise ValueError("pair metrics must be finite numbers")
    differences = [t - b for _, b, t in pairs]
    rng = random.Random(seed)
    samples = sorted(fmean(rng.choices(differences, k=len(differences))) for _ in range(resamples))

    def quantile(p: float) -> float:
        index = (len(samples) - 1) * p
        low = math.floor(index)
        high = math.ceil(index)
        return samples[low] + (samples[high] - samples[low]) * (index - low)

    return {"pairs": len(pairs), "mean_treatment_minus_baseline": fmean(differences),
            "percentile_ci95": [quantile(.025), quantile(.975)],
            "seed": seed, "resamples": resamples, "method": "paired_percentile_bootstrap"}

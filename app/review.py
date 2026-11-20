"""Review-support logic: explicit jump rules, coverage eligibility and the
minimum-reviewer set-cover plan.

The module is framework free (like :mod:`engine`) so the rules can be tested
without Flask or SQLite.

Terminology
-----------

* An *explicit jump rule* is one ``(question, option) -> target question``
  entry declared in the questionnaire.  Different options of the same
  question are separate rules and are counted separately.
* A household *triggers* a rule when its walk reaches that question, picks
  that option and the routing engine takes the declared jump.
* A household *counts for coverage* only when its current path is fully
  answered and every single-choice answer on the path is a legal option.
* A household's *state signature* summarises the walked path together with
  every effective answer (and the illegal-answer flag); it changes whenever
  the path or an effective answer changes, even if an edit is later undone.
"""

from __future__ import annotations

import hashlib
import json

import engine

MAX_SELECTED_RULES = 12


class ReviewError(ValueError):
    """The review request (rule selection, quota ...) is invalid."""


def explicit_rules(definition):
    """All explicit jump rules in stable order (question order, then option).

    Each item is ``{"index", "qid", "title", "answer", "target", "label"}``.
    The index is the rule's position in this list and is used everywhere as
    its identity.
    """
    rules = []
    for question in definition:
        qid = question["id"]
        for answer, target in (question.get("jumps") or {}).items():
            rules.append({
                "index": len(rules),
                "qid": qid,
                "title": question.get("title", qid),
                "answer": answer,
                "target": target,
                "label": "%s「%s」跳至 %s" % (qid, answer, target),
            })
    return rules


def household_state(definition, stored):
    """Walk one household and derive its review state.

    Returns ``{"steps", "eligible", "triggered", "signature", "reason"}``
    where ``triggered`` is a set of explicit-rule indices and ``reason``
    explains why an ineligible household cannot count.
    """
    rules = explicit_rules(definition)
    index_by_pick = {(rule["qid"], rule["answer"]): rule["index"]
                     for rule in rules}

    steps = engine.walk_stored(definition, stored)
    triggered = set()
    for step in steps:
        if step["jump_origin"]:
            idx = index_by_pick.get((step["qid"], step["value"]))
            if idx is not None:
                triggered.add(idx)

    missing = engine.first_missing(steps)
    invalid_step = next((step for step in steps if step["invalid"]), None)
    if missing is not None:
        eligible = False
        reason = "未答完（首个漏答题 %s）" % missing["qid"]
    elif invalid_step is not None:
        eligible = False
        reason = "题 %s 的单选答案「%s」不合法" % (
            invalid_step["qid"], invalid_step["value"])
    else:
        eligible = True
        reason = ""

    return {
        "steps": steps,
        "eligible": eligible,
        "triggered": triggered,
        "signature": state_signature(steps),
        "reason": reason,
    }


def state_signature(steps):
    """Hash of the walked path plus every effective answer.

    Prefixes and question ids pin down the path; values pin down the
    effective answers; the invalid flag catches edits that make a single
    answer illegal.  A blank answer shortens the step list, so stopping
    earlier changes the signature too.
    """
    payload = [
        {"qid": step["qid"], "prefix": list(step["prefix"]),
         "value": step["value"], "answered": step["answered"],
         "invalid": step["invalid"]}
        for step in steps
    ]
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def parse_selection(raw_indices, rule_count):
    """Validate the selected rule indices coming from the query string."""
    if not raw_indices:
        raise ReviewError("请至少勾选 1 条跳题规则")
    selected = []
    for raw in raw_indices:
        try:
            idx = int(raw)
        except (TypeError, ValueError):
            raise ReviewError("跳题规则编号无效：%s" % raw)
        if idx < 0 or idx >= rule_count:
            raise ReviewError("跳题规则编号超出范围：%s" % idx)
        if idx not in selected:
            selected.append(idx)
    if len(selected) > MAX_SELECTED_RULES:
        raise ReviewError("一次最多勾选 %d 条跳题规则（当前 %d 条）"
                          % (MAX_SELECTED_RULES, len(selected)))
    return selected


def parse_quota(raw):
    """The reviewer-capacity field; blank means unlimited."""
    if raw is None or str(raw).strip() == "":
        return None
    try:
        quota = int(str(raw).strip())
    except ValueError:
        raise ReviewError("还能复核的户数必须是整数")
    if quota < 0:
        raise ReviewError("还能复核的户数不能为负数")
    return quota


def _bits(indices, selected):
    bit = {idx: 1 << pos for pos, idx in enumerate(selected)}
    mask = 0
    for idx in indices:
        if idx in bit:
            mask |= bit[idx]
    return mask


def _minimum_cover(candidates):
    """Minimum set cover over ``candidates``.

    ``candidates`` is a list of ``(household_id, rule_bits)``.  Returns a
    mapping ``covered_bits -> sorted tuple of household ids`` holding the
    smallest household set for every reachable mask.  Ties are broken by the
    lexicographically smallest id tuple so the plan is deterministic.
    """
    dp = {0: ()}
    for household_id, rule_bits in candidates:
        if not rule_bits:
            continue
        for mask, chosen in list(dp.items()):
            new_mask = mask | rule_bits
            candidate = tuple(sorted(chosen + (household_id,)))
            previous = dp.get(new_mask)
            if previous is None or \
                    (len(candidate), candidate) < (len(previous), previous):
                dp[new_mask] = candidate
    return dp


def _best_within_quota(dp, target_bits, quota):
    """Among covers using at most ``quota`` households, the widest one."""
    best_mask = 0
    best_score = (0, 0)
    for mask, chosen in dp.items():
        if len(chosen) > quota:
            continue
        covered = (mask & target_bits).bit_count()
        # More covered rules first; deterministic tie: smaller mask value.
        score = (covered, -mask)
        if score > best_score:
            best_score = score
            best_mask = mask
    return best_mask


def plan_coverage(definition, households, selected, quota=None):
    """Build the minimum-reviewer coverage plan.

    ``households`` is a list of dictionaries with ``id``, ``household_no``,
    ``stored`` answers and ``confirmed_signature`` (``None`` when the
    household is not confirmed).  Everything is recomputed from those saved
    answers, so the caller controls the snapshot by reading them once.

    Confirmed eligible households count first; the set cover then picks the
    fewest *pending* households for the rules still missing.
    """
    rules = explicit_rules(definition)
    target_bits = (1 << len(selected)) - 1

    snapshot = []
    for household in households:
        state = household_state(definition, household["stored"])
        current_sig = state["signature"]
        confirmed = (household.get("confirmed_signature") is not None and
                     household["confirmed_signature"] == current_sig)
        snapshot.append({
            "id": household["id"],
            "household_no": household["household_no"],
            "eligible": state["eligible"],
            "confirmed": confirmed,
            "triggered": state["triggered"],
            "bits": _bits(state["triggered"], selected),
            "reason": state["reason"],
        })

    confirmed_households = [item for item in snapshot
                            if item["eligible"] and item["confirmed"]]
    pending = [item for item in snapshot
               if item["eligible"] and not item["confirmed"]]

    confirmed_bits = 0
    for item in confirmed_households:
        confirmed_bits |= item["bits"]
    remaining_bits = target_bits & ~confirmed_bits

    # Rules that still need coverage but no pending eligible household can
    # ever trigger them -> no candidate sample.
    pending_union = 0
    for item in pending:
        pending_union |= item["bits"]
    missing_bits = remaining_bits & ~pending_union
    coverable_bits = remaining_bits & ~missing_bits

    dp = _minimum_cover([(item["id"], item["bits"]) for item in pending])
    minimum_chosen = dp.get(coverable_bits, ())
    needed = len(minimum_chosen)
    shortfall = max(0, needed - quota) if quota is not None else 0

    if quota is not None and needed > quota:
        quota_mask = _best_within_quota(dp, coverable_bits, quota)
        quota_chosen = dp.get(quota_mask, ())
    else:
        quota_chosen = minimum_chosen

    by_id = {item["id"]: item for item in snapshot}

    def rule_list(bits):
        out = []
        for pos in range(len(selected)):
            if bits & (1 << pos):
                out.append(rules[selected[pos]])
        return out

    chosen_rows = []
    for order, household_id in enumerate(minimum_chosen, start=1):
        item = by_id[household_id]
        within = household_id in set(quota_chosen)
        chosen_rows.append({
            "order": order,
            "id": household_id,
            "household_no": item["household_no"],
            "within_quota": within,
            "rules": rule_list(item["bits"] & coverable_bits),
        })

    confirmed_rows = []
    for item in confirmed_households:
        covered = item["bits"] & target_bits
        if covered:
            confirmed_rows.append({
                "id": item["id"],
                "household_no": item["household_no"],
                "rules": rule_list(covered),
            })

    return {
        "rules": [rules[idx] for idx in selected],
        "confirmed_households": confirmed_rows,
        "confirmed_bits": confirmed_bits & target_bits,
        "missing_rules": rule_list(missing_bits),
        "minimum_households": chosen_rows,
        "needed": needed,
        "quota": quota,
        "shortfall": shortfall,
        "fully_covered": not (remaining_bits),
        "all_coverable_covered": not (coverable_bits),
        "pending_candidate_count": len(pending),
        "eligible_count": sum(1 for item in snapshot if item["eligible"]),
        "confirmed_count": len(confirmed_households),
    }

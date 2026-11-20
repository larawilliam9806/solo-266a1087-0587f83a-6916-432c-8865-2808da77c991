"""Tests for the review/coverage planning logic (framework free)."""

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))

import engine  # noqa: E402
import review  # noqa: E402


def make_definition():
    # Q1 routes self-buy (Q2) vs rental (Q5); Q2 has a jump on 采摘平台;
    # Q4 always jumps to Q7. Q3/Q6/Q7 are non-routing.
    return [
        {"id": "Q1", "title": "设备来源", "type": engine.SINGLE,
         "options": ["自购", "租赁"],
         "jumps": {"自购": "Q2", "租赁": "Q5"}},
        {"id": "Q2", "title": "自购设备类型", "type": engine.SINGLE,
         "options": ["喷药机", "割草机", "采摘平台"],
         "jumps": {"采摘平台": "Q7"}},
        {"id": "Q3", "title": "自购台数", "type": engine.NUMBER,
         "options": [], "jumps": {}},
        {"id": "Q4", "title": "是否享受购置补贴", "type": engine.SINGLE,
         "options": ["有", "无"], "jumps": {"有": "Q7", "无": "Q7"}},
        {"id": "Q5", "title": "租赁方式", "type": engine.SINGLE,
         "options": ["按年整租", "按季短租"], "jumps": {}},
        {"id": "Q6", "title": "租赁台数", "type": engine.NUMBER,
         "options": [], "jumps": {}},
        {"id": "Q7", "title": "年费用合计", "type": engine.NUMBER,
         "options": [], "jumps": {}},
    ]


def stored_for(definition, answers):
    _, stored = engine.import_row(definition, answers)
    return stored


# Rule indices as produced by review.explicit_rules.
R_Q1_SELF = 0   # Q1 自购 -> Q2
R_Q1_RENT = 1   # Q1 租赁 -> Q5
R_Q2_PLAT = 2   # Q2 采摘平台 -> Q7
R_Q4_YES = 3    # Q4 有 -> Q7
R_Q4_NO = 4     # Q4 无 -> Q7


def test_explicit_rules_list_each_option_separately():
    rules = review.explicit_rules(make_definition())
    assert [r["index"] for r in rules] == [0, 1, 2, 3, 4]
    assert (rules[R_Q1_RENT]["qid"], rules[R_Q1_RENT]["answer"],
            rules[R_Q1_RENT]["target"]) == ("Q1", "租赁", "Q5")
    # The two Q4 options are distinct rules even though they share a target.
    assert rules[R_Q4_YES]["answer"] == "有"
    assert rules[R_Q4_NO]["answer"] == "无"


def test_household_triggers_only_jumps_actually_taken():
    definition = make_definition()
    stored = stored_for(definition, {
        "Q1": "租赁", "Q5": "按年整租", "Q6": "3", "Q7": "8000"})
    state = review.household_state(definition, stored)
    assert state["eligible"] is True
    assert state["triggered"] == {R_Q1_RENT}

    # Full self-buy route, Q4=无.
    stored = stored_for(definition, {
        "Q1": "自购", "Q2": "喷药机", "Q3": "2", "Q4": "无", "Q7": "12000"})
    state = review.household_state(definition, stored)
    assert state["triggered"] == {R_Q1_SELF, R_Q4_NO}

    # Self-buy with the Q2 jump option.
    stored = stored_for(definition, {
        "Q1": "自购", "Q2": "采摘平台", "Q7": "5000"})
    state = review.household_state(definition, stored)
    assert state["triggered"] == {R_Q1_SELF, R_Q2_PLAT}


def test_incomplete_path_is_ineligible():
    definition = make_definition()
    stored = stored_for(definition, {"Q1": "自购"})  # stops at blank Q2
    state = review.household_state(definition, stored)
    assert state["eligible"] is False
    assert "Q2" in state["reason"]
    assert state["triggered"] == {R_Q1_SELF}  # still took the Q1 jump


def test_illegal_single_answer_is_ineligible():
    definition = make_definition()
    stored = stored_for(definition, {
        "Q1": "租赁", "Q5": "按年整租", "Q6": "2", "Q7": "6000"})
    # Corrupt the saved Q5 answer with an illegal option.
    stored[(("Q1",), "Q5")] = "不存在的方式"
    state = review.household_state(definition, stored)
    assert state["eligible"] is False
    assert "Q5" in state["reason"]


def test_signature_changes_on_path_and_answer_edits_and_revert():
    definition = make_definition()
    base = stored_for(definition, {
        "Q1": "租赁", "Q5": "按年整租", "Q6": "3", "Q7": "8000"})
    sig0 = review.household_state(definition, base)["signature"]

    # A different effective answer on the same path changes the signature.
    edited = dict(base)
    edited[(("Q1",), "Q5")] = "按季短租"
    sig1 = review.household_state(definition, edited)["signature"]
    assert sig1 != sig0

    # Reverting to the original value restores the signature, which is why a
    # revoked confirmation still needs a fresh human review: the application
    # never auto-restores it even though the data matches again.
    reverted = dict(edited)
    reverted[(("Q1",), "Q5")] = "按年整租"
    assert review.household_state(definition, reverted)["signature"] == sig0

    # A path change (branch switch) changes the signature.
    switched = dict(base)
    switched[((), "Q1")] = "自购"
    switched[(("Q1",), "Q2")] = "采摘平台"
    switched[(("Q1", "Q2"), "Q7")] = "9000"
    assert review.household_state(definition, switched)["signature"] != sig0


def test_plan_counts_confirmed_first_and_picks_minimum_pending():
    definition = make_definition()
    rental = stored_for(definition, {
        "Q1": "租赁", "Q5": "按年整租", "Q6": "3", "Q7": "8000"})
    rental2 = stored_for(definition, {
        "Q1": "租赁", "Q5": "按季短租", "Q6": "1", "Q7": "5000"})
    self_no = stored_for(definition, {
        "Q1": "自购", "Q2": "喷药机", "Q3": "2", "Q4": "无", "Q7": "12000"})
    self_yes = stored_for(definition, {
        "Q1": "自购", "Q2": "割草机", "Q3": "1", "Q4": "有", "Q7": "15000"})
    platform = stored_for(definition, {
        "Q1": "自购", "Q2": "采摘平台", "Q7": "4000"})
    incomplete = stored_for(definition, {"Q1": "自购"})

    households = [
        {"id": 1, "household_no": "H1", "stored": rental,
         "confirmed_signature": None},
        {"id": 2, "household_no": "H2", "stored": rental2,
         "confirmed_signature": None},
        {"id": 3, "household_no": "H3", "stored": self_no,
         "confirmed_signature": None},
        {"id": 4, "household_no": "H4", "stored": self_yes,
         "confirmed_signature": None},
        {"id": 5, "household_no": "H5", "stored": platform,
         "confirmed_signature": None},
        {"id": 6, "household_no": "H6", "stored": incomplete,
         "confirmed_signature": None},
    ]
    selected = [R_Q1_RENT, R_Q2_PLAT, R_Q4_YES, R_Q4_NO]

    plan = review.plan_coverage(definition, households, selected)
    # Q1-RENT can only be covered by a rental household (1 or 2); Q2-PLAT by
    # household 5; Q4-YES by 4; Q4-NO by 3 -> four pending households needed.
    assert plan["needed"] == 4
    assert set(row["id"] for row in plan["minimum_households"]) == {1, 3, 4, 5}
    assert plan["missing_rules"] == []
    assert plan["fully_covered"] is False
    assert plan["eligible_count"] == 5
    assert plan["confirmed_count"] == 0

    # Now confirm rental H1: it covers Q1-RENT up front, leaving three.
    sig1 = review.household_state(definition, rental)["signature"]
    households[0]["confirmed_signature"] = sig1
    plan = review.plan_coverage(definition, households, selected)
    assert plan["confirmed_count"] == 1
    assert [row["id"] for row in plan["confirmed_households"]] == [1]
    assert plan["needed"] == 3
    assert set(row["id"] for row in plan["minimum_households"]) == {3, 4, 5}

    # A stale confirmation (signature no longer matches saved answers) must
    # not count.
    households[0]["confirmed_signature"] = "stale-signature"
    plan = review.plan_coverage(definition, households, selected)
    assert plan["confirmed_count"] == 0
    assert plan["needed"] == 4


def test_plan_reports_missing_sample_rules():
    definition = make_definition()
    rental = stored_for(definition, {
        "Q1": "租赁", "Q5": "按年整租", "Q6": "3", "Q7": "8000"})
    households = [{"id": 1, "household_no": "H1", "stored": rental,
                   "confirmed_signature": None}]
    # No self-buy household exists: the Q2/Q4 rules have no candidate.
    plan = review.plan_coverage(
        definition, households, [R_Q1_RENT, R_Q2_PLAT, R_Q4_YES])
    missing = {(rule["qid"], rule["answer"])
               for rule in plan["missing_rules"]}
    assert missing == {("Q2", "采摘平台"), ("Q4", "有")}
    # The coverable rule still gets its minimum plan.
    assert plan["needed"] == 1
    assert plan["minimum_households"][0]["id"] == 1


def test_plan_quota_shortfall_and_best_effort():
    definition = make_definition()
    rental = stored_for(definition, {
        "Q1": "租赁", "Q5": "按年整租", "Q6": "3", "Q7": "8000"})
    self_no = stored_for(definition, {
        "Q1": "自购", "Q2": "喷药机", "Q3": "2", "Q4": "无", "Q7": "12000"})
    platform = stored_for(definition, {
        "Q1": "自购", "Q2": "采摘平台", "Q7": "4000"})
    households = [
        {"id": 1, "household_no": "H1", "stored": rental,
         "confirmed_signature": None},
        {"id": 2, "household_no": "H2", "stored": self_no,
         "confirmed_signature": None},
        {"id": 3, "household_no": "H3", "stored": platform,
         "confirmed_signature": None},
    ]
    selected = [R_Q1_RENT, R_Q2_PLAT, R_Q4_NO]
    plan = review.plan_coverage(definition, households, selected, quota=2)
    assert plan["needed"] == 3
    assert plan["shortfall"] == 1
    within = [row["id"] for row in plan["minimum_households"]
              if row["within_quota"]]
    assert len(within) == 2

    # Generous quota -> no shortfall, all rows within quota.
    plan = review.plan_coverage(definition, households, selected, quota=5)
    assert plan["shortfall"] == 0
    assert all(row["within_quota"] for row in plan["minimum_households"])


def test_plan_deterministic_tie_breaking():
    definition = make_definition()
    rental_a = stored_for(definition, {
        "Q1": "租赁", "Q5": "按年整租", "Q6": "3", "Q7": "8000"})
    rental_b = stored_for(definition, {
        "Q1": "租赁", "Q5": "按季短租", "Q6": "1", "Q7": "5000"})
    households = [
        {"id": 10, "household_no": "H10", "stored": rental_a,
         "confirmed_signature": None},
        {"id": 3, "household_no": "H3", "stored": rental_b,
         "confirmed_signature": None},
    ]
    plan = review.plan_coverage(definition, households, [R_Q1_RENT])
    # Either household alone covers the rule; the smaller id wins.
    assert [row["id"] for row in plan["minimum_households"]] == [3]


def test_selection_validation():
    rules = review.explicit_rules(make_definition())
    assert review.parse_selection(["1", "4"], len(rules)) == [1, 4]
    with pytest.raises(review.ReviewError):
        review.parse_selection([], len(rules))
    with pytest.raises(review.ReviewError):
        review.parse_selection(["99"], len(rules))
    with pytest.raises(review.ReviewError):
        review.parse_selection(["x"], len(rules))
    too_many = [str(i) for i in range(review.MAX_SELECTED_RULES + 1)]
    with pytest.raises(review.ReviewError):
        review.parse_selection(too_many, len(rules) * 3)


def test_quota_validation():
    assert review.parse_quota("") is None
    assert review.parse_quota(None) is None
    assert review.parse_quota("3") == 3
    with pytest.raises(review.ReviewError):
        review.parse_quota("-1")
    with pytest.raises(review.ReviewError):
        review.parse_quota("abc")

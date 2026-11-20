"""End-to-end tests for the paper-roll review / jump-coverage workflow."""

import io
import json
import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SURVEY_DB", str(tmp_path / "survey.sqlite3"))
    monkeypatch.setenv("SURVEY_SECRET", "test")
    for module in ("storage", "engine", "importer", "main", "review"):
        sys.modules.pop(module, None)
    import main
    main.app.config.update(TESTING=True)
    with main.app.test_client() as test_client:
        yield test_client


CSV_TEXT = """户号,Q1 设备来源,Q2 自购设备类型,Q3 自购台数,Q4 是否享受购置补贴,Q5 租赁方式,Q6 租赁台数,Q7 年费用合计（元）
H1,自购,喷药机,2,无,,,12000
H2,租赁,喷药机,2,有,按年整租,3,8000
H3,自购,割草机,1,有,,,15000
H4,租赁,,,有,按季短租,2,6000
H5,自购,,,无,,,9000
H6,自购,采摘平台,,有,,7000
"""

JUMPS = {
    "order": ["Q1", "Q2", "Q3", "Q4", "Q5", "Q6", "Q7"],
    "titles": {
        "Q1": "设备来源", "Q2": "自购设备类型", "Q3": "自购台数",
        "Q4": "是否享受购置补贴", "Q5": "租赁方式", "Q6": "租赁台数",
        "Q7": "年费用合计（元）",
    },
    "types": {"Q1": "single", "Q2": "single", "Q4": "single",
              "Q5": "single", "Q3": "number", "Q6": "number",
              "Q7": "number"},
    "options": {"Q1": ["自购", "租赁"], "Q2": ["喷药机", "割草机", "采摘平台"],
                "Q4": ["有", "无"], "Q5": ["按年整租", "按季短租"]},
    "jumps": {"Q1": {"自购": "Q2", "租赁": "Q5"},
              "Q2": {"采摘平台": "Q7"},
              "Q4": {"有": "Q7", "无": "Q7"}},
}

# Rule indices (question order, option order):
# 0 Q1 自购->Q2, 1 Q1 租赁->Q5, 2 Q2 采摘平台->Q7,
# 3 Q4 有->Q7, 4 Q4 无->Q7


def upload(client):
    response = client.post("/upload", data={
        "csv_file": (io.BytesIO(CSV_TEXT.encode("utf-8")), "answers.csv"),
        "json_file": (io.BytesIO(json.dumps(JUMPS).encode("utf-8")),
                      "jumps.json"),
        "batch_name": "复核批次",
    }, content_type="multipart/form-data", follow_redirects=True)
    assert response.status_code == 200
    return response.get_data(as_text=True)


def signature_from_page(body):
    match = re.search(r'name="state_signature"\s+value="([^"]+)"', body)
    assert match, "confirm form missing"
    return match.group(1)


def confirm(client, household_id, signature):
    return client.post(
        "/batch/1/household/%d/confirm" % household_id,
        data={"state_signature": signature}, follow_redirects=True)


def test_batch_page_lists_explicit_rules_and_eligibility(client):
    body = upload(client)
    # All five explicit rules are selectable.
    for label in ("Q1", "租赁", "Q5", "采摘平台"):
        assert label in body
    checkboxes = re.findall(r'name="rule"\s+value="(\d+)"', body)
    assert checkboxes == ["0", "1", "2", "3", "4"]
    # H5 stops at blank Q2 -> cannot count; H4 has a complete rental path.
    assert "不可计入" in body


def test_confirm_household_and_it_counts_first(client):
    upload(client)
    # H2 (id 2) is a complete rental household.
    page = client.get("/batch/1/household/2").get_data(as_text=True)
    assert "可计入覆盖" in page
    sig = signature_from_page(page)
    body = confirm(client, 2, sig).get_data(as_text=True)
    assert "已确认户号 H2" in body
    assert "badge ok\">已确认" in body

    batch_page = client.get("/batch/1").get_data(as_text=True)
    assert "badge ok\">已确认" in batch_page

    # Coverage for the rental rule: H2 is confirmed, no pending household
    # needed.
    resp = client.get("/batch/1/coverage?rule=1")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "已确认户先计入" in body
    assert "无需再复核新户" in body
    assert "H2" in body


def test_stale_signature_is_rejected(client):
    upload(client)
    page = client.get("/batch/1/household/2").get_data(as_text=True)
    sig = signature_from_page(page)

    # Another save changes the answers before the reviewer confirms.
    client.post("/batch/1/household/2", data={
        "k_qid_1": "Q1", "k_prefix_1": "[]", "v_1": "租赁",
        "k_qid_2": "Q5", "k_prefix_2": '["Q1"]', "v_2": "按季短租",
        "k_qid_3": "Q6", "k_prefix_3": '["Q1", "Q5"]', "v_3": "2",
        "k_qid_4": "Q7", "k_prefix_4": '["Q1", "Q5", "Q6"]', "v_4": "6000",
    })
    body = confirm(client, 2, sig).get_data(as_text=True)
    assert "已拒绝确认" in body
    assert "请重新查看" in body
    # Nothing was confirmed: the household stays pending (no confirmed badge).
    household_page = client.get("/batch/1/household/2").get_data(as_text=True)
    assert "badge ok\">已确认" not in household_page
    assert ">待复核<" in household_page


def test_successful_path_edit_revokes_confirmation(client):
    upload(client)
    page = client.get("/batch/1/household/2").get_data(as_text=True)
    sig = signature_from_page(page)
    confirm(client, 2, sig)
    assert "badge ok\">已确认" in client.get("/batch/1").get_data(as_text=True)

    # Change Q5 to the other rental option: same path, different answer.
    body = client.post("/batch/1/household/2", data={
        "k_qid_1": "Q1", "k_prefix_1": "[]", "v_1": "租赁",
        "k_qid_2": "Q5", "k_prefix_2": '["Q1"]', "v_2": "按季短租",
        "k_qid_3": "Q6", "k_prefix_3": '["Q1", "Q5"]', "v_3": "3",
        "k_qid_4": "Q7", "k_prefix_4": '["Q1", "Q5", "Q6"]', "v_4": "8000",
    }, follow_redirects=True).get_data(as_text=True)
    assert "复核确认已作废" in body
    assert ">待复核<" in body
    assert "badge ok\">已确认" not in body

    # Reverting the answer does not restore the confirmation.
    body = client.post("/batch/1/household/2", data={
        "k_qid_1": "Q1", "k_prefix_1": "[]", "v_1": "租赁",
        "k_qid_2": "Q5", "k_prefix_2": '["Q1"]', "v_2": "按年整租",
        "k_qid_3": "Q6", "k_prefix_3": '["Q1", "Q5"]', "v_3": "3",
        "k_qid_4": "Q7", "k_prefix_4": '["Q1", "Q5", "Q6"]', "v_4": "8000",
    }, follow_redirects=True).get_data(as_text=True)
    assert ">待复核<" in body
    assert "badge ok\">已确认" not in body


def test_failed_save_keeps_confirmation(client, monkeypatch):
    upload(client)
    sig = signature_from_page(
        client.get("/batch/1/household/2").get_data(as_text=True))
    confirm(client, 2, sig)

    import main
    def explode(connection, household_id, changes, revoke_confirmation=False):
        raise RuntimeError("disk full")
    monkeypatch.setattr(main.storage, "save_answers", explode)

    response = client.post("/batch/1/household/2", data={
        "k_qid_1": "Q1", "k_prefix_1": "[]", "v_1": "租赁",
        "k_qid_2": "Q5", "k_prefix_2": '["Q1"]', "v_2": "按季短租",
    }, follow_redirects=True)
    assert "保存失败" in response.get_data(as_text=True)

    # The confirmation is still valid.
    body = client.get("/batch/1/household/2").get_data(as_text=True)
    assert "badge ok\">已确认" in body
    plan = client.get("/batch/1/coverage?rule=1").get_data(as_text=True)
    assert "无需再复核新户" in plan


def test_minimum_plan_missing_sample_and_quota(client):
    upload(client)
    # Rules 3 (Q4 有, H3) and 4 (Q4 无, H1) need two different households;
    # a quota of 1 leaves a shortfall of one and marks the extra row.
    resp = client.get("/batch/1/coverage?rule=3&rule=4&quota=1")
    body = resp.get_data(as_text=True)
    assert re.search(r"还差 1 户", body)
    assert "超名额" in body

    # Rule 2 genuinely has no candidate once H6 (the only 采摘平台 household)
    # is made ineligible: clearing its Q7 stops its path before completion.
    client.post("/batch/1/household/6", data={
        "k_qid_1": "Q1", "k_prefix_1": "[]", "v_1": "自购",
        "k_qid_2": "Q2", "k_prefix_2": '["Q1"]', "v_2": "采摘平台",
        "k_qid_3": "Q7", "k_prefix_3": '["Q1", "Q2"]', "v_3": "",
    })
    body = client.get("/batch/1/coverage?rule=2").get_data(as_text=True)
    assert "缺样：没有候选户的规则" in body
    assert "采摘平台" in body

    # No rule selected -> validation redirect with a flash.
    resp = client.get("/batch/1/coverage")
    assert resp.status_code == 302


def test_ineligible_households_are_excluded_from_plan(client):
    upload(client)
    # H5 (id 5) stops at blank Q2: it took the Q1 自购 jump but cannot count.
    # Rule 0 (Q1 自购) still has eligible H1/H3/H6 candidates.
    body = client.get("/batch/1/coverage?rule=0").get_data(as_text=True)
    # One eligible self-buy household suffices; H5 must not appear as a pick.
    pick_rows = re.findall(r"<td class=\"strong\">\s*<a[^>]*>(H\d+)</a>",
                           body)
    assert "H5" not in pick_rows
    assert pick_rows  # some eligible household was chosen


def test_more_than_twelve_rules_rejected(client):
    # A questionnaire whose single question jumps on 13 options gives 13
    # explicit rules; selecting all of them exceeds the cap of 12.
    jumps = {
        "order": ["Q1", "Q2"],
        "types": {"Q1": "single"},
        "options": {"Q1": ["o%d" % i for i in range(13)]},
        "jumps": {"Q1": {"o%d" % i: "Q2" for i in range(13)}},
    }
    csv_text = "户号,Q1,Q2\n" + "\n".join(
        "H%d,o%d,x" % (i, i) for i in range(13)) + "\n"
    client.post("/upload", data={
        "csv_file": (io.BytesIO(csv_text.encode("utf-8")), "big.csv"),
        "json_file": (io.BytesIO(json.dumps(jumps).encode("utf-8")),
                      "big.json"),
    }, content_type="multipart/form-data", follow_redirects=True)
    # The upload runs in the same client/database as the fixture batch.
    batch_id = 2 if client.get("/batch/2").status_code == 200 else 1

    thirteen = "&".join("rule=%d" % i for i in range(13))
    response = client.get("/batch/%d/coverage?%s" % (batch_id, thirteen),
                          follow_redirects=True)
    assert "一次最多勾选 12 条" in response.get_data(as_text=True)

    twelve = "&".join("rule=%d" % i for i in range(12))
    assert client.get(
        "/batch/%d/coverage?%s" % (batch_id, twelve)).status_code == 200


def test_coverage_links_open_household_pages(client):
    upload(client)
    body = client.get("/batch/1/coverage?rule=1").get_data(as_text=True)
    # Pending rental candidates H2 and H4 link to their household pages.
    links = re.findall(r'href="(/batch/1/household/\d+)"', body)
    assert links
    for link in dict.fromkeys(links):
        assert client.get(link).status_code == 200


def test_coverage_uses_saved_snapshot_not_live_edits(client):
    upload(client)
    sig = signature_from_page(
        client.get("/batch/1/household/2").get_data(as_text=True))
    confirm(client, 2, sig)
    # Plan computed against the snapshot: rental rule fully covered.
    body = client.get("/batch/1/coverage?rule=1").get_data(as_text=True)
    assert "无需再复核新户" in body

    # Revoke by an edit AFTER opening/using the plan; the rendered plan keeps
    # its snapshot wording until recomputed.
    client.post("/batch/1/household/2", data={
        "k_qid_1": "Q1", "k_prefix_1": "[]", "v_1": "租赁",
        "k_qid_2": "Q5", "k_prefix_2": '["Q1"]', "v_2": "按季短租",
        "k_qid_3": "Q6", "k_prefix_3": '["Q1", "Q5"]', "v_3": "3",
        "k_qid_4": "Q7", "k_prefix_4": '["Q1", "Q5", "Q6"]', "v_4": "8000",
    })
    body = client.get("/batch/1/household/2").get_data(as_text=True)
    assert ">待复核<" in body
    assert "badge ok\">已确认" not in body
    # Recomputing now treats H2 as pending again.
    body = client.get("/batch/1/coverage?rule=1").get_data(as_text=True)
    assert "无需再复核新户" not in body


def test_batch_page_flags_household_with_stale_confirmation_row(client):
    upload(client)
    # Confirm H2, then inject a confirmation row whose signature no longer
    # matches (simulating an externally edited answer): the batch page and
    # household page must show the stale state rather than "已确认".
    page = client.get("/batch/1/household/2").get_data(as_text=True)
    sig = signature_from_page(page)
    confirm(client, 2, sig)
    import storage
    conn = storage.connect(storage.db_path_from_env())
    conn.execute("UPDATE confirmations SET signature = ? WHERE household_id = 2",
                 ("old-signature",))
    conn.commit()
    conn.close()

    body = client.get("/batch/1/household/2").get_data(as_text=True)
    assert "原确认已作废" in body
    assert "badge ok\">已确认" not in body
    batch_page = client.get("/batch/1").get_data(as_text=True)
    assert "确认已作废" in batch_page
    # A stale confirmation does not count towards coverage.
    plan = client.get("/batch/1/coverage?rule=1").get_data(as_text=True)
    assert "无需再复核新户" not in plan

from __future__ import annotations

import os
import time
from pathlib import Path

from fastapi.testclient import TestClient


def make_user(client, admin, username: str, permissions: list[str]) -> dict:
    role = client.post(
        "/api/roles",
        headers=admin["headers"],
        json={"code": f"role.{username}", "name": f"角色{username}", "permission_codes": permissions},
    )
    assert role.status_code == 201, role.text
    user = client.post(
        "/api/users",
        headers=admin["headers"],
        json={"username": username, "password": "Tester!23456", "display_name": username, "role_codes": [f"role.{username}"]},
    )
    assert user.status_code == 201, user.text
    login = client.post("/api/auth/login", json={"username": username, "password": "Tester!23456", "client_label": "tests"})
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['token']}"}


def add_lot(client, code: str, supplier: str, quantity_kg: float) -> dict:
    response = client.post(
        "/api/food/lots",
        json={
            "lot_code": code,
            "product_name": "菠菜",
            "category": "叶菜",
            "supplier": supplier,
            "origin": "山东寿光",
            "harvest_date": "2026-09-20",
            "quantity_kg": quantity_kg,
            "trace_code": code + "-TRACE",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def add_result(client, lot_id: int, sample_code: str, analyte: str, value: float, limit: float) -> dict:
    sample = client.post(
        f"/api/food/lots/{lot_id}/samples",
        json={"sample_code": sample_code, "collected_at": "2026-09-21T08:00:00+00:00", "collector": "监管员", "location": "批发市场", "sample_weight_g": 250},
    )
    assert sample.status_code == 201, sample.text
    result = client.post(
        f"/api/food/samples/{sample.json()['id']}/results",
        json={"analyte": analyte, "method": "GB/T 5009", "value_mg_kg": value, "limit_mg_kg": limit, "lab_operator": "实验员", "tested_at": "2026-09-21T18:00:00+00:00"},
    )
    assert result.status_code == 201, result.text
    return result.json()


def create_rehearsal(client, headers, **overrides) -> dict:
    payload = {"title": "毒死蜱限值收紧预演", "limits": {"毒死蜱": 0.01}, "sampling_per_ton": 4.0}
    payload.update(overrides)
    response = client.post("/api/food/rule-rehearsals", headers=headers, json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def test_rehearsal_report_diffs_suppliers_and_resampling(client, admin):
    lot_a = add_lot(client, "LOT-A", "安心农场", 500)  # 合格批次，候选规则下将转为待复核
    add_result(client, lot_a["id"], "S-A1", "毒死蜱", 0.04, 0.05)
    lot_b = add_lot(client, "LOT-B", "绿源基地", 2500)  # 本就不合格，结论不变
    add_result(client, lot_b["id"], "S-B1", "毒死蜱", 0.2, 0.05)
    lot_c = add_lot(client, "LOT-C", "安心农场", 300)  # 无检测结果，只受抽检比例影响

    before_a = client.get(f"/api/food/lots/{lot_a['id']}").json()

    rehearsal = create_rehearsal(client, admin["headers"])
    assert rehearsal["status"] == "computed"
    assert rehearsal["base_version_no"] == 1
    report = rehearsal["report"]

    totals = report["totals"]
    assert totals["lots_evaluated"] == 3
    assert totals["pass_to_review"] == 1
    assert totals["review_to_pass"] == 0
    assert totals["lots_with_conclusion_change"] == 1
    # 补采量：LOT-A 需 2 实有 1 → 1；LOT-B 需 10 实有 1 → 9；LOT-C 需 2 实有 0 → 2
    assert totals["additional_samples_for_ratio"] == 12
    assert totals["retest_samples_for_review"] == 1
    assert totals["total_additional_tests"] == 13

    diffs = report["conclusion_diffs"]
    assert len(diffs) == 1
    diff = diffs[0]
    assert diff["lot_code"] == "LOT-A"
    assert diff["current_conclusion"] == "pass"
    assert diff["candidate_conclusion"] == "review"
    changed = diff["changed_results"][0]
    assert changed["analyte"] == "毒死蜱"
    assert changed["current_limit"] == 0.05
    assert changed["candidate_limit"] == 0.01
    assert changed["current_verdict"] == "pass"
    assert changed["candidate_verdict"] == "fail"

    suppliers = {item["supplier"]: item for item in report["affected_suppliers"]}
    assert suppliers["安心农场"]["lots_changed"] == 1
    assert suppliers["安心农场"]["additional_samples"] == 3  # LOT-A 1 + LOT-C 2
    assert suppliers["绿源基地"]["lots_changed"] == 0
    assert suppliers["绿源基地"]["additional_samples"] == 9

    resampling = {item["lot_code"]: item for item in report["resampling"]}
    assert resampling["LOT-A"]["additional"] == 1
    assert resampling["LOT-B"]["additional"] == 9
    assert resampling["LOT-C"]["additional"] == 2

    permission_check = report["permission_check"]
    assert permission_check["generated_for"] == "系统管理员"
    assert permission_check["steps"][0]["step"] == "rehearse"
    assert permission_check["steps"][0]["satisfied"] is True
    assert permission_check["steps"][1]["satisfied"] is None

    # 预演不得修改当前结论：批次状态与检测结果保持原样
    after_a = client.get(f"/api/food/lots/{lot_a['id']}").json()
    assert after_a["status"] == before_a["status"]
    assert after_a["risk_level"] == before_a["risk_level"]
    assert after_a["samples"][0]["results"][0]["verdict"] == "pass"
    current = client.get("/api/food/rule-sets/current", headers=admin["headers"]).json()
    assert current["version_no"] == 1
    assert current["limits"] == {}

    # 报告摘要与详情可查询
    listed = client.get("/api/food/rule-rehearsals", headers=admin["headers"]).json()
    assert len(listed) == 1
    assert listed[0]["report_summary"]["pass_to_review"] == 1
    detail = client.get(f"/api/food/rule-rehearsals/{rehearsal['id']}", headers=admin["headers"]).json()
    assert detail["code"] == rehearsal["code"]
    fetched = client.get(f"/api/food/rule-rehearsals/{rehearsal['id']}/report", headers=admin["headers"]).json()
    assert fetched["totals"] == totals


def test_approval_publish_flow_and_rule_versions(client, admin):
    lot = add_lot(client, "LOT-P", "安心农场", 500)
    add_result(client, lot["id"], "S-P1", "毒死蜱", 0.04, 0.05)
    approver = make_user(client, admin, "approver.one", ["food.rules.read", "food.rules.approve"])

    rehearsal = create_rehearsal(client, admin["headers"])

    # 未审批不能发布
    blocked = client.post(f"/api/food/rule-rehearsals/{rehearsal['id']}/publish", headers=admin["headers"])
    assert blocked.status_code == 409

    # 创建人不能审批自己的预演（职责分离）
    self_approve = client.post(
        f"/api/food/rule-rehearsals/{rehearsal['id']}/approval",
        headers=admin["headers"],
        json={"decision": "approve", "opinion": "自审"},
    )
    assert self_approve.status_code == 409

    approved = client.post(
        f"/api/food/rule-rehearsals/{rehearsal['id']}/approval",
        headers=approver,
        json={"decision": "approve", "opinion": "影响可接受，同意发布"},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved"
    assert approved.json()["approval_opinion"] == "影响可接受，同意发布"
    assert approved.json()["approvals"][0]["actor"] == "approver.one"

    # 重复审批被拒绝
    duplicate = client.post(
        f"/api/food/rule-rehearsals/{rehearsal['id']}/approval",
        headers=approver,
        json={"decision": "approve", "opinion": "重复审批"},
    )
    assert duplicate.status_code == 409

    published = client.post(f"/api/food/rule-rehearsals/{rehearsal['id']}/publish", headers=admin["headers"])
    assert published.status_code == 200, published.text
    rule_set = published.json()["rule_set"]
    assert rule_set["version_no"] == 2
    assert rule_set["status"] == "active"
    assert rule_set["limits"] == {"毒死蜱": 0.01}
    assert rule_set["sampling_per_ton"] == 4.0
    assert rule_set["rehearsal_id"] == rehearsal["id"]
    assert published.json()["rehearsal"]["status"] == "published"

    # 重复发布被拒绝
    republish = client.post(f"/api/food/rule-rehearsals/{rehearsal['id']}/publish", headers=admin["headers"])
    assert republish.status_code == 409

    current = client.get("/api/food/rule-sets/current", headers=admin["headers"]).json()
    assert current["version_no"] == 2
    versions = client.get("/api/food/rule-sets", headers=admin["headers"]).json()
    assert [item["version_no"] for item in versions] == [2, 1]
    assert versions[1]["status"] == "superseded"

    # 历史检测结论不被发布动作改写
    detail = client.get(f"/api/food/lots/{lot['id']}").json()
    assert detail["samples"][0]["results"][0]["verdict"] == "pass"


def test_reject_is_terminal(client, admin):
    approver = make_user(client, admin, "approver.two", ["food.rules.approve"])
    rehearsal = create_rehearsal(client, admin["headers"])
    rejected = client.post(
        f"/api/food/rule-rehearsals/{rehearsal['id']}/approval",
        headers=approver,
        json={"decision": "reject", "opinion": "补采量过大，退回重算"},
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"
    assert rejected.json()["approval_opinion"] == "补采量过大，退回重算"

    again = client.post(
        f"/api/food/rule-rehearsals/{rehearsal['id']}/approval",
        headers=approver,
        json={"decision": "approve", "opinion": "改变主意"},
    )
    assert again.status_code == 409
    publish = client.post(f"/api/food/rule-rehearsals/{rehearsal['id']}/publish", headers=admin["headers"])
    assert publish.status_code == 409
    current = client.get("/api/food/rule-sets/current", headers=admin["headers"]).json()
    assert current["version_no"] == 1


def test_expired_candidate_cannot_be_approved_or_published(client, admin):
    approver = make_user(client, admin, "approver.three", ["food.rules.approve"])
    rehearsal = create_rehearsal(client, admin["headers"], expires_at="2020-01-01T00:00:00+00:00")
    assert rehearsal["status"] == "expired"

    approve = client.post(
        f"/api/food/rule-rehearsals/{rehearsal['id']}/approval",
        headers=approver,
        json={"decision": "approve", "opinion": "过期候选"},
    )
    assert approve.status_code == 409
    publish = client.post(f"/api/food/rule-rehearsals/{rehearsal['id']}/publish", headers=admin["headers"])
    assert publish.status_code == 409


def test_candidate_expires_lazily_between_create_and_approve(client, admin):
    approver = make_user(client, admin, "approver.four", ["food.rules.approve"])
    rehearsal = create_rehearsal(client, admin["headers"], expires_in_hours=0.0005)
    assert rehearsal["status"] == "computed"
    time.sleep(2)

    detail = client.get(f"/api/food/rule-rehearsals/{rehearsal['id']}", headers=admin["headers"]).json()
    assert detail["status"] == "expired"
    approve = client.post(
        f"/api/food/rule-rehearsals/{rehearsal['id']}/approval",
        headers=approver,
        json={"decision": "approve", "opinion": "已过期"},
    )
    assert approve.status_code == 409
    current = client.get("/api/food/rule-sets/current", headers=admin["headers"]).json()
    assert current["version_no"] == 1


def test_approved_candidate_expiring_before_publish_cannot_be_published(client, admin):
    approver = make_user(client, admin, "approver.six", ["food.rules.approve"])
    rehearsal = create_rehearsal(client, admin["headers"], expires_in_hours=0.0005)
    approved = client.post(
        f"/api/food/rule-rehearsals/{rehearsal['id']}/approval",
        headers=approver,
        json={"decision": "approve", "opinion": "同意，但发布前会过期"},
    )
    assert approved.status_code == 200
    time.sleep(2)

    publish = client.post(f"/api/food/rule-rehearsals/{rehearsal['id']}/publish", headers=admin["headers"])
    assert publish.status_code == 409
    detail = client.get(f"/api/food/rule-rehearsals/{rehearsal['id']}", headers=admin["headers"]).json()
    assert detail["status"] == "expired"
    current = client.get("/api/food/rule-sets/current", headers=admin["headers"]).json()
    assert current["version_no"] == 1


def test_stale_base_blocks_second_publish(client, admin):
    approver = make_user(client, admin, "approver.five", ["food.rules.approve"])
    first = create_rehearsal(client, admin["headers"], title="第一次调整")
    second = create_rehearsal(client, admin["headers"], title="第二次调整", limits={"氯氰菊酯": 0.02})
    for rehearsal in (first, second):
        response = client.post(
            f"/api/food/rule-rehearsals/{rehearsal['id']}/approval",
            headers=approver,
            json={"decision": "approve", "opinion": "同意"},
        )
        assert response.status_code == 200

    published = client.post(f"/api/food/rule-rehearsals/{first['id']}/publish", headers=admin["headers"])
    assert published.status_code == 200
    stale = client.post(f"/api/food/rule-rehearsals/{second['id']}/publish", headers=admin["headers"])
    assert stale.status_code == 409
    assert "基准规则版本已变化" in stale.json()["error"]["message"]
    current = client.get("/api/food/rule-sets/current", headers=admin["headers"]).json()
    assert current["version_no"] == 2
    assert current["limits"] == {"毒死蜱": 0.01}


def test_permissions_are_enforced(client, admin):
    outsider = make_user(client, admin, "outsider.one", ["residents.read"])
    rehearsal_only = make_user(client, admin, "rehearser.one", ["food.rules.rehearse"])

    denied = client.post(
        "/api/food/rule-rehearsals",
        headers=outsider,
        json={"title": "无权限预演", "limits": {"毒死蜱": 0.01}, "sampling_per_ton": 2.0},
    )
    assert denied.status_code == 403
    assert client.get("/api/food/rule-rehearsals", headers=outsider).status_code == 403
    assert client.get("/api/food/rule-sets/current", headers=outsider).status_code == 403
    assert client.get("/api/food/rule-rehearsals").status_code == 401

    created = client.post(
        "/api/food/rule-rehearsals",
        headers=rehearsal_only,
        json={"title": "仅预演权限", "limits": {"毒死蜱": 0.01}, "sampling_per_ton": 2.0},
    )
    assert created.status_code == 201, created.text
    rehearsal_id = created.json()["id"]
    approve = client.post(
        f"/api/food/rule-rehearsals/{rehearsal_id}/approval",
        headers=rehearsal_only,
        json={"decision": "approve", "opinion": "越权审批"},
    )
    assert approve.status_code == 403
    publish = client.post(f"/api/food/rule-rehearsals/{rehearsal_id}/publish", headers=rehearsal_only)
    assert publish.status_code == 403


def test_restart_keeps_unfinished_rehearsal_inactive(tmp_path: Path):
    os.environ["TOWNSHIP_DATABASE_PATH"] = str(tmp_path / "restart.db")
    from app.database import close_connection
    from app.main import app

    close_connection()
    with TestClient(app) as client:
        response = client.post("/api/auth/bootstrap", json={"username": "admin", "password": "Admin!23456", "client_label": "tests"})
        assert response.status_code == 201, response.text
        login = client.post("/api/auth/login", json={"username": "admin", "password": "Admin!23456", "client_label": "tests"})
        admin_headers = {"Authorization": f"Bearer {login.json()['token']}"}
        approver = make_user(client, {"headers": admin_headers}, "restart.approver", ["food.rules.approve"])
        rehearsal = create_rehearsal(client, admin_headers)
        approved = client.post(
            f"/api/food/rule-rehearsals/{rehearsal['id']}/approval",
            headers=approver,
            json={"decision": "approve", "opinion": "重启前审批"},
        )
        assert approved.status_code == 200
    close_connection()

    # 模拟服务重启：已审批未发布的预演不得被当成已生效
    with TestClient(app) as client:
        login = client.post("/api/auth/login", json={"username": "admin", "password": "Admin!23456", "client_label": "tests"})
        admin_headers = {"Authorization": f"Bearer {login.json()['token']}"}
        current = client.get("/api/food/rule-sets/current", headers=admin_headers).json()
        assert current["version_no"] == 1
        assert current["source"] == "seed"
        detail = client.get(f"/api/food/rule-rehearsals/{rehearsal['id']}", headers=admin_headers).json()
        assert detail["status"] == "approved"
        assert detail["published_rule_set_id"] is None

        published = client.post(f"/api/food/rule-rehearsals/{rehearsal['id']}/publish", headers=admin_headers)
        assert published.status_code == 200
        assert published.json()["rule_set"]["version_no"] == 2
    close_connection()

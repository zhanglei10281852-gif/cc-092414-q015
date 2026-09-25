from __future__ import annotations

from fastapi.testclient import TestClient

from app.core.clock import FrozenClock, to_storage, utc_now
from app.core.security import Principal
from app.database import get_connection
from app.food.governance import RuleGovernanceService
from app.food.service import FoodService
from datetime import UTC, datetime, timedelta


# ---------- 辅助 ----------

def make_role_and_user(client: TestClient, admin_headers: dict, username: str, permissions: list[str]) -> dict:
    role_code = f"role_{username}"
    role = client.post("/api/roles", json={"code": role_code, "name": username, "permission_codes": permissions}, headers=admin_headers)
    assert role.status_code == 201, role.text
    created = client.post(
        "/api/users",
        json={"username": username, "password": "Role!123456", "display_name": username, "role_codes": [role_code]},
        headers=admin_headers,
    )
    assert created.status_code == 201, created.text
    login = client.post("/api/auth/login", json={"username": username, "password": "Role!123456", "client_label": "t"})
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['token']}"}


def planner_approver(client: TestClient, admin: dict) -> tuple[dict, dict]:
    planner = make_role_and_user(client, admin["headers"], "planner", ["food.rules.read", "food.rules.rehearse"])
    approver = make_role_and_user(client, admin["headers"], "approver", ["food.rules.read", "food.rules.approve"])
    return planner, approver


def seed_lot(client: TestClient, code: str, supplier: str, category: str = "叶菜", quantity_kg: float = 1000.0) -> int:
    lot = client.post(
        "/api/food/lots",
        json={"lot_code": code, "product_name": "菠菜", "category": category, "supplier": supplier,
              "origin": "山东寿光", "harvest_date": "2026-09-20", "quantity_kg": quantity_kg, "trace_code": code + "-T"},
    )
    assert lot.status_code == 201, lot.text
    return lot.json()["id"]


def add_pass_result(client: TestClient, lot_id: int, sample_code: str, analyte: str, value: float, stored_limit: float) -> None:
    sample = client.post(f"/api/food/lots/{lot_id}/samples", json={
        "sample_code": sample_code, "collected_at": "2026-09-21T08:00:00+00:00",
        "collector": "监管员", "location": "批发市场", "sample_weight_g": 250})
    assert sample.status_code == 201, sample.text
    result = client.post(f"/api/food/samples/{sample.json()['id']}/results", json={
        "analyte": analyte, "method": "GB/T 5009", "value_mg_kg": value, "limit_mg_kg": stored_limit,
        "lab_operator": "实验员", "tested_at": "2026-09-21T18:00:00+00:00"})
    assert result.status_code == 201, result.text


TIGHTER_RULE = {
    "code": "REH-001",
    "title": "毒死蜱加严与抽检提比例",
    "limits": [{"analyte": "毒死蜱", "category": None, "limit_mg_kg": 0.01}],
    "sampling": [{"scope": "global", "key": None, "rate": 0.001}],
    "selection": {"mode": "recent", "limit": 50},
    "expires_in_minutes": 1440,
}


# ---------- 预演不改动历史结论，报告内容完整 ----------

def test_rehearsal_recomputes_without_touching_conclusions(client, admin):
    lot_id = seed_lot(client, "LOT-R1", "安心农场")
    add_pass_result(client, lot_id, "S-R1", "毒死蜱", 0.02, 0.05)
    before = client.get(f"/api/food/lots/{lot_id}").json()

    response = client.post("/api/food/rule-governance/rehearsals", json=TIGHTER_RULE, headers=admin["headers"])
    assert response.status_code == 201, response.text
    rehearsal = response.json()
    assert rehearsal["status"] == "ready"
    assert rehearsal["report"] is None  # 列表型响应默认不内嵌完整报告

    after = client.get(f"/api/food/lots/{lot_id}").json()
    # 现行结论与检测记录一个字节都不能变
    assert after["status"] == before["status"]
    assert after["risk_level"] == before["risk_level"]
    assert after["samples"][0]["results"][0]["verdict"] == "pass"
    assert after["samples"][0]["results"][0]["limit_mg_kg"] == 0.05

    detail = client.get(f"/api/food/rule-governance/rehearsals/{rehearsal['id']}", headers=admin["headers"]).json()
    summary = detail["report_summary"]
    assert summary["lots_total"] == 1
    assert summary["pass_to_review"] == 1
    assert summary["review_to_pass"] == 0
    assert summary["suppliers_affected"] == 1
    # 1000kg * 0.1% = 1000g 目标采样，已采 250g，补 750g = 3 个样，每项次 30 分钟
    assert summary["total_suggested_extra_g"] == 750.0
    assert summary["additional_samples"] == 3
    assert summary["additional_analyte_tests"] == 3
    assert summary["estimated_lab_minutes"] == 90

    report = client.get(f"/api/food/rule-governance/rehearsals/{rehearsal['id']}/report", headers=admin["headers"]).json()["report"]
    diff = report["differences"][0]
    assert diff["lot_id"] == lot_id and diff["change"] == "pass_to_review"
    assert diff["triggers"][0]["current_limit_mg_kg"] == 0.05
    assert diff["triggers"][0]["candidate_limit_mg_kg"] == 0.01
    assert report["suppliers"][0]["supplier"] == "安心农场"
    assert report["suppliers"][0]["lots_affected"] == 1
    assert report["permission_check"]["created_by"]["permission"] == "food.rules.rehearse"
    assert report["permission_check"]["created_by"]["granted"] is True
    assert "food.rules.approve" in report["permission_check"]["approval_required"]


def test_unaffected_supplier_not_listed_and_relaxation_flips_back(client, admin):
    first = seed_lot(client, "LOT-A1", "安心农场")
    add_pass_result(client, first, "S-A1", "毒死蜱", 0.02, 0.05)
    second = seed_lot(client, "LOT-B1", "丰禾菜园", quantity_kg=100.0)
    add_pass_result(client, second, "S-B1", "氯氰菊酯", 0.01, 0.05)

    response = client.post("/api/food/rule-governance/rehearsals", json=TIGHTER_RULE, headers=admin["headers"])
    report = client.get(f"/api/food/rule-governance/rehearsals/{response.json()['id']}/report", headers=admin["headers"]).json()["report"]
    assert report["lots"]["pass_to_review"] == 1
    assert {item["supplier"] for item in report["suppliers"]} == {"安心农场"}

    # 放宽限值：基线限值下失败的批次（0.08 > 0.05）在候选 0.1 下变 pass
    third = seed_lot(client, "LOT-C1", "整改农场")
    add_pass_result(client, third, "S-C1", "毒死蜱", 0.08, 0.05)
    relax = {
        "code": "REH-RELAX",
        "limits": [{"analyte": "毒死蜱", "category": None, "limit_mg_kg": 0.1}],
        "selection": {"mode": "recent", "limit": 50},
        "expires_in_minutes": 60,
    }
    relaxed = client.post("/api/food/rule-governance/rehearsals", json=relax, headers=admin["headers"]).json()
    summary = relaxed["report_summary"]
    assert summary["review_to_pass"] == 1
    assert summary["pass_to_review"] == 0


def test_stored_verdict_disagreement_is_flagged(client, admin):
    """历史留档结论与按现行规则重算不一致时，报告必须暴露完整性疑点。"""
    lot_id = seed_lot(client, "LOT-M1", "疑点农场")
    add_pass_result(client, lot_id, "S-M1", "毒死蜱", 0.02, 0.05)
    # 人为制造一条留档结论与数值不符的历史记录（模拟历史责任问题）
    connection = get_connection()
    connection.execute("UPDATE food_test_results SET verdict='fail' WHERE sample_id=(SELECT id FROM food_samples WHERE lot_id=?)", (lot_id,))
    connection.commit()

    response = client.post("/api/food/rule-governance/rehearsals", json=TIGHTER_RULE, headers=admin["headers"])
    report = client.get(f"/api/food/rule-governance/rehearsals/{response.json()['id']}/report", headers=admin["headers"]).json()["report"]
    assert report["lots"]["integrity_mismatches"] == 1
    # 候选 0.01 下它确实失败，结论差异仍要报出来
    assert report["lots"]["pass_to_review"] == 1


# ---------- 权限 ----------

def test_permissions_enforced_and_segregation_of_duties(client, admin):
    lot_id = seed_lot(client, "LOT-P1", "安心农场")
    add_pass_result(client, lot_id, "S-P1", "毒死蜱", 0.02, 0.05)
    planner = make_role_and_user(client, admin["headers"], "planner",
                                 ["food.rules.read", "food.rules.rehearse", "food.rules.approve"])
    approver = make_role_and_user(client, admin["headers"], "approver", ["food.rules.read", "food.rules.approve"])
    nobody = make_role_and_user(client, admin["headers"], "nobody", [])

    # 未认证
    assert client.get("/api/food/rule-governance/current-version").status_code == 401
    # 无权限
    assert client.get("/api/food/rule-governance/current-version", headers=nobody).status_code == 403

    created = client.post("/api/food/rule-governance/rehearsals", json=TIGHTER_RULE, headers=planner)
    assert created.status_code == 201, created.text
    rehearsal_id = created.json()["id"]

    # 经办人不能审批
    forbidden = client.post(f"/api/food/rule-governance/rehearsals/{rehearsal_id}/decision",
                            json={"decision": "approved", "comment": "自批"}, headers=planner)
    assert forbidden.status_code == 403, forbidden.text
    # 审批权账号不能发起预演
    assert client.post("/api/food/rule-governance/rehearsals", json=TIGHTER_RULE, headers=approver).status_code == 403

    # 被职责分离拒绝的尝试也要留痕，且没有产生任何版本
    records = client.get(f"/api/food/rule-governance/rehearsals/{rehearsal_id}/approvals", headers=approver).json()
    assert len(records) == 1 and records[0]["result"] == "forbidden"
    versions = client.get("/api/food/rule-governance/versions", headers=approver).json()
    assert versions == []


# ---------- 审批发布：原子生效、历史不动 ----------

def test_approval_publishes_atomically_and_queryable(client, admin):
    planner, approver = planner_approver(client, admin)
    lot_id = seed_lot(client, "LOT-F1", "安心农场")
    add_pass_result(client, lot_id, "S-F1", "毒死蜱", 0.02, 0.05)

    created = client.post("/api/food/rule-governance/rehearsals", json=TIGHTER_RULE, headers=planner).json()
    # 发布前只有基线
    current = client.get("/api/food/rule-governance/current-version", headers=admin["headers"]).json()
    assert current["version"] == 0 and current["status"] == "baseline"

    decision = client.post(f"/api/food/rule-governance/rehearsals/{created['id']}/decision",
                           json={"decision": "approved", "comment": "同意按期发布"}, headers=approver)
    assert decision.status_code == 200, decision.text
    body = decision.json()
    assert body["status"] == "approved"
    assert body["published_version_id"] is not None

    current = client.get("/api/food/rule-governance/current-version", headers=admin["headers"]).json()
    assert current["version"] == 1 and current["status"] == "active"
    assert current["limits"][0]["analyte"] == "毒死蜱"
    assert current["published_by"] == "approver"
    versions = client.get("/api/food/rule-governance/versions", headers=admin["headers"]).json()
    assert len(versions) == 1 and versions[0]["status"] == "active"

    detail = client.get(f"/api/food/rule-governance/rehearsals/{created['id']}", headers=approver).json()
    assert detail["published_version"]["version"] == 1
    approvals = client.get(f"/api/food/rule-governance/rehearsals/{created['id']}/approvals", headers=approver).json()
    assert approvals[0]["decision"] == "approved" and approvals[0]["result"] == "accepted"
    assert approvals[0]["comment"] == "同意按期发布"

    # 发布不回写任何历史批次/检测结论
    lot = client.get(f"/api/food/lots/{lot_id}").json()
    assert lot["status"] == "testing"
    assert lot["risk_level"] == "unknown"
    assert lot["samples"][0]["results"][0]["verdict"] == "pass"


def test_second_publish_supersedes_and_duplicate_approval_rejected(client, admin):
    planner, approver = planner_approver(client, admin)
    seed_lot(client, "LOT-D1", "安心农场")
    first = client.post("/api/food/rule-governance/rehearsals", json=TIGHTER_RULE, headers=planner).json()
    client.post(f"/api/food/rule-governance/rehearsals/{first['id']}/decision",
                json={"decision": "approved", "comment": "第一次发布"}, headers=approver)

    # 重复审批同一预演：409 + duplicate 留痕，版本数不变
    again = client.post(f"/api/food/rule-governance/rehearsals/{first['id']}/decision",
                        json={"decision": "approved", "comment": "重复审批"}, headers=approver)
    assert again.status_code == 409
    records = client.get(f"/api/food/rule-governance/rehearsals/{first['id']}/approvals", headers=approver).json()
    assert [item["result"] for item in records] == ["accepted", "duplicate"]
    assert len(client.get("/api/food/rule-governance/versions", headers=approver).json()) == 1

    # 基于版本 1 再预演、发布版本 2：旧版本原子下线
    rule2 = dict(TIGHTER_RULE, code="REH-002", limits=[{"analyte": "毒死蜱", "category": None, "limit_mg_kg": 0.02}])
    second = client.post("/api/food/rule-governance/rehearsals", json=rule2, headers=planner).json()
    published = client.post(f"/api/food/rule-governance/rehearsals/{second['id']}/decision",
                            json={"decision": "approved", "comment": "第二次发布"}, headers=approver)
    assert published.status_code == 200
    versions = client.get("/api/food/rule-governance/versions", headers=approver).json()
    assert {v["version"]: v["status"] for v in versions} == {1: "superseded", 2: "active"}


def test_stale_basis_candidate_cannot_publish(client, admin):
    planner, approver = planner_approver(client, admin)
    seed_lot(client, "LOT-S1", "安心农场")
    first = client.post("/api/food/rule-governance/rehearsals", json=TIGHTER_RULE, headers=planner).json()
    rule2 = dict(TIGHTER_RULE, code="REH-002", limits=[{"analyte": "毒死蜱", "category": None, "limit_mg_kg": 0.02}])
    second = client.post("/api/food/rule-governance/rehearsals", json=rule2, headers=planner).json()

    client.post(f"/api/food/rule-governance/rehearsals/{second['id']}/decision",
                json={"decision": "approved", "comment": "先发布第二个"}, headers=approver)
    # first 的基础版本已漂移
    stale = client.post(f"/api/food/rule-governance/rehearsals/{first['id']}/decision",
                        json={"decision": "approved", "comment": "再发第一个"}, headers=approver)
    assert stale.status_code == 409
    assert stale.json()["error"]["context"]["status"] == "basis_stale"
    records = client.get(f"/api/food/rule-governance/rehearsals/{first['id']}/approvals", headers=approver).json()
    assert records[0]["result"] == "basis_stale"
    assert client.get(f"/api/food/rule-governance/rehearsals/{first['id']}", headers=approver).json()["status"] == "ready"


def test_rejection_does_not_publish(client, admin):
    planner, approver = planner_approver(client, admin)
    seed_lot(client, "LOT-X1", "安心农场")
    created = client.post("/api/food/rule-governance/rehearsals", json=TIGHTER_RULE, headers=planner).json()
    rejected = client.post(f"/api/food/rule-governance/rehearsals/{created['id']}/decision",
                           json={"decision": "rejected", "comment": "依据不足"}, headers=approver)
    assert rejected.status_code == 200 and rejected.json()["status"] == "rejected"
    assert client.get("/api/food/rule-governance/current-version", headers=approver).json()["version"] == 0
    # 驳回后再尝试通过也被拒
    again = client.post(f"/api/food/rule-governance/rehearsals/{created['id']}/decision",
                        json={"decision": "approved", "comment": "翻案"}, headers=approver)
    assert again.status_code == 409


# ---------- 过期、篡改、重启残留 ----------

def test_expired_candidate_cannot_publish(client, admin):
    planner, approver = planner_approver(client, admin)
    seed_lot(client, "LOT-E1", "安心农场")
    created = client.post("/api/food/rule-governance/rehearsals", json=TIGHTER_RULE, headers=planner).json()
    connection = get_connection()
    connection.execute("UPDATE food_rule_rehearsals SET expires_at=? WHERE id=?",
                       (to_storage(utc_now() - timedelta(minutes=1)), created["id"]))
    connection.commit()

    expired = client.post(f"/api/food/rule-governance/rehearsals/{created['id']}/decision",
                          json={"decision": "approved", "comment": "过期通过"}, headers=approver)
    assert expired.status_code == 409
    detail = client.get(f"/api/food/rule-governance/rehearsals/{created['id']}", headers=approver).json()
    assert detail["status"] == "expired"
    assert detail["published_version_id"] is None
    records = client.get(f"/api/food/rule-governance/rehearsals/{created['id']}/approvals", headers=approver).json()
    assert records[0]["result"] == "expired"
    assert client.get("/api/food/rule-governance/versions", headers=approver).json() == []


def test_tampered_report_blocks_publish(client, admin):
    planner, approver = planner_approver(client, admin)
    seed_lot(client, "LOT-T1", "安心农场")
    created = client.post("/api/food/rule-governance/rehearsals", json=TIGHTER_RULE, headers=planner).json()
    connection = get_connection()
    connection.execute("UPDATE food_rule_rehearsals SET report_json='{}' WHERE id=?", (created["id"],))
    connection.commit()
    blocked = client.post(f"/api/food/rule-governance/rehearsals/{created['id']}/decision",
                          json={"decision": "approved", "comment": "强推"}, headers=approver)
    assert blocked.status_code == 409
    assert blocked.json()["error"]["context"]["status"] == "report_corrupt"
    assert client.get("/api/food/rule-governance/versions", headers=approver).json() == []


def test_restart_never_publishes_interrupted_rehearsal(client, admin):
    seed_lot(client, "LOT-Z1", "安心农场")
    connection = get_connection()
    # 模拟服务在预演计算过程中崩溃：留下 running 残留，心跳停在很久以前
    old = to_storage(datetime(2026, 9, 20, tzinfo=UTC))
    connection.execute(
        "INSERT INTO food_rule_rehearsals(code,status,candidate_limits_json,candidate_sampling_json,"
        "candidate_checksum,basis_checksum,selection_json,expires_at,created_by,created_at,heartbeat_at,updated_at) "
        "VALUES('REH-CRASH','running','[]','[]','x','baseline','{}',?,?,?,?,?)",
        (to_storage(datetime(2026, 9, 21, tzinfo=UTC)), "tester", old, old, old),
    )
    connection.commit()
    crashed_id = connection.execute("SELECT id FROM food_rule_rehearsals WHERE code='REH-CRASH'").fetchone()[0]

    # “服务重启”：重新跑一遍 lifespan 的初始化逻辑，不允许有任何自动发布或状态迁移
    with TestClient(client.app) as restarted:
        row = connection.execute("SELECT status FROM food_rule_rehearsals WHERE id=?", (crashed_id,)).fetchone()
        assert row["status"] == "running"
        assert restarted.get("/api/food/rule-governance/current-version", headers=admin["headers"]).json()["version"] == 0

        # 尝试审批残留预演：被识别为 interrupted，拒绝发布并留痕
        blocked = restarted.post(f"/api/food/rule-governance/rehearsals/{crashed_id}/decision",
                                 json={"decision": "approved", "comment": "蒙混过关"}, headers=admin["headers"])
        assert blocked.status_code == 409
        assert blocked.json()["error"]["context"]["status"] == "interrupted"

    detail = client.get(f"/api/food/rule-governance/rehearsals/{crashed_id}", headers=admin["headers"]).json()
    assert detail["status"] == "interrupted" and detail["published_version_id"] is None
    records = client.get(f"/api/food/rule-governance/rehearsals/{crashed_id}/approvals", headers=admin["headers"]).json()
    assert records[0]["result"] == "interrupted"
    assert client.get("/api/food/rule-governance/versions", headers=admin["headers"]).json() == []


# ---------- 服务层：冻结时钟下的过期判定 ----------

def _principal() -> Principal:
    return Principal(user_id=1, username="admin", display_name="管理员", department_id=None,
                     permissions=frozenset({"*"}), session_id=1)


def test_service_expiry_with_frozen_clock(client):
    FoodService().create_lot({
        "lot_code": "LOT-C1", "product_name": "菠菜", "category": "叶菜", "supplier": "安心农场",
        "origin": "鲁", "harvest_date": "2026-09-20", "quantity_kg": 1000, "trace_code": "LOT-C1-T",
    })
    clock = FrozenClock(datetime(2026, 9, 25, 10, 0, tzinfo=UTC))
    service = RuleGovernanceService(get_connection(), clock=clock)
    rehearsal = service.create_rehearsal(_principal(), {**TIGHTER_RULE, "code": "REH-CLOCK", "expires_in_minutes": 60})
    assert rehearsal["status"] == "ready"

    clock.advance(minutes=61)
    from app.core.errors import ConflictError
    try:
        service.decide(_principal(), rehearsal["id"], "approved", "过期审批")
    except ConflictError as exc:
        assert "过期" in exc.message
    else:
        raise AssertionError("过期候选应当发布失败")
    row = service.get_rehearsal(rehearsal["id"])
    assert row["status"] == "expired" and row["published_version_id"] is None

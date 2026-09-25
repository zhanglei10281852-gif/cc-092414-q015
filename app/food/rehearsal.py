from __future__ import annotations

import json
import math
import secrets
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from app.core.clock import from_storage, to_storage, utc_now
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.security import Principal
from app.database import get_connection, transaction


SCHEMA = """
CREATE TABLE IF NOT EXISTS food_rule_sets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_no INTEGER NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','superseded')),
    limits_json TEXT NOT NULL DEFAULT '{}',
    sampling_per_ton REAL NOT NULL DEFAULT 1.0,
    source TEXT NOT NULL DEFAULT 'seed' CHECK(source IN ('seed','rehearsal')),
    rehearsal_id INTEGER,
    published_by TEXT NOT NULL DEFAULT '',
    published_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_rule_sets_single_active ON food_rule_sets(status) WHERE status='active';
CREATE TABLE IF NOT EXISTS food_rule_rehearsals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'computed' CHECK(status IN ('computed','approved','rejected','published','expired')),
    candidate_limits_json TEXT NOT NULL,
    candidate_sampling_per_ton REAL NOT NULL,
    base_rule_set_id INTEGER NOT NULL REFERENCES food_rule_sets(id),
    base_version_no INTEGER NOT NULL,
    report_json TEXT NOT NULL DEFAULT '{}',
    expires_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_by_id INTEGER,
    approved_by TEXT,
    approved_at TEXT,
    approval_opinion TEXT,
    published_rule_set_id INTEGER,
    published_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rule_rehearsals_status ON food_rule_rehearsals(status, created_at);
CREATE TABLE IF NOT EXISTS food_rule_rehearsal_approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rehearsal_id INTEGER NOT NULL REFERENCES food_rule_rehearsals(id) ON DELETE CASCADE,
    decision TEXT NOT NULL CHECK(decision IN ('approve','reject')),
    opinion TEXT NOT NULL,
    actor TEXT NOT NULL,
    actor_id INTEGER,
    created_at TEXT NOT NULL
);
"""

# 预演只评估仍处于监管流转中的批次；已召回、已销毁的批次结论不再变化。
LIVE_LOT_STATUSES = ("pending", "testing", "released", "held")
DEFAULT_SAMPLING_PER_TON = 1.0
REHEARSAL_PERMISSIONS = ("food.rules.rehearse", "food.rules.approve", "food.rules.publish")


def ensure_schema() -> None:
    connection = get_connection()
    connection.executescript(SCHEMA)
    count = connection.execute("SELECT COUNT(*) FROM food_rule_sets").fetchone()[0]
    if count == 0:
        now = to_storage(utc_now())
        connection.execute(
            "INSERT INTO food_rule_sets(version_no,status,limits_json,sampling_per_ton,source,published_by,published_at,created_at) VALUES(1,'active','{}',?,'seed','system',?,?)",
            (DEFAULT_SAMPLING_PER_TON, now, now),
        )


def _loads(text: str | None) -> Any:
    return json.loads(text) if text else {}


class RuleRehearsalService:
    """限值规则预演：候选规则只写预演表，审批通过后在单个事务内原子发布。"""

    def __init__(self, connection: sqlite3.Connection | None = None):
        self.connection = connection or get_connection()
        ensure_schema()

    # ---------- 查询 ----------

    def current_rule_set(self) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM food_rule_sets WHERE status='active'").fetchone()
        if row is None:
            raise NotFoundError("当前没有生效的规则版本")
        return self._rule_set_view(row)

    def list_rule_sets(self) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM food_rule_sets ORDER BY version_no DESC").fetchall()
        return [self._rule_set_view(row) for row in rows]

    def list_rehearsals(self, status: str | None = None) -> list[dict[str, Any]]:
        now = utc_now()
        with transaction(immediate=True) as connection:
            self._materialize_expired(connection, now)
            if status:
                rows = connection.execute("SELECT * FROM food_rule_rehearsals WHERE status=? ORDER BY id DESC", (status,)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM food_rule_rehearsals ORDER BY id DESC").fetchall()
            return [self._rehearsal_view(connection, row) for row in rows]

    def get_rehearsal(self, rehearsal_id: int, *, include_report: bool = False) -> dict[str, Any]:
        now = utc_now()
        with transaction(immediate=True) as connection:
            row = self._fetch_rehearsal(connection, rehearsal_id, now)
            return self._rehearsal_view(connection, row, include_report=include_report)

    def get_report(self, rehearsal_id: int) -> dict[str, Any]:
        now = utc_now()
        with transaction(immediate=True) as connection:
            row = self._fetch_rehearsal(connection, rehearsal_id, now)
            report = _loads(row["report_json"])
            report["rehearsal_id"] = row["id"]
            report["rehearsal_code"] = row["code"]
            report["status"] = row["status"]
            return report

    # ---------- 状态流转 ----------

    def create(self, payload: dict[str, Any], principal: Principal) -> dict[str, Any]:
        principal.require("food.rules.rehearse")
        now = utc_now()
        expires_at = self._resolve_expiry(payload, now)
        candidate_limits = payload["limits"]
        candidate_ratio = payload["sampling_per_ton"]
        with transaction(immediate=True) as connection:
            base = connection.execute("SELECT * FROM food_rule_sets WHERE status='active'").fetchone()
            if base is None:
                raise ConflictError("当前没有生效的规则版本，无法预演")
            report = self._compute_report(connection, base, candidate_limits, candidate_ratio, principal, now)
            status = "expired" if expires_at <= now else "computed"
            code = self._generate_code(connection, now)
            now_text = to_storage(now)
            cursor = connection.execute(
                "INSERT INTO food_rule_rehearsals(code,title,status,candidate_limits_json,candidate_sampling_per_ton,base_rule_set_id,base_version_no,report_json,expires_at,created_by,created_by_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    code,
                    payload["title"],
                    status,
                    json.dumps(candidate_limits, ensure_ascii=False, sort_keys=True),
                    candidate_ratio,
                    base["id"],
                    base["version_no"],
                    json.dumps(report, ensure_ascii=False),
                    to_storage(expires_at),
                    principal.display_name,
                    principal.user_id,
                    now_text,
                    now_text,
                ),
            )
            rehearsal_id = cursor.lastrowid
            self._audit(connection, "rules.rehearse", principal, {"rehearsal_id": rehearsal_id, "code": code, "base_version_no": base["version_no"]}, now)
            row = connection.execute("SELECT * FROM food_rule_rehearsals WHERE id=?", (rehearsal_id,)).fetchone()
            return self._rehearsal_view(connection, row, include_report=True)

    def approve(self, rehearsal_id: int, payload: dict[str, Any], principal: Principal) -> dict[str, Any]:
        principal.require("food.rules.approve")
        now = utc_now()
        decision = payload["decision"]
        with transaction(immediate=True) as connection:
            row = self._fetch_rehearsal(connection, rehearsal_id, now)
            if row["status"] == "expired":
                raise ConflictError("候选规则已过期，不能审批")
            if row["status"] != "computed":
                raise ConflictError(f"预演当前状态为 {row['status']}，不能重复审批")
            if decision == "approve" and row["created_by_id"] == principal.user_id:
                raise ConflictError("审批人不能与预演创建人相同")
            new_status = "approved" if decision == "approve" else "rejected"
            now_text = to_storage(now)
            cursor = connection.execute(
                "UPDATE food_rule_rehearsals SET status=?,approved_by=?,approved_at=?,approval_opinion=?,updated_at=? WHERE id=? AND status='computed'",
                (new_status, principal.display_name, now_text, payload["opinion"], now_text, rehearsal_id),
            )
            if cursor.rowcount == 0:
                raise ConflictError("预演已被其他审批处理")
            connection.execute(
                "INSERT INTO food_rule_rehearsal_approvals(rehearsal_id,decision,opinion,actor,actor_id,created_at) VALUES(?,?,?,?,?,?)",
                (rehearsal_id, decision, payload["opinion"], principal.display_name, principal.user_id, now_text),
            )
            report = _loads(row["report_json"])
            self._mark_permission_step(report, "approve", principal, now)
            connection.execute("UPDATE food_rule_rehearsals SET report_json=? WHERE id=?", (json.dumps(report, ensure_ascii=False), rehearsal_id))
            self._audit(connection, "rules." + decision, principal, {"rehearsal_id": rehearsal_id, "opinion": payload["opinion"]}, now)
            row = connection.execute("SELECT * FROM food_rule_rehearsals WHERE id=?", (rehearsal_id,)).fetchone()
            return self._rehearsal_view(connection, row, include_report=True)

    def publish(self, rehearsal_id: int, principal: Principal) -> dict[str, Any]:
        principal.require("food.rules.publish")
        now = utc_now()
        with transaction(immediate=True) as connection:
            row = self._fetch_rehearsal(connection, rehearsal_id, now)
            if row["status"] == "expired":
                raise ConflictError("候选规则已过期，不能发布")
            if row["status"] != "approved":
                raise ConflictError(f"预演当前状态为 {row['status']}，必须先审批通过才能发布")
            active = connection.execute("SELECT * FROM food_rule_sets WHERE status='active'").fetchone()
            if active is None or active["id"] != row["base_rule_set_id"]:
                raise ConflictError("基准规则版本已变化，该预演结论已失效，请重新预演")
            now_text = to_storage(now)
            connection.execute("UPDATE food_rule_sets SET status='superseded' WHERE id=? AND status='active'", (active["id"],))
            cursor = connection.execute(
                "INSERT INTO food_rule_sets(version_no,status,limits_json,sampling_per_ton,source,rehearsal_id,published_by,published_at,created_at) VALUES(?,?,?,?,'rehearsal',?,?,?,?)",
                (active["version_no"] + 1, "active", row["candidate_limits_json"], row["candidate_sampling_per_ton"], rehearsal_id, principal.display_name, now_text, now_text),
            )
            rule_set_id = cursor.lastrowid
            updated = connection.execute(
                "UPDATE food_rule_rehearsals SET status='published',published_rule_set_id=?,published_at=?,updated_at=? WHERE id=? AND status='approved'",
                (rule_set_id, now_text, now_text, rehearsal_id),
            )
            if updated.rowcount == 0:
                raise ConflictError("预演状态已变化，发布中止")
            report = _loads(row["report_json"])
            self._mark_permission_step(report, "publish", principal, now)
            connection.execute("UPDATE food_rule_rehearsals SET report_json=? WHERE id=?", (json.dumps(report, ensure_ascii=False), rehearsal_id))
            self._audit(connection, "rules.publish", principal, {"rehearsal_id": rehearsal_id, "rule_set_id": rule_set_id, "version_no": active["version_no"] + 1}, now)
            rule_set = connection.execute("SELECT * FROM food_rule_sets WHERE id=?", (rule_set_id,)).fetchone()
            rehearsal = connection.execute("SELECT * FROM food_rule_rehearsals WHERE id=?", (rehearsal_id,)).fetchone()
            return {"rule_set": self._rule_set_view(rule_set), "rehearsal": self._rehearsal_view(connection, rehearsal)}

    # ---------- 内部：报告计算 ----------

    def _compute_report(
        self,
        connection: sqlite3.Connection,
        base: sqlite3.Row,
        candidate_limits: dict[str, float],
        candidate_ratio: float,
        principal: Principal,
        now: datetime,
    ) -> dict[str, Any]:
        base_limits = _loads(base["limits_json"])
        base_ratio = float(base["sampling_per_ton"])
        placeholders = ",".join("?" for _ in LIVE_LOT_STATUSES)
        lots = connection.execute(f"SELECT * FROM food_lots WHERE status IN ({placeholders}) ORDER BY id", LIVE_LOT_STATUSES).fetchall()
        diffs: list[dict[str, Any]] = []
        resampling: list[dict[str, Any]] = []
        suppliers: dict[str, dict[str, Any]] = {}
        pass_to_review = 0
        review_to_pass = 0
        additional_total = 0
        for lot in lots:
            results = connection.execute(
                "SELECT r.* FROM food_test_results r JOIN food_samples s ON s.id=r.sample_id WHERE s.lot_id=? ORDER BY r.id",
                (lot["id"],),
            ).fetchall()
            current_conclusion = "review" if any(item["verdict"] == "fail" for item in results) else "pass"
            candidate_conclusion = "pass"
            changed_results: list[dict[str, Any]] = []
            for item in results:
                analyte = item["analyte"]
                current_limit = base_limits.get(analyte, item["limit_mg_kg"])
                candidate_limit = candidate_limits.get(analyte, current_limit)
                candidate_verdict = "pass" if item["value_mg_kg"] <= candidate_limit else "fail"
                if candidate_verdict == "fail":
                    candidate_conclusion = "review"
                if candidate_verdict != item["verdict"] or candidate_limit != current_limit:
                    changed_results.append(
                        {
                            "sample_id": item["sample_id"],
                            "analyte": analyte,
                            "value_mg_kg": item["value_mg_kg"],
                            "current_limit": current_limit,
                            "candidate_limit": candidate_limit,
                            "current_verdict": item["verdict"],
                            "candidate_verdict": candidate_verdict,
                        }
                    )
            supplier = lot["supplier"]
            entry = suppliers.setdefault(
                supplier,
                {"supplier": supplier, "lots_changed": 0, "pass_to_review": 0, "review_to_pass": 0, "additional_samples": 0, "lot_codes": []},
            )
            if candidate_conclusion != current_conclusion:
                if candidate_conclusion == "review":
                    pass_to_review += 1
                    entry["pass_to_review"] += 1
                else:
                    review_to_pass += 1
                    entry["review_to_pass"] += 1
                entry["lots_changed"] += 1
                entry["lot_codes"].append(lot["lot_code"])
                diffs.append(
                    {
                        "lot_id": lot["id"],
                        "lot_code": lot["lot_code"],
                        "product_name": lot["product_name"],
                        "supplier": supplier,
                        "lot_status": lot["status"],
                        "current_conclusion": current_conclusion,
                        "candidate_conclusion": candidate_conclusion,
                        "changed_results": changed_results,
                    }
                )
            actual_samples = connection.execute("SELECT COUNT(*) FROM food_samples WHERE lot_id=? AND status!='void'", (lot["id"],)).fetchone()[0]
            current_required = max(1, math.ceil(lot["quantity_kg"] / 1000 * base_ratio))
            candidate_required = max(1, math.ceil(lot["quantity_kg"] / 1000 * candidate_ratio))
            additional = max(0, candidate_required - actual_samples)
            if additional > 0:
                additional_total += additional
                entry["additional_samples"] += additional
                resampling.append(
                    {
                        "lot_id": lot["id"],
                        "lot_code": lot["lot_code"],
                        "supplier": supplier,
                        "quantity_kg": lot["quantity_kg"],
                        "actual_samples": actual_samples,
                        "current_required": current_required,
                        "candidate_required": candidate_required,
                        "additional": additional,
                    }
                )
        affected = [item for item in suppliers.values() if item["lots_changed"] or item["additional_samples"]]
        affected.sort(key=lambda item: (-(item["lots_changed"] + item["additional_samples"]), item["supplier"]))
        totals = {
            "lots_evaluated": len(lots),
            "lots_with_conclusion_change": pass_to_review + review_to_pass,
            "pass_to_review": pass_to_review,
            "review_to_pass": review_to_pass,
            "affected_suppliers": len(affected),
            "additional_samples_for_ratio": additional_total,
            "retest_samples_for_review": pass_to_review,
            "total_additional_tests": additional_total + pass_to_review,
        }
        return {
            "generated_at": to_storage(now),
            "scope": {"lot_statuses": list(LIVE_LOT_STATUSES), "lot_count": len(lots)},
            "base_rule": {"version_no": base["version_no"], "sampling_per_ton": base_ratio, "limits": base_limits},
            "candidate_rule": {"sampling_per_ton": candidate_ratio, "limits": candidate_limits},
            "totals": totals,
            "conclusion_diffs": diffs,
            "affected_suppliers": affected,
            "resampling": resampling,
            "permission_check": self._permission_check(principal),
        }

    def _permission_check(self, principal: Principal) -> dict[str, Any]:
        def held(permission: str) -> bool:
            return "*" in principal.permissions or permission in principal.permissions

        return {
            "generated_for": principal.display_name,
            "creator_can_approve": held("food.rules.approve"),
            "creator_can_publish": held("food.rules.publish"),
            "separation_of_duties": "审批人必须与预演创建人不同",
            "steps": [
                {"step": "rehearse", "permission": "food.rules.rehearse", "actor": principal.display_name, "satisfied": held("food.rules.rehearse")},
                {"step": "approve", "permission": "food.rules.approve", "actor": None, "satisfied": None},
                {"step": "publish", "permission": "food.rules.publish", "actor": None, "satisfied": None},
            ],
        }

    @staticmethod
    def _mark_permission_step(report: dict[str, Any], step: str, principal: Principal, now: datetime) -> None:
        check = report.get("permission_check")
        if not isinstance(check, dict):
            return
        for item in check.get("steps", []):
            if item.get("step") == step:
                item["actor"] = principal.display_name
                item["satisfied"] = True
                item["at"] = to_storage(now)

    # ---------- 内部：状态与视图 ----------

    def _fetch_rehearsal(self, connection: sqlite3.Connection, rehearsal_id: int, now: datetime) -> sqlite3.Row:
        self._materialize_expired(connection, now)
        row = connection.execute("SELECT * FROM food_rule_rehearsals WHERE id=?", (rehearsal_id,)).fetchone()
        if row is None:
            raise NotFoundError("预演不存在")
        return row

    @staticmethod
    def _materialize_expired(connection: sqlite3.Connection, now: datetime) -> None:
        connection.execute(
            "UPDATE food_rule_rehearsals SET status='expired',updated_at=? WHERE status IN ('computed','approved') AND expires_at<=?",
            (to_storage(now), to_storage(now)),
        )

    @staticmethod
    def _resolve_expiry(payload: dict[str, Any], now: datetime) -> datetime:
        explicit = payload.get("expires_at")
        if explicit:
            try:
                parsed = from_storage(explicit)
            except ValueError as exc:
                raise ValidationError("expires_at 格式无效") from exc
            if parsed is None:
                raise ValidationError("expires_at 格式无效")
            return parsed
        return now + timedelta(hours=payload["expires_in_hours"])

    @staticmethod
    def _generate_code(connection: sqlite3.Connection, now: datetime) -> str:
        for _ in range(5):
            code = f"RH-{now:%Y%m%d}-{secrets.token_hex(3).upper()}"
            if connection.execute("SELECT 1 FROM food_rule_rehearsals WHERE code=?", (code,)).fetchone() is None:
                return code
        raise ConflictError("预演编号生成冲突，请重试")

    def _audit(self, connection: sqlite3.Connection, action: str, principal: Principal, payload: dict[str, Any], now: datetime) -> None:
        connection.execute(
            "INSERT INTO food_audit(lot_id,action,actor,payload_json,created_at) VALUES(NULL,?,?,?,?)",
            (action, principal.display_name, json.dumps(payload, ensure_ascii=False), to_storage(now)),
        )

    def _rehearsal_view(self, connection: sqlite3.Connection, row: sqlite3.Row, *, include_report: bool = False) -> dict[str, Any]:
        report = _loads(row["report_json"])
        approvals = connection.execute("SELECT * FROM food_rule_rehearsal_approvals WHERE rehearsal_id=? ORDER BY id", (row["id"],)).fetchall()
        view = {
            "id": row["id"],
            "code": row["code"],
            "title": row["title"],
            "status": row["status"],
            "candidate_limits": _loads(row["candidate_limits_json"]),
            "candidate_sampling_per_ton": row["candidate_sampling_per_ton"],
            "base_version_no": row["base_version_no"],
            "expires_at": row["expires_at"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "approved_by": row["approved_by"],
            "approved_at": row["approved_at"],
            "approval_opinion": row["approval_opinion"],
            "published_rule_set_id": row["published_rule_set_id"],
            "published_at": row["published_at"],
            "report_summary": report.get("totals", {}),
            "approvals": [
                {"decision": item["decision"], "opinion": item["opinion"], "actor": item["actor"], "created_at": item["created_at"]}
                for item in approvals
            ],
        }
        if include_report:
            view["report"] = report
        return view

    @staticmethod
    def _rule_set_view(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "version_no": row["version_no"],
            "status": row["status"],
            "limits": _loads(row["limits_json"]),
            "sampling_per_ton": row["sampling_per_ton"],
            "source": row["source"],
            "rehearsal_id": row["rehearsal_id"],
            "published_by": row["published_by"],
            "published_at": row["published_at"],
            "created_at": row["created_at"],
        }

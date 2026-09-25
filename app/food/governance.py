"""农残限值与抽检比例的规则治理：候选规则预演、报告、审批与原子发布。

设计要点：
- 历史检测结论不可变：预演只在报告里重算，绝不回写 food_test_results / food_lots。
- 预演生命周期落库：running -> ready -> approved/rejected/expired/interrupted/failed，
  服务重启不会执行任何发布动作，崩溃残留的 running 记录只会被惰性标记为 interrupted。
- 审批通过与新版本发布在同一个 IMMEDIATE 事务内完成；
  部分唯一索引保证同一时刻只有一个 active 规则版本。
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from datetime import timedelta
from typing import Any

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, PermissionDeniedError, ValidationError
from app.core.security import Principal
from app.database import get_connection, transaction

# 每个新增检测项次占用的实验室工时（分钟），用于工作量估算
LAB_MINUTES_PER_TEST = 30
# 没有历史采样时，单个新增样品的参考重量（克）
DEFAULT_SAMPLE_WEIGHT_G = 250.0
# running 状态超过该时长没有心跳，视为进程中断的残留预演
STALE_RUNNING_SECONDS = 1800
BASELINE_CHECKSUM = "baseline"

GOVERNANCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS food_rule_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version INTEGER NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','superseded')),
    limits_json TEXT NOT NULL,
    sampling_json TEXT NOT NULL,
    checksum TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    published_by TEXT NOT NULL,
    published_by_user_id INTEGER,
    published_at TEXT NOT NULL,
    superseded_at TEXT,
    source_rehearsal_id INTEGER REFERENCES food_rule_rehearsals(id)
);
CREATE TABLE IF NOT EXISTS food_rule_rehearsals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK(status IN ('running','ready','approved','rejected','expired','interrupted','failed')),
    candidate_limits_json TEXT NOT NULL,
    candidate_sampling_json TEXT NOT NULL,
    candidate_checksum TEXT NOT NULL,
    basis_version_id INTEGER,
    basis_checksum TEXT NOT NULL,
    selection_json TEXT NOT NULL,
    report_json TEXT,
    report_checksum TEXT,
    expires_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_by_user_id INTEGER,
    created_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    decided_by TEXT,
    decided_by_user_id INTEGER,
    decided_at TEXT,
    decision_note TEXT NOT NULL DEFAULT '',
    published_version_id INTEGER REFERENCES food_rule_versions(id)
);
CREATE INDEX IF NOT EXISTS idx_food_rule_rehearsals_status ON food_rule_rehearsals(status);
CREATE TABLE IF NOT EXISTS food_rule_approval_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rehearsal_id INTEGER NOT NULL REFERENCES food_rule_rehearsals(id) ON DELETE CASCADE,
    decision TEXT NOT NULL CHECK(decision IN ('approved','rejected')),
    comment TEXT NOT NULL DEFAULT '',
    result TEXT NOT NULL CHECK(result IN ('accepted','rejected_recorded','duplicate','expired','interrupted','basis_stale','not_ready','forbidden')),
    approver TEXT NOT NULL,
    approver_user_id INTEGER,
    permission_check_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_food_rule_approvals_rehearsal ON food_rule_approval_records(rehearsal_id, id);
/* 同一时刻只允许一个生效版本：原子发布的最后一道防线 */
CREATE UNIQUE INDEX IF NOT EXISTS idx_food_rule_one_active
    ON food_rule_versions(status) WHERE status='active';
"""


def ensure_governance_schema() -> None:
    get_connection().executescript(GOVERNANCE_SCHEMA)


def _canonical_limits(limits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for item in limits:
        normalized.append(
            {
                "analyte": str(item["analyte"]).strip(),
                "category": str(item["category"]).strip() if item.get("category") else None,
                "limit_mg_kg": round(float(item["limit_mg_kg"]), 6),
            }
        )
    return normalized


def _canonical_sampling(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for item in items:
        normalized.append(
            {
                "scope": item["scope"],
                "key": str(item["key"]).strip() if item.get("key") else None,
                "rate": round(float(item["rate"]), 6),
            }
        )
    return normalized


def rules_checksum(limits: list[dict[str, Any]], sampling: list[dict[str, Any]]) -> str:
    payload = json.dumps({"limits": limits, "sampling": sampling}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def resolve_limit(limits: list[dict[str, Any]], analyte: str, category: str) -> float | None:
    """优先精确匹配品类，其次匹配通用限值（category 为空）。"""
    fallback: float | None = None
    for item in limits:
        if item["analyte"] != analyte:
            continue
        if item["category"] == category:
            return float(item["limit_mg_kg"])
        if item["category"] is None and fallback is None:
            fallback = float(item["limit_mg_kg"])
    return fallback


def resolve_rate(sampling: list[dict[str, Any]], category: str, supplier: str) -> float | None:
    """抽检比例优先级：供应商 > 品类 > 全局。"""
    category_rate: float | None = None
    global_rate: float | None = None
    for item in sampling:
        if item["scope"] == "supplier" and item["key"] == supplier:
            return float(item["rate"])
        if item["scope"] == "category" and item["key"] == category and category_rate is None:
            category_rate = float(item["rate"])
        if item["scope"] == "global" and global_rate is None:
            global_rate = float(item["rate"])
    return category_rate if category_rate is not None else global_rate


def _verdict(value: float, limit: float) -> str:
    return "pass" if value <= limit else "fail"


def _report_checksum(report: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def summarize_report(report: dict[str, Any]) -> dict[str, Any]:
    lots = report["lots"]
    workload = report["workload"]
    return {
        "generated_at": report["generated_at"],
        "basis_checksum": report["basis"]["checksum"],
        "candidate_checksum": report["candidate_checksum"],
        "lots_total": lots["total"],
        "lots_with_results": lots["with_results"],
        "pass_to_review": lots["pass_to_review"],
        "review_to_pass": lots["review_to_pass"],
        "unchanged": lots["unchanged"],
        "integrity_mismatches": lots["integrity_mismatches"],
        "suppliers_affected": len(report["suppliers"]),
        "total_suggested_extra_g": report["resampling"]["total_extra_g"],
        "additional_samples": workload["additional_samples"],
        "additional_analyte_tests": workload["additional_analyte_tests"],
        "estimated_lab_minutes": workload["estimated_lab_minutes"],
    }


class RuleGovernanceService:
    """规则预演、审批与发布的事务边界。"""

    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None) -> None:
        self.connection = connection or get_connection()
        self.clock = clock or SystemClock()
        ensure_governance_schema()

    # ---------- 版本查询 ----------

    def current_version(self) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM food_rule_versions WHERE status='active' ORDER BY version DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return {
                "version": 0,
                "status": "baseline",
                "checksum": BASELINE_CHECKSUM,
                "limits": [],
                "sampling": [],
                "published_by": None,
                "published_at": None,
                "note": "尚未发布规则版本，现行结论以检测记录自带限值为准",
            }
        return self._version_dict(row)

    def list_versions(self) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM food_rule_versions ORDER BY version DESC").fetchall()
        return [self._version_dict(row) for row in rows]

    @staticmethod
    def _version_dict(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["limits"] = json.loads(result.pop("limits_json"))
        result["sampling"] = json.loads(result.pop("sampling_json"))
        return result

    # ---------- 预演 ----------

    def create_rehearsal(self, principal: Principal, payload: dict[str, Any]) -> dict[str, Any]:
        principal.require("food.rules.rehearse")
        limits = _canonical_limits(payload["limits"])
        sampling = _canonical_sampling(payload["sampling"])
        self._validate_rule_items(limits, sampling)
        checksum = rules_checksum(limits, sampling)

        current = self.current_version()
        basis_checksum = current["checksum"]
        basis_version_id = current.get("id")
        now = self.clock.now()
        now_text = to_storage(now)
        expires_text = to_storage(now + timedelta(minutes=payload["expires_in_minutes"]))
        selection = payload["selection"] or {"mode": "stratified", "per_supplier": 3, "limit": 50, "lot_ids": None}
        # 先完成参数与代表性批次校验，避免无效预演留下 running 残留
        lots = self._select_lots(selection)

        with transaction(immediate=True) as connection:
            try:
                cursor = connection.execute(
                    "INSERT INTO food_rule_rehearsals(code,title,status,candidate_limits_json,"
                    "candidate_sampling_json,candidate_checksum,basis_version_id,basis_checksum,"
                    "selection_json,expires_at,created_by,created_by_user_id,created_at,heartbeat_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        payload["code"].strip().upper(),
                        payload.get("title", ""),
                        "running",
                        json.dumps(limits, ensure_ascii=False),
                        json.dumps(sampling, ensure_ascii=False),
                        checksum,
                        basis_version_id,
                        basis_checksum,
                        json.dumps(selection, ensure_ascii=False, sort_keys=True),
                        expires_text,
                        principal.display_name,
                        principal.user_id,
                        now_text,
                        now_text,
                        now_text,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("预演编号已存在") from exc
            rehearsal_id = int(cursor.lastrowid)

        # 重算在事务外完成；进程在此刻崩溃只会留下 running 残留，绝不会产生生效版本。
        try:
            report = self._build_report(
                rehearsal_id=rehearsal_id,
                limits=limits,
                sampling=sampling,
                selection=selection,
                basis=current,
                principal=principal,
            )
        except Exception:
            with transaction(immediate=True) as connection:
                connection.execute(
                    "UPDATE food_rule_rehearsals SET status='failed',updated_at=? WHERE id=? AND status='running'",
                    (to_storage(self.clock.now()), rehearsal_id),
                )
            raise

        report_json = json.dumps(report, ensure_ascii=False, sort_keys=True)
        checksum_value = _report_checksum(report)
        with transaction(immediate=True) as connection:
            cursor = connection.execute(
                "UPDATE food_rule_rehearsals SET status='ready',report_json=?,report_checksum=?,"
                "heartbeat_at=?,updated_at=? WHERE id=? AND status='running'",
                (report_json, checksum_value, to_storage(self.clock.now()), to_storage(self.clock.now()), rehearsal_id),
            )
            if cursor.rowcount != 1:
                raise ConflictError("预演状态异常，已中止，请重新发起")
            connection.execute(
                "INSERT INTO food_audit(lot_id,action,actor,payload_json,created_at) VALUES(?,?,?,?,?)",
                (
                    None,
                    "rule.rehearsal.create",
                    principal.display_name,
                    json.dumps({"rehearsal_id": rehearsal_id, "code": payload["code"], "summary": summarize_report(report)}, ensure_ascii=False),
                    to_storage(self.clock.now()),
                ),
            )
        row = connection.execute("SELECT * FROM food_rule_rehearsals WHERE id=?", (rehearsal_id,)).fetchone()
        return self._rehearsal_dict(row, include_report=False)

    @staticmethod
    def _validate_rule_items(limits: list[dict[str, Any]], sampling: list[dict[str, Any]]) -> None:
        seen_limits: set[tuple[str, str | None]] = set()
        for item in limits:
            key = (item["analyte"], item["category"])
            if key in seen_limits:
                raise ValidationError(f"限值重复：{item['analyte']} / {item['category'] or '通用'}")
            seen_limits.add(key)
        seen_sampling: set[tuple[str, str | None]] = set()
        for item in sampling:
            if item["scope"] in {"category", "supplier"} and not item["key"]:
                raise ValidationError(f"{item['scope']} 级抽检比例必须提供 key")
            if item["scope"] == "global" and item["key"]:
                raise ValidationError("全局抽检比例不能指定 key")
            key = (item["scope"], item["key"])
            if key in seen_sampling:
                raise ValidationError(f"抽检比例重复：{item['scope']} / {item['key'] or '全局'}")
            seen_sampling.add(key)
        if not limits and not sampling:
            raise ValidationError("候选规则至少需要包含一项限值或抽检比例调整")

    def _select_lots(self, selection: dict[str, Any]) -> list[sqlite3.Row]:
        mode = selection.get("mode", "stratified")
        if mode == "explicit":
            lot_ids = selection.get("lot_ids") or []
            if not lot_ids:
                raise ValidationError("显式选择批次时必须提供 lot_ids")
            placeholders = ",".join("?" for _ in lot_ids)
            rows = self.connection.execute(
                f"SELECT * FROM food_lots WHERE id IN ({placeholders})", tuple(lot_ids)
            ).fetchall()
            if len(rows) != len(set(lot_ids)):
                found = {row["id"] for row in rows}
                missing = [value for value in lot_ids if value not in found]
                raise ValidationError(f"代表性批次不存在：{missing}")
            return list(rows)
        if mode == "recent":
            return list(
                self.connection.execute(
                    "SELECT * FROM food_lots ORDER BY harvest_date DESC,id DESC LIMIT ?",
                    (selection.get("limit", 50),),
                ).fetchall()
            )
        per_supplier = selection.get("per_supplier", 3)
        cap = selection.get("limit", 50)
        return list(
            self.connection.execute(
                "SELECT * FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY supplier "
                "ORDER BY harvest_date DESC,id DESC) AS rn FROM food_lots) WHERE rn<=? "
                "ORDER BY supplier,harvest_date DESC,id DESC LIMIT ?",
                (per_supplier, cap),
            ).fetchall()
        )

    def _build_report(
        self,
        *,
        rehearsal_id: int,
        limits: list[dict[str, Any]],
        sampling: list[dict[str, Any]],
        selection: dict[str, Any],
        basis: dict[str, Any],
        principal: Principal,
    ) -> dict[str, Any]:
        lots = self._select_lots(selection)
        current_limits = basis.get("limits", [])
        current_sampling = basis.get("sampling", [])

        result_rows = self.connection.execute(
            "SELECT l.id AS lot_id,l.lot_code,l.product_name,l.category,l.supplier,l.quantity_kg,"
            "r.analyte,r.value_mg_kg,r.limit_mg_kg,r.verdict "
            "FROM food_lots l JOIN food_samples s ON s.lot_id=l.id "
            "JOIN food_test_results r ON r.sample_id=s.id "
            + ("WHERE l.id IN (%s)" % ",".join("?" for _ in lots) if lots else "WHERE 0")
            + " ORDER BY l.id,r.id",
            tuple(lot["id"] for lot in lots),
        ).fetchall() if lots else []

        results_by_lot: dict[int, list[sqlite3.Row]] = {}
        for row in result_rows:
            results_by_lot.setdefault(row["lot_id"], []).append(row)

        differences: list[dict[str, Any]] = []
        per_lot_resampling: list[dict[str, Any]] = []
        suppliers_map: dict[str, dict[str, Any]] = {}
        pass_to_review = review_to_pass = unchanged = with_results = mismatches = 0
        total_extra_g = 0.0
        total_additional_samples = 0
        total_additional_tests = 0

        for lot in lots:
            lot_id = lot["id"]
            rows = results_by_lot.get(lot_id, [])
            triggers: list[dict[str, Any]] = []
            current_fails = candidate_fails = 0
            if rows:
                with_results += 1
            for row in rows:
                current_limit = resolve_limit(current_limits, row["analyte"], lot["category"])
                current_limit = float(row["limit_mg_kg"]) if current_limit is None else current_limit
                candidate_limit = resolve_limit(limits, row["analyte"], lot["category"])
                if candidate_limit is None:
                    candidate_limit = resolve_limit(current_limits, row["analyte"], lot["category"])
                    candidate_limit = float(row["limit_mg_kg"]) if candidate_limit is None else candidate_limit
                current_verdict = _verdict(row["value_mg_kg"], current_limit)
                candidate_verdict = _verdict(row["value_mg_kg"], candidate_limit)
                if current_verdict == "fail":
                    current_fails += 1
                if candidate_verdict == "fail":
                    candidate_fails += 1
                # 基线重算结论与历史留档结论不一致：历史数据责任疑点，必须在报告中暴露
                if current_verdict != row["verdict"]:
                    mismatches += 1
                if current_verdict != candidate_verdict:
                    triggers.append(
                        {
                            "analyte": row["analyte"],
                            "value_mg_kg": row["value_mg_kg"],
                            "stored_limit_mg_kg": row["limit_mg_kg"],
                            "current_limit_mg_kg": current_limit,
                            "candidate_limit_mg_kg": candidate_limit,
                            "stored_verdict": row["verdict"],
                            "current_verdict": current_verdict,
                            "candidate_verdict": candidate_verdict,
                        }
                    )
            current_conclusion = "review" if current_fails else "pass"
            candidate_conclusion = "review" if candidate_fails else "pass"
            if not rows:
                change = "no_results"
            elif current_conclusion == candidate_conclusion:
                change = "unchanged"
                unchanged += 1
            elif current_conclusion == "pass" and candidate_conclusion == "review":
                change = "pass_to_review"
                pass_to_review += 1
                differences.append(
                    {
                        "lot_id": lot_id,
                        "lot_code": lot["lot_code"],
                        "product_name": lot["product_name"],
                        "category": lot["category"],
                        "supplier": lot["supplier"],
                        "quantity_kg": lot["quantity_kg"],
                        "change": change,
                        "current_conclusion": current_conclusion,
                        "candidate_conclusion": candidate_conclusion,
                        "triggers": triggers,
                    }
                )
            else:
                change = "review_to_pass"
                review_to_pass += 1
                differences.append(
                    {
                        "lot_id": lot_id,
                        "lot_code": lot["lot_code"],
                        "product_name": lot["product_name"],
                        "category": lot["category"],
                        "supplier": lot["supplier"],
                        "quantity_kg": lot["quantity_kg"],
                        "change": change,
                        "current_conclusion": current_conclusion,
                        "candidate_conclusion": candidate_conclusion,
                        "triggers": triggers,
                    }
                )

            sample_row = self.connection.execute(
                "SELECT COALESCE(SUM(sample_weight_g),0) AS weight,COUNT(*) AS count FROM food_samples WHERE lot_id=?",
                (lot_id,),
            ).fetchone()
            sampled_weight_g = float(sample_row["weight"])
            sample_count = int(sample_row["count"])
            current_rate = resolve_rate(current_sampling, lot["category"], lot["supplier"])
            candidate_rate = resolve_rate(sampling, lot["category"], lot["supplier"])
            if candidate_rate is None:
                candidate_rate = current_rate
            current_target_g = lot["quantity_kg"] * 1000.0 * current_rate if current_rate is not None else None
            candidate_target_g = lot["quantity_kg"] * 1000.0 * candidate_rate if candidate_rate is not None else None
            suggested_extra_g = (
                max(0.0, candidate_target_g - sampled_weight_g) if candidate_target_g is not None else 0.0
            )
            avg_weight = sampled_weight_g / sample_count if sample_count else DEFAULT_SAMPLE_WEIGHT_G
            additional_samples = math.ceil(suggested_extra_g / avg_weight) if suggested_extra_g > 0 else 0
            applicable_analytes = {
                item["analyte"]
                for item in limits
                if item["category"] is None or item["category"] == lot["category"]
            }
            additional_tests = additional_samples * len(applicable_analytes)
            total_extra_g += suggested_extra_g
            total_additional_samples += additional_samples
            total_additional_tests += additional_tests

            per_lot_resampling.append(
                {
                    "lot_id": lot_id,
                    "lot_code": lot["lot_code"],
                    "supplier": lot["supplier"],
                    "category": lot["category"],
                    "quantity_kg": lot["quantity_kg"],
                    "sample_count": sample_count,
                    "sampled_weight_g": round(sampled_weight_g, 3),
                    "current_rate": current_rate,
                    "candidate_rate": candidate_rate,
                    "current_target_g": round(current_target_g, 3) if current_target_g is not None else None,
                    "candidate_target_g": round(candidate_target_g, 3) if candidate_target_g is not None else None,
                    "suggested_extra_g": round(suggested_extra_g, 3),
                    "additional_samples": additional_samples,
                    "additional_analyte_tests": additional_tests,
                    "conclusion_change": change,
                }
            )

            affected = change in {"pass_to_review", "review_to_pass"} or suggested_extra_g > 0
            bucket = suppliers_map.setdefault(
                lot["supplier"],
                {"supplier": lot["supplier"], "lots_total": 0, "lots_affected": 0, "quantity_kg": 0.0, "suggested_extra_g": 0.0, "additional_samples": 0},
            )
            bucket["lots_total"] += 1
            bucket["lots_affected"] += int(affected)
            bucket["quantity_kg"] = round(bucket["quantity_kg"] + lot["quantity_kg"], 3)
            bucket["suggested_extra_g"] = round(bucket["suggested_extra_g"] + suggested_extra_g, 3)
            bucket["additional_samples"] += additional_samples

        roles = [
            row[0]
            for row in self.connection.execute(
                "SELECT r.code FROM user_roles ur JOIN roles r ON r.id=ur.role_id WHERE ur.user_id=?",
                (principal.user_id,),
            ).fetchall()
        ]
        report = {
            "rehearsal_id": rehearsal_id,
            "generated_at": to_storage(self.clock.now()),
            "basis": {
                "version_id": basis.get("id"),
                "checksum": basis["checksum"],
                "label": "基线（检测记录自带限值）" if basis["checksum"] == BASELINE_CHECKSUM else f"第 {basis['version']} 版",
            },
            "candidate_checksum": rules_checksum(limits, sampling),
            "selection": selection,
            "lots": {
                "total": len(lots),
                "with_results": with_results,
                "pass_to_review": pass_to_review,
                "review_to_pass": review_to_pass,
                "unchanged": unchanged,
                "integrity_mismatches": mismatches,
            },
            "differences": differences,
            "suppliers": [suppliers_map[key] for key in sorted(suppliers_map) if suppliers_map[key]["lots_affected"]],
            "resampling": {
                "per_lot": per_lot_resampling,
                "total_extra_g": round(total_extra_g, 3),
            },
            "workload": {
                "additional_samples": total_additional_samples,
                "additional_analyte_tests": total_additional_tests,
                "estimated_lab_minutes": total_additional_tests * LAB_MINUTES_PER_TEST,
            },
            "permission_check": {
                "created_by": {
                    "user_id": principal.user_id,
                    "username": principal.username,
                    "display_name": principal.display_name,
                    "roles": roles,
                    "permission": "food.rules.rehearse",
                    "granted": principal.can("food.rules.rehearse"),
                },
                "approval_required": ["food.rules.approve"],
                "segregation_rule": "审批人不得是预演发起人",
            },
        }
        return report

    # ---------- 查询 ----------

    def list_rehearsals(self, status: str | None = None, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        self.sweep()
        sql = "SELECT * FROM food_rule_rehearsals"
        params: list[Any] = []
        if status:
            sql += " WHERE status=?"
            params.append(status)
        sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        rows = self.connection.execute(sql, tuple(params)).fetchall()
        count_sql = "SELECT COUNT(*) FROM food_rule_rehearsals"
        if status:
            count_sql += " WHERE status=?"
        total = self.connection.execute(count_sql, tuple(params[:-2]) if status else ()).fetchone()[0]
        return {"total": total, "data": [self._rehearsal_dict(row, include_report=False) for row in rows]}

    def get_rehearsal(self, rehearsal_id: int) -> dict[str, Any]:
        self.sweep()
        row = self.connection.execute("SELECT * FROM food_rule_rehearsals WHERE id=?", (rehearsal_id,)).fetchone()
        if row is None:
            raise NotFoundError("预演不存在")
        return self._rehearsal_dict(row, include_report=True)

    def get_report(self, rehearsal_id: int) -> dict[str, Any]:
        rehearsal = self.get_rehearsal(rehearsal_id)
        if rehearsal["report"] is None:
            raise ConflictError(f"预演尚未生成报告（当前状态：{rehearsal['status']}）")
        if _report_checksum(rehearsal["report"]) != rehearsal["report_checksum"]:
            raise ConflictError("报告校验和不一致，报告可能被篡改，禁止审批")
        return {"rehearsal_id": rehearsal_id, "code": rehearsal["code"], "status": rehearsal["status"], "report": rehearsal["report"]}

    def list_approvals(self, rehearsal_id: int) -> list[dict[str, Any]]:
        if self.connection.execute("SELECT 1 FROM food_rule_rehearsals WHERE id=?", (rehearsal_id,)).fetchone() is None:
            raise NotFoundError("预演不存在")
        rows = self.connection.execute(
            "SELECT * FROM food_rule_approval_records WHERE rehearsal_id=? ORDER BY id",
            (rehearsal_id,),
        ).fetchall()
        records: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["permission_check"] = json.loads(item.pop("permission_check_json"))
            records.append(item)
        return records

    def _rehearsal_dict(self, row: sqlite3.Row, *, include_report: bool) -> dict[str, Any]:
        result = dict(row)
        report = json.loads(result.pop("report_json")) if result["report_json"] else None
        result["candidate_limits"] = json.loads(result.pop("candidate_limits_json"))
        result["candidate_sampling"] = json.loads(result.pop("candidate_sampling_json"))
        result["selection"] = json.loads(result.pop("selection_json"))
        result["report"] = report if include_report else None
        result["report_summary"] = summarize_report(report) if report else None
        if result["published_version_id"]:
            version = self.connection.execute(
                "SELECT version,status,checksum,published_at,published_by FROM food_rule_versions WHERE id=?",
                (result["published_version_id"],),
            ).fetchone()
            result["published_version"] = dict(version) if version else None
        else:
            result["published_version"] = None
        return result

    def sweep(self) -> int:
        """惰性清理：过期 ready 候选标记 expired；崩溃残留的 running 标记 interrupted。

        仅在被 API 显式调用时执行，服务重启本身不会触发任何状态迁移。
        """
        now_text = to_storage(self.clock.now())
        stale_text = to_storage(self.clock.now() - timedelta(seconds=STALE_RUNNING_SECONDS))
        with transaction(immediate=True) as connection:
            cursor = connection.execute(
                "UPDATE food_rule_rehearsals SET status='expired',updated_at=? "
                "WHERE status='ready' AND expires_at<?",
                (now_text, now_text),
            )
            expired = cursor.rowcount
            cursor = connection.execute(
                "UPDATE food_rule_rehearsals SET status='interrupted',updated_at=? "
                "WHERE status='running' AND heartbeat_at<?",
                (now_text, stale_text),
            )
            interrupted = cursor.rowcount
        return expired + interrupted

    # ---------- 审批与原子发布 ----------

    def decide(self, principal: Principal, rehearsal_id: int, decision: str, comment: str) -> dict[str, Any]:
        principal.require("food.rules.approve")
        self.sweep()
        now_text = to_storage(self.clock.now())

        branch: tuple[str, str, dict[str, Any]] | None = None
        result: dict[str, Any] | None = None
        with transaction(immediate=True) as connection:
            row = connection.execute("SELECT * FROM food_rule_rehearsals WHERE id=?", (rehearsal_id,)).fetchone()
            if row is None:
                raise NotFoundError("预演不存在")
            status = row["status"]

            if status != "ready":
                result_map = {
                    "approved": "duplicate",
                    "rejected": "duplicate",
                    "expired": "expired",
                    "interrupted": "interrupted",
                    "failed": "not_ready",
                    "running": "not_ready",
                }
                self._insert_approval(connection, row, principal, decision, comment, result_map[status], now_text)
                message = {
                    "approved": "该预演已经审批发布，规则版本已生效，请勿重复审批",
                    "rejected": "该预演已驳回，不能再次审批",
                    "expired": "候选规则已过期，不能发布，请重新发起预演",
                    "interrupted": "预演曾因服务中断未完成，不能发布，请重新发起",
                    "failed": "预演计算失败，不能发布，请重新发起",
                    "running": "预演尚未完成，不能发布",
                }[status]
                branch = ("conflict", message, {"status": status, "published_version_id": row["published_version_id"]})

            elif row["expires_at"] < now_text:
                connection.execute(
                    "UPDATE food_rule_rehearsals SET status='expired',updated_at=? WHERE id=?",
                    (now_text, rehearsal_id),
                )
                self._insert_approval(connection, row, principal, decision, comment, "expired", now_text)
                branch = ("conflict", "候选规则已过期，不能发布，请重新发起预演", {"status": "expired"})

            elif decision == "rejected":
                connection.execute(
                    "UPDATE food_rule_rehearsals SET status='rejected',decided_by=?,decided_by_user_id=?,"
                    "decided_at=?,decision_note=?,updated_at=? WHERE id=?",
                    (principal.display_name, principal.user_id, now_text, comment, now_text, rehearsal_id),
                )
                self._insert_approval(connection, row, principal, decision, comment, "rejected_recorded", now_text)
                connection.execute(
                    "INSERT INTO food_audit(lot_id,action,actor,payload_json,created_at) VALUES(?,?,?,?,?)",
                    (None, "rule.rehearsal.reject", principal.display_name,
                     json.dumps({"rehearsal_id": rehearsal_id, "comment": comment}, ensure_ascii=False), now_text),
                )
                result = self._rehearsal_dict(
                    connection.execute("SELECT * FROM food_rule_rehearsals WHERE id=?", (rehearsal_id,)).fetchone(),
                    include_report=False,
                )

            elif row["created_by_user_id"] is not None and row["created_by_user_id"] == principal.user_id:
                # 职责分离：发起人不能审批自己的预演
                self._insert_approval(connection, row, principal, decision, comment, "forbidden", now_text)
                branch = (
                    "forbidden",
                    "预演发起人不能审批自己的候选规则，请由其他具备审批权限的账号处理",
                    {"status": status},
                )

            else:
                # 基础版本漂移：本预演生成之后已有别的候选发布
                active = connection.execute(
                    "SELECT * FROM food_rule_versions WHERE status='active' ORDER BY version DESC LIMIT 1"
                ).fetchone()
                active_checksum = dict(active)["checksum"] if active else BASELINE_CHECKSUM
                if active_checksum != row["basis_checksum"]:
                    self._insert_approval(connection, row, principal, decision, comment, "basis_stale", now_text)
                    branch = ("conflict", "预演基于的规则版本已被新版本取代，请基于当前版本重新预演", {"status": "basis_stale"})
                else:
                    # 报告完整性校验：审批的必须是当初生成且未被篡改的报告
                    report = json.loads(row["report_json"]) if row["report_json"] else None
                    if report is None or _report_checksum(report) != row["report_checksum"]:
                        self._insert_approval(connection, row, principal, decision, comment, "not_ready", now_text)
                        branch = ("conflict", "报告缺失或校验和不一致，不能发布", {"status": "report_corrupt"})
                    else:
                        limits = json.loads(row["candidate_limits_json"])
                        sampling = json.loads(row["candidate_sampling_json"])
                        next_version = int(connection.execute("SELECT COALESCE(MAX(version),0)+1 FROM food_rule_versions").fetchone()[0])
                        # 先下线旧版本，再插入新 active：两条语句同事务，对外仍是原子切换；
                        # 同时满足部分唯一索引"任意时刻只有一个 active"
                        connection.execute(
                            "UPDATE food_rule_versions SET status='superseded',superseded_at=? WHERE status='active'",
                            (now_text,),
                        )
                        version_cursor = connection.execute(
                            "INSERT INTO food_rule_versions(version,status,limits_json,sampling_json,checksum,note,"
                            "published_by,published_by_user_id,published_at,source_rehearsal_id) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (
                                next_version,
                                "active",
                                json.dumps(limits, ensure_ascii=False),
                                json.dumps(sampling, ensure_ascii=False),
                                row["candidate_checksum"],
                                row["title"],
                                principal.display_name,
                                principal.user_id,
                                now_text,
                                rehearsal_id,
                            ),
                        )
                        new_version_id = int(version_cursor.lastrowid)
                        connection.execute(
                            "UPDATE food_rule_rehearsals SET status='approved',decided_by=?,decided_by_user_id=?,"
                            "decided_at=?,decision_note=?,published_version_id=?,updated_at=? WHERE id=?",
                            (principal.display_name, principal.user_id, now_text, comment, new_version_id, now_text, rehearsal_id),
                        )
                        self._insert_approval(connection, row, principal, decision, comment, "accepted", now_text)
                        connection.execute(
                            "INSERT INTO food_audit(lot_id,action,actor,payload_json,created_at) VALUES(?,?,?,?,?)",
                            (
                                None,
                                "rule.publish",
                                principal.display_name,
                                json.dumps(
                                    {"rehearsal_id": rehearsal_id, "version": next_version, "checksum": row["candidate_checksum"],
                                     "limits": limits, "sampling": sampling, "summary": summarize_report(report)},
                                    ensure_ascii=False,
                                ),
                                now_text,
                            ),
                        )
                        result = self._rehearsal_dict(
                            connection.execute("SELECT * FROM food_rule_rehearsals WHERE id=?", (rehearsal_id,)).fetchone(),
                            include_report=False,
                        )

        # 事务已提交：留痕不会因下面抛错而回滚
        if branch is not None:
            kind, message, context = branch
            if kind == "forbidden":
                raise PermissionDeniedError(message, context=context)
            raise ConflictError(message, context=context)
        assert result is not None
        return result

    def _insert_approval(
        self,
        connection: sqlite3.Connection,
        rehearsal: sqlite3.Row,
        principal: Principal,
        decision: str,
        comment: str,
        result: str,
        now_text: str,
    ) -> None:
        permission_check = {
            "permission": "food.rules.approve",
            "granted": principal.can("food.rules.approve"),
            "segregation": "violated"
            if rehearsal["created_by_user_id"] is not None and rehearsal["created_by_user_id"] == principal.user_id
            else "ok",
        }
        connection.execute(
            "INSERT INTO food_rule_approval_records(rehearsal_id,decision,comment,result,approver,"
            "approver_user_id,permission_check_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                rehearsal["id"],
                decision,
                comment,
                result,
                principal.display_name,
                principal.user_id,
                json.dumps(permission_check, ensure_ascii=False, sort_keys=True),
                now_text,
            ),
        )

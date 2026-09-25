from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import current_principal
from app.core.security import Principal
from app.database import get_connection
from app.food.governance import RuleGovernanceService
from app.food.governance_schemas import ApprovalDecision, RehearsalCreate

router = APIRouter(prefix="/api/food/rule-governance", tags=["农残规则治理"])


def service() -> RuleGovernanceService:
    return RuleGovernanceService(get_connection())


@router.get("/current-version")
def current_version(principal: Principal = Depends(current_principal)) -> dict:
    """查询最终规则版本（当前生效版本；未发布时返回基线说明）。"""
    principal.require("food.rules.read")
    return service().current_version()


@router.get("/versions")
def list_versions(principal: Principal = Depends(current_principal)) -> list[dict]:
    principal.require("food.rules.read")
    return service().list_versions()


@router.post("/rehearsals", status_code=201)
def create_rehearsal(payload: RehearsalCreate, principal: Principal = Depends(current_principal)) -> dict:
    """用候选规则重算代表性批次，生成差异、供应商影响、补采量和权限检查报告。"""
    return RuleGovernanceService(get_connection()).create_rehearsal(principal, payload.model_dump())


@router.get("/rehearsals")
def list_rehearsals(
    status: str | None = Query(default=None, pattern="^(running|ready|approved|rejected|expired|interrupted|failed)$"),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    principal: Principal = Depends(current_principal),
) -> dict:
    principal.require("food.rules.read")
    return service().list_rehearsals(status, limit=size, offset=(page - 1) * size)


@router.get("/rehearsals/{rehearsal_id}")
def get_rehearsal(rehearsal_id: int, principal: Principal = Depends(current_principal)) -> dict:
    """查询预演状态、候选规则、报告摘要以及最终发布的规则版本。"""
    principal.require("food.rules.read")
    return service().get_rehearsal(rehearsal_id)


@router.get("/rehearsals/{rehearsal_id}/report")
def get_report(rehearsal_id: int, principal: Principal = Depends(current_principal)) -> dict:
    """获取完整预演报告（含结论差异明细、供应商影响、建议补采量、工作量与权限检查）。"""
    principal.require("food.rules.read")
    return service().get_report(rehearsal_id)


@router.get("/rehearsals/{rehearsal_id}/approvals")
def list_approvals(rehearsal_id: int, principal: Principal = Depends(current_principal)) -> list[dict]:
    """查询全部审批意见，包括重复审批、过期、职责分离冲突等被拒绝的尝试。"""
    principal.require("food.rules.read")
    return service().list_approvals(rehearsal_id)


@router.post("/rehearsals/{rehearsal_id}/decision")
def decide(rehearsal_id: int, payload: ApprovalDecision, principal: Principal = Depends(current_principal)) -> dict:
    """审批候选规则；通过时与新版本发布在同一事务内原子完成。"""
    return RuleGovernanceService(get_connection()).decide(
        principal, rehearsal_id, payload.decision, payload.comment
    )

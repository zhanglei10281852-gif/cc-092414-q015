from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import current_principal
from app.core.security import Principal
from app.food.rehearsal import RuleRehearsalService
from app.food.schemas import ApprovalCreate, RehearsalCreate

router = APIRouter(prefix="/api/food", tags=["限值规则预演"])


def service() -> RuleRehearsalService:
    return RuleRehearsalService()


@router.get("/rule-sets")
def list_rule_sets(principal: Principal = Depends(current_principal)) -> list[dict]:
    principal.require("food.rules.read")
    return service().list_rule_sets()


@router.get("/rule-sets/current")
def current_rule_set(principal: Principal = Depends(current_principal)) -> dict:
    principal.require("food.rules.read")
    return service().current_rule_set()


@router.post("/rule-rehearsals", status_code=201)
def create_rehearsal(payload: RehearsalCreate, principal: Principal = Depends(current_principal)) -> dict:
    return service().create(payload.model_dump(), principal)


@router.get("/rule-rehearsals")
def list_rehearsals(status: str | None = Query(default=None), principal: Principal = Depends(current_principal)) -> list[dict]:
    principal.require("food.rules.read")
    return service().list_rehearsals(status)


@router.get("/rule-rehearsals/{rehearsal_id}")
def get_rehearsal(rehearsal_id: int, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("food.rules.read")
    return service().get_rehearsal(rehearsal_id)


@router.get("/rule-rehearsals/{rehearsal_id}/report")
def get_rehearsal_report(rehearsal_id: int, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("food.rules.read")
    return service().get_report(rehearsal_id)


@router.post("/rule-rehearsals/{rehearsal_id}/approval")
def approve_rehearsal(rehearsal_id: int, payload: ApprovalCreate, principal: Principal = Depends(current_principal)) -> dict:
    return service().approve(rehearsal_id, payload.model_dump(), principal)


@router.post("/rule-rehearsals/{rehearsal_id}/publish")
def publish_rehearsal(rehearsal_id: int, principal: Principal = Depends(current_principal)) -> dict:
    return service().publish(rehearsal_id, principal)

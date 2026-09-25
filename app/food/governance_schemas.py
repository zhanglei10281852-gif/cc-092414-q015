from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class CandidateLimit(BaseModel):
    analyte: str = Field(..., min_length=1, max_length=80, description="检测项目，如毒死蜱")
    category: str | None = Field(default=None, max_length=40, description="适用品类；为空表示通用限值")
    limit_mg_kg: float = Field(..., gt=0, le=100000)


class CandidateSampling(BaseModel):
    scope: Literal["global", "category", "supplier"]
    key: str | None = Field(default=None, max_length=120, description="品类名或供应商名；global 时必须为空")
    rate: float = Field(..., gt=0, lt=1, description="抽检比例，0 到 1 之间，如 0.1 表示 10%")


class RehearsalSelection(BaseModel):
    mode: Literal["stratified", "recent", "explicit"] = "stratified"
    per_supplier: int = Field(default=3, ge=1, le=100)
    limit: int = Field(default=50, ge=1, le=500)
    lot_ids: list[int] | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def check_explicit(self) -> "RehearsalSelection":
        if self.mode == "explicit" and not self.lot_ids:
            raise ValueError("显式选择批次时必须提供 lot_ids")
        return self


class RehearsalCreate(BaseModel):
    code: str = Field(..., min_length=3, max_length=64, description="预演编号，便于审计追踪")
    title: str = Field(default="", max_length=200)
    limits: list[CandidateLimit] = Field(default_factory=list, max_length=200)
    sampling: list[CandidateSampling] = Field(default_factory=list, max_length=200)
    selection: RehearsalSelection | None = None
    expires_in_minutes: int = Field(default=1440, ge=1, le=60 * 24 * 30)

    @model_validator(mode="after")
    def check_non_empty(self) -> "RehearsalCreate":
        if not self.limits and not self.sampling:
            raise ValueError("候选规则至少需要包含一项限值或抽检比例调整")
        return self


class ApprovalDecision(BaseModel):
    decision: Literal["approved", "rejected"]
    comment: str = Field(..., min_length=1, max_length=500, description="审批意见，必填并随预演永久留痕")

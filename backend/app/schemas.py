from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# 业务代码统一大写字母数字与短横线，便于扫码枪输入
CODE_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9-]{1,31}$")
# 操作键：页面生成并在断网/关页后保留，用于服务端识别同一命令
KEY_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


def _check_code(value: str) -> str:
    value = value.strip().upper()
    if not CODE_PATTERN.match(value):
        raise ValueError("需为 2-32 位大写字母、数字或短横线")
    return value


class CreateHandoffRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    tube_code: str = Field(min_length=2, max_length=32)
    from_staff_code: str = Field(min_length=2, max_length=32)
    to_staff_code: str = Field(min_length=2, max_length=32)
    operation_key: str = Field(min_length=8, max_length=64)

    @field_validator("tube_code", "from_staff_code", "to_staff_code")
    @classmethod
    def _valid_code(cls, v: str) -> str:
        return _check_code(v)

    @field_validator("operation_key")
    @classmethod
    def _valid_key(cls, v: str) -> str:
        v = v.strip()
        if not KEY_PATTERN.match(v):
            raise ValueError("需为 8-64 位字母、数字、下划线或短横线")
        return v


class AcceptHandoffRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    staff_code: str = Field(min_length=2, max_length=32)
    operation_key: str = Field(min_length=8, max_length=64)

    @field_validator("staff_code")
    @classmethod
    def _valid_code(cls, v: str) -> str:
        return _check_code(v)

    @field_validator("operation_key")
    @classmethod
    def _valid_key(cls, v: str) -> str:
        v = v.strip()
        if not KEY_PATTERN.match(v):
            raise ValueError("需为 8-64 位字母、数字、下划线或短横线")
        return v


ConfirmHandoffRequest = AcceptHandoffRequest


class StaffOut(BaseModel):
    code: str
    name: str


class HandoffOut(BaseModel):
    code: str
    tube_code: str
    from_staff: StaffOut
    to_staff: StaffOut
    status: Literal["pending", "accepted", "completed", "expired"]
    custodian_staff_code: str
    created_at: datetime
    expires_at: datetime
    accepted_at: datetime | None
    completed_at: datetime | None
    expired: bool
    seconds_remaining: int


class TubeOut(BaseModel):
    code: str
    custodian: StaffOut
    active_handoff: HandoffOut | None

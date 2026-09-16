from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from .database import get_db
from .errors import AppError, error_body
from .idempotency import run_command
from .models import ACTIVE_STATUSES, Handoff, Staff, Tube
from .schemas import (
    AcceptHandoffRequest,
    ConfirmHandoffRequest,
    CreateHandoffRequest,
)
from .service import (
    accept_handoff,
    close_expired_for_read,
    confirm_handoff,
    create_handoff,
    db_now,
    serialize_handoff,
)

router = APIRouter(prefix="/api")


def _pointer_for(location: tuple[str | int, ...]) -> str:
    # body 字段错误形如 ("body", "tube_code") -> "/tube_code"
    parts = [str(p) for p in location if p != "body"]
    return "/" + "/".join(parts) if parts else ""


async def validation_exception_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
    fields: dict[str, str] = {}
    for err in exc.errors():
        if err["type"] == "missing":
            pointer = _pointer_for(tuple(err["loc"]))
        else:
            pointer = _pointer_for(tuple(err["loc"][1:]) if err["loc"] and err["loc"][0] == "body" else tuple(err["loc"]))
        msg = err["msg"].replace("Value error, ", "")
        if pointer:
            fields[pointer] = msg
    return JSONResponse(
        status_code=422,
        content=error_body("validation_error", "请求字段校验失败", fields),
    )


def _command_response(result, response: Response) -> dict[str, Any]:
    # 重放必须还原首次响应的状态码（包括首次失败的 4xx）
    response.status_code = result.status_code
    if result.replayed:
        response.headers["X-Idempotent-Replay"] = "true"
    return result.body


@router.post("/handoffs", status_code=201)
def create(
    body: CreateHandoffRequest,
    response: Response,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    payload = body.model_dump()

    def handler(session: Session, _record) -> tuple[int, dict[str, Any]]:
        handoff = create_handoff(session, payload)
        return 201, {"handoff": serialize_handoff(session, handoff)}

    result = run_command(
        db,
        command="create_handoff",
        operation_key=body.operation_key,
        payload=payload,
        handler=handler,
    )
    return _command_response(result, response)


@router.post("/handoffs/{code}/accept")
def accept(
    code: str,
    body: AcceptHandoffRequest,
    response: Response,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    code = code.strip().upper()
    payload = {"code": code, **body.model_dump()}

    def handler(session: Session, _record) -> tuple[int, dict[str, Any]]:
        handoff = accept_handoff(session, code, body.staff_code)
        return 200, {"handoff": serialize_handoff(session, handoff)}

    result = run_command(
        db,
        command="accept_handoff",
        operation_key=body.operation_key,
        payload=payload,
        handler=handler,
    )
    return _command_response(result, response)


@router.post("/handoffs/{code}/confirm")
def confirm(
    code: str,
    body: ConfirmHandoffRequest,
    response: Response,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    code = code.strip().upper()
    payload = {"code": code, **body.model_dump()}

    def handler(session: Session, _record) -> tuple[int, dict[str, Any]]:
        handoff = confirm_handoff(session, code, body.staff_code)
        return 200, {"handoff": serialize_handoff(session, handoff)}

    result = run_command(
        db,
        command="confirm_handoff",
        operation_key=body.operation_key,
        payload=payload,
        handler=handler,
    )
    return _command_response(result, response)


@router.get("/handoffs/{code}")
def get_handoff(code: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    code = code.strip().upper()
    handoff = db.scalar(
        select(Handoff)
        .options(
            joinedload(Handoff.tube),
            joinedload(Handoff.from_staff),
            joinedload(Handoff.to_staff),
        )
        .where(Handoff.code == code)
    )
    if handoff is None:
        raise AppError(404, "handoff_not_found", "交接码不存在", fields={"/code": "未找到该交接码"})
    close_expired_for_read(db, handoff)
    now = db_now(db)
    return {"handoff": serialize_handoff(db, handoff, now)}


@router.get("/tubes/{code}")
def get_tube(code: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    code = code.strip().upper()
    tube = db.scalar(select(Tube).options(joinedload(Tube.custodian)).where(Tube.code == code))
    if tube is None:
        raise AppError(404, "tube_not_found", "冻存管不存在", fields={"/code": "未找到该管码"})
    active = db.scalar(
        select(Handoff)
        .options(joinedload(Handoff.from_staff), joinedload(Handoff.to_staff))
        .where(Handoff.tube_id == tube.id, Handoff.status.in_(ACTIVE_STATUSES))
        .order_by(Handoff.id.desc())
    )
    now = db_now(db)
    if active is not None:
        close_expired_for_read(db, active)
        active = active if active.status in ACTIVE_STATUSES else None
    return {
        "tube": {
            "code": tube.code,
            "custodian": {"code": tube.custodian.code, "name": tube.custodian.name},
            "active_handoff": serialize_handoff(db, active, now) if active else None,
        }
    }


@router.get("/health")
def health(db: Session = Depends(get_db)) -> dict[str, str]:
    db.scalar(select(1))
    return {"status": "ok"}


@router.get("/staff")
def list_staff(db: Session = Depends(get_db)) -> dict[str, Any]:
    rows = db.scalars(select(Staff).order_by(Staff.code)).all()
    return {"staff": [{"code": s.code, "name": s.name} for s in rows]}

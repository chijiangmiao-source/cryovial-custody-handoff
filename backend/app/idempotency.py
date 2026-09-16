from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .errors import AppError, error_body
from .models import CommandRecord


def request_fingerprint(command: str, payload: dict[str, Any]) -> str:
    """对命令参数做规范化指纹，用于识别同键异参。"""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(f"{command}\n{canonical}".encode("utf-8")).hexdigest()


@dataclass
class CommandResult:
    status_code: int
    body: dict[str, Any]
    replayed: bool


Handler = Callable[[Session, CommandRecord], tuple[int, dict[str, Any]]]


def run_command(
    db: Session,
    *,
    command: str,
    operation_key: str,
    payload: dict[str, Any],
    handler: Handler,
) -> CommandResult:
    """
    以操作键执行命令并保证：
    - 同键同参重放：返回首次状态码与响应体；
    - 同键异参：409 conflict，不执行任何业务逻辑；
    - 首次执行（含业务失败）的结果持久化后随事务一起提交。

    利用 (operation_key) 唯一约束 + 行锁串行化并发重发：
    第二个请求会阻塞到首个请求提交，随后读到首次结果。
    """
    fingerprint = request_fingerprint(command, payload)

    while True:
        record = db.scalar(
            select(CommandRecord)
            .where(CommandRecord.operation_key == operation_key)
            .with_for_update()
        )
        if record is not None:
            if record.request_hash != fingerprint:
                raise AppError(
                    409,
                    "idempotency_conflict",
                    "同一操作键已用于不同参数的请求，拒绝执行",
                    fields={"/operation_key": "操作键与首次请求参数不一致"},
                )
            return CommandResult(record.status_code, dict(record.response), replayed=True)

        record = CommandRecord(
            operation_key=operation_key,
            command=command,
            request_hash=fingerprint,
            status_code=0,
            response={},
        )
        db.add(record)
        try:
            db.flush()
        except IntegrityError:
            # 并发首次插入：回退后重读已提交记录并重放
            db.rollback()
            continue
        break

    try:
        status_code, body = handler(db, record)
    except AppError as exc:
        # 业务失败同样记录首次结果（例如扫码时发现已到期），随已发生的状态封闭一并提交
        record.status_code = exc.status_code
        record.response = error_body(exc.code, exc.message, exc.fields)
        db.commit()
        raise

    record.status_code = status_code
    record.response = body
    db.commit()
    return CommandResult(status_code, body, replayed=False)

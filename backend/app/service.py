from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .config import Settings, get_settings
from .errors import AppError
from .ledger import append_transfer, ensure_tube_baseline
from .models import ACTIVE_STATUSES, Handoff, Staff, Tube

# 去除易混字符 I/O/0/1
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def db_now(db: Session) -> datetime:
    """以数据库时钟为唯一权威时间源。"""
    return db.scalar(select(func.statement_timestamp()))


def _generate_code(length: int) -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(length))


def _lock_staff(db: Session, code: str, pointer: str) -> Staff:
    # 人员记录在本流程中只读，无需行锁
    staff = db.scalar(select(Staff).where(Staff.code == code))
    if staff is None:
        raise AppError(404, "staff_not_found", "人员不存在", fields={pointer: "未找到该工号"})
    return staff


def _lock_tube(db: Session, code: str, pointer: str = "/tube_code") -> Tube:
    # of=Tube：只冻结核存管行，避免 FOR UPDATE 落到 joinedload 外连接的可空侧
    tube = db.scalar(select(Tube).where(Tube.code == code).with_for_update(of=Tube))
    if tube is None:
        raise AppError(404, "tube_not_found", "冻存管不存在", fields={pointer: "未找到该管码"})
    return tube


def _lock_handoff_chain(db: Session, code: str) -> tuple[Tube, Handoff, datetime]:
    """
    按 冻存管 -> 交接 的固定顺序加锁，避免跨事务死锁。
    """
    now = db_now(db)
    ref = db.execute(select(Handoff.id, Handoff.tube_id).where(Handoff.code == code)).first()
    if ref is None:
        raise AppError(404, "handoff_not_found", "交接码不存在", fields={"/code": "未找到该交接码"})
    handoff_id, tube_id = ref
    tube = db.scalar(select(Tube).where(Tube.id == tube_id).with_for_update(of=Tube))
    handoff = db.scalar(
        select(Handoff).where(Handoff.id == handoff_id).with_for_update(of=Handoff)
    )
    return tube, handoff, now


def _close_if_expired(handoff: Handoff, now: datetime) -> bool:
    """到期判定：截止时刻（== expires_at）仍有效，仅封闭未完成交接且不触碰保管人。"""
    if handoff.status in ACTIVE_STATUSES and now > handoff.expires_at:
        handoff.status = "expired"
        return True
    return False


def create_handoff(db: Session, payload: dict[str, str]) -> Handoff:
    settings: Settings = get_settings()
    now = db_now(db)

    # 固定顺序：冻存管 -> 双方人员 -> 该管全部交接记录
    tube = _lock_tube(db, payload["tube_code"])
    from_staff = _lock_staff(db, payload["from_staff_code"], "/from_staff_code")
    to_staff = _lock_staff(db, payload["to_staff_code"], "/to_staff_code")

    if from_staff.id == to_staff.id:
        raise AppError(
            422,
            "same_party",
            "转出员与接收员不能为同一人",
            fields={"/to_staff_code": "接收员不能与转出员相同"},
        )
    if tube.custodian_id != from_staff.id:
        raise AppError(
            409,
            "not_custodian",
            "转出员不是该冻存管的当前保管人，无权发起交接",
            fields={"/from_staff_code": "该人员当前不持有此冻存管"},
        )

    existing = db.scalars(
        select(Handoff)
        .where(Handoff.tube_id == tube.id)
        .order_by(Handoff.id)
        .with_for_update(of=Handoff)
    ).all()
    for handoff in existing:
        _close_if_expired(handoff, now)
        if handoff.status in ACTIVE_STATUSES:
            raise AppError(
                409,
                "active_handoff_exists",
                "该冻存管已有进行中的交接，完成或到期后才能再次发起",
                fields={"/tube_code": "存在未完成的交接"},
            )

    expires_at = now + timedelta(seconds=settings.handoff_ttl_seconds)
    # 部分唯一索引兜底并发竞争；碰撞时换码重试
    for _ in range(5):
        code = _generate_code(settings.code_length)
        handoff = Handoff(
            code=code,
            tube_id=tube.id,
            from_staff_id=from_staff.id,
            to_staff_id=to_staff.id,
            status="pending",
            created_at=now,
            expires_at=expires_at,
        )
        try:
            with db.begin_nested():
                db.add(handoff)
                db.flush()
            break
        except IntegrityError as exc:
            if "ux_handoffs_active_tube" in str(exc.orig):
                raise AppError(
                    409,
                    "active_handoff_exists",
                    "该冻存管已有进行中的交接，完成或到期后才能再次发起",
                    fields={"/tube_code": "存在未完成的交接"},
                ) from exc
            # code 唯一碰撞则换码重试（保存点已回滚）
            continue
    else:
        raise AppError(500, "code_generation_failed", "交接码生成失败，请重试")

    db.refresh(handoff)
    return handoff


def accept_handoff(db: Session, code: str, staff_code: str) -> Handoff:
    tube, handoff, now = _lock_handoff_chain(db, code)

    # 每步都按数据库时间先判定到期
    if _close_if_expired(handoff, now) or handoff.status == "expired":
        raise AppError(
            410,
            "handoff_expired",
            "交接码已到期，未能完成交接，保管人不变",
            fields={"/code": "交接码超过十分钟有效期"},
        )

    # 身份校验先于状态短路：交接完成后，非指定接收员再次扫码必须提示无权，
    # 不能因为 completed 的幂等返回而显示“接受成功”
    if handoff.to_staff_id != _staff_id(db, staff_code):
        raise AppError(
            403,
            "not_receiver",
            "只有指定的接收员可以扫码接受",
            fields={"/staff_code": "该人员不是本次交接的接收员"},
        )

    if handoff.status == "completed":
        # 指定接收员本人在完成后重扫：幂等返回当前完成状态（扫码器重放语义）
        return handoff

    if handoff.status == "pending":
        handoff.status = "accepted"
        handoff.accepted_at = now
        db.flush()
    # accepted：重复扫码幂等返回当前状态（已接受）
    return handoff


def confirm_handoff(db: Session, code: str, staff_code: str) -> Handoff:
    tube, handoff, now = _lock_handoff_chain(db, code)

    if _close_if_expired(handoff, now) or handoff.status == "expired":
        raise AppError(
            410,
            "handoff_expired",
            "交接码已到期，未能完成交接，保管人不变",
            fields={"/code": "交接码超过十分钟有效期"},
        )

    staff = db.scalar(select(Staff).where(Staff.code == staff_code))
    if staff is None or handoff.from_staff_id != staff.id:
        raise AppError(
            403,
            "not_owner",
            "只有发起交接的转出员可以最终确认",
            fields={"/staff_code": "该人员不是本次交接的转出员"},
        )

    if handoff.status == "completed":
        return handoff
    if handoff.status == "pending":
        raise AppError(
            409,
            "not_accepted",
            "接收员尚未扫码接受，不能确认",
            fields={"/code": "等待接收员扫码接受"},
        )

    # accepted：仅当转出员仍是当前保管人时，才允许确认
    if tube.custodian_id != handoff.from_staff_id:
        raise AppError(
            409,
            "custodian_changed",
            "冻存管保管人已变更，旧交接不能覆盖现有归属",
            fields={"/code": "该交接已失效"},
        )

    # 同一事务内：先校验账本尾部 == 当前保管人 == 转出员，再原子写入
    # 不可变保管变更、新保管人与完成状态，任何一步失败整体回滚（不留下事件）
    event = append_transfer(db, tube=tube, handoff=handoff, now=now)
    tube.custodian_id = handoff.to_staff_id
    handoff.status = "completed"
    handoff.completed_at = event.effective_at
    db.flush()
    return handoff


def close_expired_for_read(db: Session, handoff: Handoff) -> None:
    """读路径上顺手封闭已到期交接，保证页面与数据库状态一致。"""
    now = db_now(db)
    if _close_if_expired(handoff, now):
        db.commit()


def _staff_id(db: Session, staff_code: str) -> int | None:
    return db.scalar(select(Staff.id).where(Staff.code == staff_code))


def serialize_handoff(db: Session, handoff: Handoff, now: datetime | None = None) -> dict[str, Any]:
    now = now or db_now(db)
    tube = handoff.tube or db.get(Tube, handoff.tube_id)
    from_staff = handoff.from_staff or db.get(Staff, handoff.from_staff_id)
    to_staff = handoff.to_staff or db.get(Staff, handoff.to_staff_id)
    custodian = db.get(Staff, tube.custodian_id)

    status = handoff.status
    if status in ACTIVE_STATUSES and now > handoff.expires_at:
        status = "expired"
    remaining = (handoff.expires_at - now).total_seconds()

    return {
        "code": handoff.code,
        "tube_code": tube.code,
        "from_staff": {"code": from_staff.code, "name": from_staff.name},
        "to_staff": {"code": to_staff.code, "name": to_staff.name},
        "status": status,
        "custodian_staff_code": custodian.code,
        "created_at": handoff.created_at.isoformat(),
        "expires_at": handoff.expires_at.isoformat(),
        "accepted_at": handoff.accepted_at.isoformat() if handoff.accepted_at else None,
        "completed_at": handoff.completed_at.isoformat() if handoff.completed_at else None,
        "expired": status == "expired",
        "seconds_remaining": max(0, int(remaining)),
    }

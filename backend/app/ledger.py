"""
不可变保管账本（custody_events）。

不变量：
- 每管一条从 seq=0（上线基线）开始、序号连续的事件链；
- 基线只代表上线/升级那一刻的现状，不对应任何交接；
- 每次确认交接时，在**原事务内**追加 seq+1 的 transfer 事件，生效时间取数据库时钟；
- 追加前必须满足“账本尾部保管人 == 冻存管当前保管人 == 本次转出员”，
  且新事件接续到本次转入人，否则拒绝提交（不写事件、不改状态）；
- 任意时刻归属 = effective_at <= 该时刻的最后一条事件；等于生效时刻即已生效。

账本只增不改。读历史时若发现缺基线或断链，返回可识别错误，绝不投影出可能误导的结果。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from .errors import AppError
from .models import CustodyEvent, Handoff, Staff, Tube

BASELINE_KIND = "baseline"
TRANSFER_KIND = "transfer"


def ensure_tube_baseline(db: Session, tube: Tube, now: datetime) -> CustodyEvent:
    """为新冻存管写入 seq=0 上线基线（幂等：已存在则直接返回）。"""
    existing = db.scalar(
        select(CustodyEvent).where(CustodyEvent.tube_id == tube.id, CustodyEvent.seq == 0)
    )
    if existing is not None:
        return existing
    event = CustodyEvent(
        tube_id=tube.id,
        seq=0,
        kind=BASELINE_KIND,
        custodian_id=tube.custodian_id,
        effective_at=now,
    )
    db.add(event)
    db.flush()
    return event


def backfill_baselines(db: Session) -> int:
    """
    旧数据升级：为所有尚无账本的冻存管补一条仅代表“升级时现状”的基线。
    基线生效时刻即账本可追溯起点（取数据库时钟），更早的时刻没有任何历史证据。
    """
    now = db.scalar(select(func.statement_timestamp()))
    missing = db.scalars(
        select(Tube)
        .outerjoin(CustodyEvent, (CustodyEvent.tube_id == Tube.id) & (CustodyEvent.seq == 0))
        .where(CustodyEvent.id.is_(None))
        .order_by(Tube.id)
    ).all()
    for tube in missing:
        db.add(
            CustodyEvent(
                tube_id=tube.id,
                seq=0,
                kind=BASELINE_KIND,
                custodian_id=tube.custodian_id,
                effective_at=now,
            )
        )
    db.flush()
    return len(missing)


def append_transfer(
    db: Session, *, tube: Tube, handoff: Handoff, now: datetime
) -> CustodyEvent:
    """
    在确认交接的原事务内追加不可变保管变更。
    调用方已持有“冻存管行 → 交接行”锁；此处再锁账本尾部做最后兜底。
    """
    events = db.scalars(
        select(CustodyEvent)
        .where(CustodyEvent.tube_id == tube.id)
        .order_by(CustodyEvent.seq)
        .with_for_update(of=CustodyEvent)
    ).all()

    if not events:
        raise AppError(
            409,
            "custody_baseline_missing",
            "该冻存管缺少上线基线，保管账本不可追溯，拒绝追加保管变更",
            fields={"/tube_code": "缺少 seq=0 基线事件"},
        )

    # 内部一致性：序号连续、首条为基线、每条转移都从前一保管人接续
    _validate_chain_or_raise(events)

    tail = events[-1]
    if tail.custodian_id != tube.custodian_id or tail.custodian_id != handoff.from_staff_id:
        # 账本尾部与当前保管人不一致（例如库外纠正），新事件无法从转出员合法接续
        raise AppError(
            409,
            "custody_chain_broken",
            "保管账本尾部与当前保管人不一致，拒绝在断链上追加保管变更",
            fields={"/code": "账本尾部保管人与冻存管当前保管人不符"},
        )

    event = CustodyEvent(
        tube_id=tube.id,
        seq=len(events),
        kind=TRANSFER_KIND,
        custodian_id=handoff.to_staff_id,
        from_staff_id=handoff.from_staff_id,
        to_staff_id=handoff.to_staff_id,
        handoff_id=handoff.id,
        handoff_code=handoff.code,
        effective_at=now,
    )
    db.add(event)
    db.flush()
    return event


def load_validated_chain(db: Session, tube: Tube) -> list[CustodyEvent]:
    """读出整条链并校验；缺基线/断链/尾部与当前保管人不一致时抛出可识别错误。"""
    events = db.scalars(
        select(CustodyEvent)
        .options(
            joinedload(CustodyEvent.custodian),
            joinedload(CustodyEvent.from_staff),
            joinedload(CustodyEvent.to_staff),
        )
        .where(CustodyEvent.tube_id == tube.id)
        .order_by(CustodyEvent.seq)
    ).all()

    if not events:
        raise AppError(
            409,
            "custody_baseline_missing",
            "该冻存管没有上线基线，不存在可追溯的保管历史",
            fields={"/code": "缺少 seq=0 基线事件"},
        )
    _validate_chain_or_raise(events)
    # 账本尾部必须与当前保管人一致；不一致说明历史被库外改动（删除/篡改），
    # 此时任何投影都可能误导调查，直接拒绝。
    if events[-1].custodian_id != tube.custodian_id:
        raise AppError(
            409,
            "custody_chain_broken",
            "保管账本尾部与冻存管当前保管人不一致，历史证据链已断裂，拒绝给出查询结果",
        )
    return events


def _validate_chain_or_raise(events: list[CustodyEvent]) -> None:
    first = events[0]
    if first.seq != 0 or first.kind != BASELINE_KIND:
        raise AppError(
            409,
            "custody_chain_broken",
            "保管账本缺少合法的上线基线（seq=0），历史证据不可信",
        )
    if (
        first.from_staff_id is not None
        or first.to_staff_id is not None
        or first.handoff_id is not None
        or first.handoff_code is not None
    ):
        raise AppError(
            409,
            "custody_chain_broken",
            "上线基线被污染（基线不得携带交接或人员变更）",
        )

    prev = first
    for expected_seq, event in enumerate(events):
        if event.seq != expected_seq:
            raise AppError(
                409,
                "custody_chain_broken",
                f"保管账本序号不连续：期望 seq={expected_seq}，实际 seq={event.seq}",
            )
        if event.custodian_id is None:
            raise AppError(409, "custody_chain_broken", "保管账本事件缺少保管人")
        if event.kind == BASELINE_KIND:
            if expected_seq != 0:
                raise AppError(409, "custody_chain_broken", "基线事件只能位于 seq=0")
        elif event.kind == TRANSFER_KIND:
            if event.from_staff_id != prev.custodian_id:
                raise AppError(
                    409,
                    "custody_chain_broken",
                    f"seq={event.seq} 的转出人与上一事件保管人不一致，账本断链",
                )
            if event.to_staff_id != event.custodian_id:
                raise AppError(
                    409,
                    "custody_chain_broken",
                    f"seq={event.seq} 的生效保管人与转入人不一致，账本断链",
                )
            if not event.handoff_code or event.handoff_id is None:
                raise AppError(
                    409,
                    "custody_chain_broken",
                    f"seq={event.seq} 缺少对应交接码，账本证据不完整",
                )
        else:
            raise AppError(409, "custody_chain_broken", f"未知账本事件类型：{event.kind}")
        prev = event


def project(events: list[CustodyEvent], at: datetime) -> CustodyEvent:
    """投影指定时刻的生效事件：effective_at <= at 的最后一条（等于即生效）。"""
    effective = [e for e in events if e.effective_at <= at]
    return effective[-1]


def serialize_event(event: CustodyEvent, active: bool) -> dict[str, Any]:
    return {
        "seq": event.seq,
        "kind": event.kind,
        "effective_at": event.effective_at.isoformat(),
        "custodian": {"code": event.custodian.code, "name": event.custodian.name},
        "from_staff": (
            {"code": event.from_staff.code, "name": event.from_staff.name}
            if event.from_staff
            else None
        ),
        "to_staff": (
            {"code": event.to_staff.code, "name": event.to_staff.name}
            if event.to_staff
            else None
        ),
        "handoff_code": event.handoff_code,
        "active_at_point": active,
    }


def build_history(
    db: Session, *, tube_code: str, at: datetime | None
) -> dict[str, Any]:
    tube = db.scalar(
        select(Tube).options(joinedload(Tube.custodian)).where(Tube.code == tube_code)
    )
    if tube is None:
        raise AppError(404, "tube_not_found", "冻存管不存在", fields={"/code": "未找到该管码"})

    events = load_validated_chain(db, tube)
    now = db.scalar(select(func.statement_timestamp()))
    point = at or now
    baseline = events[0]

    body: dict[str, Any] = {
        "tube_code": tube.code,
        "queried_at": now.isoformat(),
        "point_in_time": point.isoformat(),
        "traceable_since": baseline.effective_at.isoformat(),
        "timeline": [serialize_event(e, active=False) for e in events],
    }

    # 早于可追溯起点：直接说明无历史证据，不猜测任何保管人
    if point < baseline.effective_at:
        body.update(
            {
                "evidence_available": False,
                "custodian": None,
                "event_seq": None,
                "handoff_code": None,
                "previous_change": None,
                "next_change": None,
            }
        )
        return body

    active_event = project(events, point)
    index = active_event.seq
    previous = events[index - 1] if index > 0 else None
    next_event = events[index + 1] if index + 1 < len(events) else None
    body["timeline"][index]["active_at_point"] = True
    body.update(
        {
            "evidence_available": True,
            "custodian": {"code": active_event.custodian.code, "name": active_event.custodian.name},
            "event_seq": active_event.seq,
            "handoff_code": active_event.handoff_code,
            "previous_change": serialize_event(previous, active=False) if previous else None,
            "next_change": serialize_event(next_event, active=False) if next_event else None,
        }
    )
    return body

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


class HandoffStatus(str, enum.Enum):
    # 已创建，等待接收员扫码接受
    pending = "pending"
    # 接收员已扫码接受，等待转出员最终确认
    accepted = "accepted"
    # 转出员已确认，保管人已原子切换为接收员
    completed = "completed"
    # 超过有效期仍未完成，仅封闭交接，保管人不变
    expired = "expired"


ACTIVE_STATUSES = (HandoffStatus.pending.value, HandoffStatus.accepted.value)


class Staff(Base):
    __tablename__ = "staff"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("statement_timestamp()"), nullable=False
    )


class Tube(Base):
    __tablename__ = "tubes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    custodian_id: Mapped[int] = mapped_column(
        ForeignKey("staff.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("statement_timestamp()"), nullable=False
    )

    custodian: Mapped[Staff] = relationship(lazy="joined")


class Handoff(Base):
    __tablename__ = "handoffs"
    __table_args__ = (
        # 数据库层面兜底：同一冻存管最多一个未完成（活动）交接
        Index(
            "ux_handoffs_active_tube",
            "tube_id",
            unique=True,
            postgresql_where=text("status IN ('pending', 'accepted')"),
        ),
        CheckConstraint(
            "status IN ('pending', 'accepted', 'completed', 'expired')",
            name="ck_handoffs_status",
        ),
        CheckConstraint("from_staff_id <> to_staff_id", name="ck_handoffs_parties"),
        CheckConstraint(
            "(status = 'completed') = (completed_at IS NOT NULL)",
            name="ck_handoffs_completed_at",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(16), unique=True, nullable=False)
    tube_id: Mapped[int] = mapped_column(
        ForeignKey("tubes.id", ondelete="RESTRICT"), nullable=False
    )
    from_staff_id: Mapped[int] = mapped_column(
        ForeignKey("staff.id", ondelete="RESTRICT"), nullable=False
    )
    to_staff_id: Mapped[int] = mapped_column(
        ForeignKey("staff.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("statement_timestamp()"), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    tube: Mapped[Tube] = relationship(lazy="joined")
    from_staff: Mapped[Staff] = relationship(foreign_keys=[from_staff_id], lazy="joined")
    to_staff: Mapped[Staff] = relationship(foreign_keys=[to_staff_id], lazy="joined")


class CustodyEvent(Base):
    """
    不可变保管账本：每管一条单调序号链。
    - seq=0 为上线基线，仅代表系统上线（或旧数据升级）时的现状，没有交接码、没有转出/接收人；
    - 其后每条 transfer 都在“确认交接”的原事务内追加，生效时刻取自数据库时钟，
      交接完成即保管变更，提交前对外不可见。
    账本只增不改：任何更正都应通过新事件体现，而不是修改历史行。
    """

    __tablename__ = "custody_events"
    __table_args__ = (
        # 每管序号严格唯一单调，作为投影任意时刻归属的依据
        UniqueConstraint("tube_id", "seq", name="ux_custody_events_tube_seq"),
        CheckConstraint("seq >= 0", name="ck_custody_events_seq"),
        CheckConstraint("kind IN ('baseline', 'transfer')", name="ck_custody_events_kind"),
        CheckConstraint(
            "(kind = 'baseline' AND from_staff_id IS NULL AND to_staff_id IS NULL "
            "AND handoff_id IS NULL AND handoff_code IS NULL) "
            "OR (kind = 'transfer' AND from_staff_id IS NOT NULL AND to_staff_id IS NOT NULL "
            "AND handoff_id IS NOT NULL AND handoff_code IS NOT NULL)",
            name="ck_custody_events_shape",
        ),
        CheckConstraint("custodian_id IS NOT NULL", name="ck_custody_events_custodian"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tube_id: Mapped[int] = mapped_column(
        ForeignKey("tubes.id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    # 该事件生效后的保管人：基线即上线时保管人，转移即本次接收人
    custodian_id: Mapped[int] = mapped_column(
        ForeignKey("staff.id", ondelete="RESTRICT"), nullable=False
    )
    from_staff_id: Mapped[int | None] = mapped_column(
        ForeignKey("staff.id", ondelete="RESTRICT"), nullable=True
    )
    to_staff_id: Mapped[int | None] = mapped_column(
        ForeignKey("staff.id", ondelete="RESTRICT"), nullable=True
    )
    handoff_id: Mapped[int | None] = mapped_column(
        ForeignKey("handoffs.id", ondelete="CASCADE"), nullable=True
    )
    # 快照交接码：账本自包含，即使交接行被清理也能读出“对应交接码”
    handoff_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    effective_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("statement_timestamp()"), nullable=False
    )

    custodian: Mapped[Staff] = relationship(foreign_keys=[custodian_id], lazy="joined")
    from_staff: Mapped[Staff | None] = relationship(foreign_keys=[from_staff_id], lazy="joined")
    to_staff: Mapped[Staff | None] = relationship(foreign_keys=[to_staff_id], lazy="joined")


class CommandRecord(Base):
    """每个操作键的首次结果，保证断网/扫码器重发时重放一致、同键异参冲突。"""

    __tablename__ = "command_records"
    __table_args__ = (
        UniqueConstraint("operation_key", name="ux_command_operation_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    operation_key: Mapped[str] = mapped_column(String(64), nullable=False)
    command: Mapped[str] = mapped_column(String(32), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    response: Mapped[dict] = mapped_column(JSONB, nullable=False)
    handoff_id: Mapped[int | None] = mapped_column(
        ForeignKey("handoffs.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("statement_timestamp()"), nullable=False
    )

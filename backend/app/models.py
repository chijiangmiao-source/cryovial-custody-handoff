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

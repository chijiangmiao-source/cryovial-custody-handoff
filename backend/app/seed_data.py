from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Staff, Tube

# 夜班演示/验收用初始数据
SEED_STAFF = [
    ("S001", "张敏（夜班转出员）"),
    ("S002", "李强（接收员）"),
    ("S003", "王芳（接收员）"),
]
SEED_TUBES = [
    ("T-1001", "S001"),
    ("T-1002", "S001"),
]


def seed_data_if_needed(db: Session) -> None:
    """幂等写入演示人员与冻存管。"""
    staff_by_code: dict[str, Staff] = {}
    for code, name in SEED_STAFF:
        staff = db.scalar(select(Staff).where(Staff.code == code))
        if staff is None:
            staff = Staff(code=code, name=name)
            db.add(staff)
            db.flush()
        staff_by_code[code] = staff

    for tube_code, custodian_code in SEED_TUBES:
        if db.scalar(select(Tube).where(Tube.code == tube_code)) is None:
            db.add(Tube(code=tube_code, custodian_id=staff_by_code[custodian_code].id))
    db.commit()

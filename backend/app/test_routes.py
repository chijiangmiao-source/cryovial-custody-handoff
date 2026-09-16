from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from .config import get_settings
from .database import get_db
from .errors import AppError
from .seed_data import seed_data_if_needed

# 仅当 SAMPLE_ENABLE_TEST_RESET=true 时挂载（见 main.py），供 Playwright 与 verify 验收使用
router = APIRouter(prefix="/api/test")


def _ensure_enabled() -> None:
    if not get_settings().enable_test_reset:
        raise AppError(404, "not_found", "不存在")


@router.post("/reset", status_code=204)
def reset(db: Session = Depends(get_db)):
    _ensure_enabled()
    db.execute(text("TRUNCATE command_records, handoffs, tubes, staff RESTART IDENTITY CASCADE"))
    db.commit()
    seed_data_if_needed(db)


@router.post("/handoffs/{code}/expire", status_code=204)
def force_expire(code: str, db: Session = Depends(get_db)):
    """把指定交接的到期时刻移到数据库当前时间之前 1 秒，模拟自然到期。"""
    _ensure_enabled()
    code = code.strip().upper()
    result = db.execute(
        text(
            "UPDATE handoffs SET expires_at = statement_timestamp() - interval '1 second' "
            "WHERE code = :c"
        ),
        {"c": code},
    )
    db.commit()
    if result.rowcount == 0:
        raise AppError(404, "handoff_not_found", "交接码不存在", fields={"/code": "未找到该交接码"})

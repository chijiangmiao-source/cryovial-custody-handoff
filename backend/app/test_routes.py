from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, Header
from sqlalchemy import text
from sqlalchemy.orm import Session

from .config import get_settings
from .database import get_db
from .errors import AppError
from .seed_data import seed_data_if_needed

# 仅当 SAMPLE_ENABLE_TEST_RESET=true 且 SAMPLE_TEST_RESET_TOKEN 非空时挂载（见 main.create_app）
router = APIRouter(prefix="/api/test")


def _ensure_enabled(x_test_token: str | None) -> None:
    settings = get_settings()
    # 双重保险：未显式开启或未配置令牌时，接口等同不存在，绝不暴露清空数据的能力
    if not settings.enable_test_reset or not settings.test_reset_token:
        raise AppError(404, "not_found", "不存在")
    if not x_test_token or not secrets.compare_digest(x_test_token, settings.test_reset_token):
        raise AppError(401, "unauthorized", "验收接口令牌无效或缺失")


@router.post("/reset", status_code=204)
def reset(
    db: Session = Depends(get_db),
    x_test_token: str | None = Header(default=None, alias="X-Test-Token"),
):
    _ensure_enabled(x_test_token)
    db.execute(text("TRUNCATE command_records, handoffs, tubes, staff RESTART IDENTITY CASCADE"))
    db.commit()
    seed_data_if_needed(db)


@router.post("/handoffs/{code}/expire", status_code=204)
def force_expire(
    code: str,
    db: Session = Depends(get_db),
    x_test_token: str | None = Header(default=None, alias="X-Test-Token"),
):
    """把指定交接的到期时刻移到数据库当前时间之前 1 秒，模拟自然到期。"""
    _ensure_enabled(x_test_token)
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

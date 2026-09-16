from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError

from .api import router, validation_exception_handler
from .config import get_settings
from .database import SessionLocal, create_all, schema_bootstrap_lock
from .errors import AppError, app_error_handler
from .ledger import backfill_baselines
from .seed_data import seed_data_if_needed


def seed() -> None:
    db = SessionLocal()
    try:
        seed_data_if_needed(db)
        # 旧数据升级：为历史遗留、尚无账本的冻存管补齐“仅代表升级时现状”的基线
        backfill_baselines(db)
        db.commit()
    finally:
        db.close()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # 公开后端与验收后端并发启动时，咨询锁保证只有一个进程执行建表与种子，
    # 后到进程等其完成后走幂等检查，避免并发 DDL 崩溃导致入口 502
    with schema_bootstrap_lock():
        create_all()
        seed()
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="冷冻样本夜班交接服务", version="1.0.0", lifespan=lifespan)
    app.include_router(router)
    if get_settings().enable_test_reset:
        # 仅验收/测试环境且配置了令牌时挂载：重置数据与模拟到期
        from .test_routes import router as test_router

        app.include_router(test_router)
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)

    @app.get("/")
    def root() -> dict[str, str]:
        return {"service": "frozen-sample-handoff", "docs": "/docs"}

    return app


app = create_app()

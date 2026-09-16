from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings


class Base(DeclarativeBase):
    pass


_settings = get_settings()

engine = create_engine(
    _settings.database_url,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# 公开后端与验收后端可能对同一个全新库并发启动：并发 CREATE TABLE 会在
# PostgreSQL 系统目录上冲突（duplicate key pg_type_typname_nsp_index），
# 失败进程退出后经 nginx 访问即为 502。用固定会话级咨询锁串行化引导段。
SCHEMA_BOOTSTRAP_LOCK_KEY = 7183902541


@contextmanager
def schema_bootstrap_lock():
    """持锁直到 DDL 与种子数据全部完成；会话级锁在 commit 后仍保持，连接关闭即释放。"""
    conn = engine.connect()
    conn.execute(text("SELECT pg_advisory_lock(:key)"), {"key": SCHEMA_BOOTSTRAP_LOCK_KEY})
    conn.commit()
    try:
        yield
    finally:
        try:
            conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": SCHEMA_BOOTSTRAP_LOCK_KEY})
            conn.commit()
        finally:
            conn.close()


def create_all() -> None:
    # 导入以在 metadata 上注册全部模型（副作用导入）
    from . import models  # noqa: F401

    Base.metadata.create_all(bind=engine)

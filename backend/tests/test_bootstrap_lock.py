"""引导段串行化：公开后端与验收后端并发冷启动时，咨询锁把建表/种子串行化。"""
from __future__ import annotations

import threading
import time

import pytest
from sqlalchemy import text

from app.database import SCHEMA_BOOTSTRAP_LOCK_KEY, engine, schema_bootstrap_lock


def test_bootstrap_lock_serializes_concurrent_startups():
    order: list[str] = []
    entered = threading.Event()
    release = threading.Event()

    def first() -> None:
        with schema_bootstrap_lock():
            order.append("first-acquired")
            entered.set()
            # 持锁直到测试主线程确认第二个会话正在等待
            release.wait(timeout=5)
            order.append("first-released")

    t = threading.Thread(target=first)
    t.start()
    assert entered.wait(timeout=5)

    # 持锁期间，另一会话能检测到该锁正被占用
    # （单 bigint 咨询锁在 pg_locks 中为 classid=key 高32位、objid=key 低32位、objsubid=1）
    oid = SCHEMA_BOOTSTRAP_LOCK_KEY % (2**32)
    with engine.connect() as conn:
        locked = conn.execute(
            text(
                "SELECT EXISTS (SELECT 1 FROM pg_locks "
                "WHERE locktype = 'advisory' AND granted "
                "AND classid = 1 AND objid = :k AND objsubid = 1)"
            ),
            {"k": oid},
        ).scalar()
    assert locked is True

    def second() -> None:
        with schema_bootstrap_lock():
            # 必须在 first 释放之后才能拿到
            order.append("second-acquired")

    t2 = threading.Thread(target=second)
    t2.start()
    time.sleep(0.5)
    assert order == ["first-acquired"]  # 第二个会话仍在等待，没有并发进入引导段

    release.set()
    t.join(timeout=5)
    t2.join(timeout=5)
    assert order == ["first-acquired", "first-released", "second-acquired"]

    # 锁已释放：无残留 advisory lock
    oid = SCHEMA_BOOTSTRAP_LOCK_KEY % (2**32)
    with engine.connect() as conn:
        leftover = conn.execute(
            text(
                "SELECT count(*) FROM pg_locks "
                "WHERE locktype = 'advisory' AND classid = 1 AND objid = :k AND objsubid = 1"
            ),
            {"k": oid},
        ).scalar()
    assert leftover == 0


def test_bootstrap_lock_releases_after_exception():
    with pytest.raises(RuntimeError):
        with schema_bootstrap_lock():
            raise RuntimeError("boom")
    # 异常后锁必须已释放，可再次获取
    with schema_bootstrap_lock():
        pass

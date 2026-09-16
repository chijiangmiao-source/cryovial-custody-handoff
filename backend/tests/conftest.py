from __future__ import annotations

import os

# 必须在导入应用前指定测试库与验收钩子（带令牌）；可用 SAMPLE_DATABASE_URL 覆盖
os.environ.setdefault(
    "SAMPLE_DATABASE_URL",
    "postgresql+psycopg://postgres@localhost:5432/sample_test",
)
os.environ["SAMPLE_ENABLE_TEST_RESET"] = "true"
os.environ.setdefault("SAMPLE_TEST_RESET_TOKEN", "test-token")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.database import SessionLocal, create_all, engine  # noqa: E402
from app.main import app, seed  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _schema():
    create_all()
    seed()
    yield
    engine.dispose()


@pytest.fixture(autouse=True)
def _clean_tables():
    with engine.begin() as conn:
        conn.execute(
            text("TRUNCATE command_records, handoffs, tubes, staff RESTART IDENTITY CASCADE")
        )
    seed()
    yield


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c

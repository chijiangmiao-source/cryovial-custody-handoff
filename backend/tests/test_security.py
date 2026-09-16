from __future__ import annotations

import importlib
import os
import uuid

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings


def key(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


def _complete_handoff(client, code: str | None = None) -> str:
    r = client.post(
        "/api/handoffs",
        json={
            "tube_code": "T-1001",
            "from_staff_code": "S001",
            "to_staff_code": "S002",
            "operation_key": key("create"),
        },
    )
    assert r.status_code == 201
    code = r.json()["handoff"]["code"]
    r = client.post(
        f"/api/handoffs/{code}/accept",
        json={"staff_code": "S002", "operation_key": key("accept")},
    )
    assert r.status_code == 200
    r = client.post(
        f"/api/handoffs/{code}/confirm",
        json={"staff_code": "S001", "operation_key": key("confirm")},
    )
    assert r.status_code == 200
    return code


def test_non_receiver_scan_after_completion_is_forbidden(client):
    code = _complete_handoff(client)
    # 非指定接收员（S003）在完成后再扫码：必须 403，不能假成功
    r = client.post(
        f"/api/handoffs/{code}/accept",
        json={"staff_code": "S003", "operation_key": key("wrong-rescan")},
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "not_receiver"


def test_non_receiver_scan_after_completion_does_not_change_custodian(client):
    code = _complete_handoff(client)
    for _ in range(2):
        r = client.post(
            f"/api/handoffs/{code}/accept",
            json={"staff_code": "S003", "operation_key": key("wrong-rescan")},
        )
        assert r.status_code == 403
    tube = client.get("/api/tubes/T-1001").json()["tube"]
    assert tube["custodian"]["code"] == "S002"
    assert tube["active_handoff"] is None


def test_designated_receiver_rescan_after_completion_is_idempotent(client):
    code = _complete_handoff(client)
    k = key("receiver-rescan")
    r1 = client.post(
        f"/api/handoffs/{code}/accept",
        json={"staff_code": "S002", "operation_key": k},
    )
    r2 = client.post(
        f"/api/handoffs/{code}/accept",
        json={"staff_code": "S002", "operation_key": k},
    )
    assert r1.status_code == r2.status_code == 200
    assert r1.json()["handoff"]["status"] == "completed"
    assert r2.headers.get("x-idempotent-replay") == "true"


def test_non_receiver_scan_while_pending_is_forbidden(client):
    r = client.post(
        "/api/handoffs",
        json={
            "tube_code": "T-1002",
            "from_staff_code": "S001",
            "to_staff_code": "S002",
            "operation_key": key("create"),
        },
    )
    code = r.json()["handoff"]["code"]
    r = client.post(
        f"/api/handoffs/{code}/accept",
        json={"staff_code": "S003", "operation_key": key("wrong")},
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "not_receiver"


def test_reset_endpoint_requires_token(client):
    r = client.post("/api/test/reset")
    assert r.status_code == 401
    r = client.post("/api/test/reset", headers={"X-Test-Token": "wrong"})
    assert r.status_code == 401
    r = client.post("/api/test/reset", headers={"X-Test-Token": "test-token"})
    assert r.status_code == 204


def test_reset_endpoint_absent_when_disabled():
    # 模拟生产默认配置：钩子关闭时接口等同不存在（404），任何访问者都无法清空数据
    from app import main as main_module

    old_enable = os.environ.get("SAMPLE_ENABLE_TEST_RESET")
    old_token = os.environ.get("SAMPLE_TEST_RESET_TOKEN")
    try:
        os.environ["SAMPLE_ENABLE_TEST_RESET"] = "false"
        os.environ["SAMPLE_TEST_RESET_TOKEN"] = ""
        get_settings.cache_clear()
        importlib.reload(main_module)
        with TestClient(main_module.app) as c:
            assert c.post("/api/test/reset").status_code == 404
            assert c.post("/api/test/reset", headers={"X-Test-Token": "anything"}).status_code == 404
    finally:
        if old_enable is None:
            os.environ.pop("SAMPLE_ENABLE_TEST_RESET", None)
        else:
            os.environ["SAMPLE_ENABLE_TEST_RESET"] = old_enable
        if old_token is None:
            os.environ.pop("SAMPLE_TEST_RESET_TOKEN", None)
        else:
            os.environ["SAMPLE_TEST_RESET_TOKEN"] = old_token
        get_settings.cache_clear()
        importlib.reload(main_module)


@pytest.fixture(autouse=True)
def _restore_app():
    # 每个测试后确保全局 app 回到 conftest 指定的配置
    yield
    from app import main as main_module

    get_settings.cache_clear()
    importlib.reload(main_module)

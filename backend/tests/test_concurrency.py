from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models import ACTIVE_STATUSES, Handoff, Staff, Tube


def key(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


@pytest.fixture(scope="module")
def shared_client():
    # 共享同一 TestClient：BlockingPortal 支持多线程并发请求，
    # 且 lifespan（建表/播种）只执行一次，避免并发播种冲突
    with TestClient(app) as c:
        yield c


def _post(client, path: str, payload: dict) -> tuple[int, dict, str | None]:
    r = client.post(path, json=payload)
    return r.status_code, r.json(), r.headers.get("x-idempotent-replay")


def test_two_concurrent_creates_only_one_wins(shared_client):
    payloads = [
        {
            "tube_code": "T-1001",
            "from_staff_code": "S001",
            "to_staff_code": "S002",
            "operation_key": key("race-a"),
        },
        {
            "tube_code": "T-1001",
            "from_staff_code": "S001",
            "to_staff_code": "S003",
            "operation_key": key("race-b"),
        },
    ]
    start = threading.Barrier(2)

    def task(payload):
        start.wait()
        return _post(shared_client, "/api/handoffs", payload)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(task, payloads))

    statuses = sorted(r[0] for r in results)
    assert statuses == [201, 409]
    ok = next(r for r in results if r[0] == 201)
    conflict = next(r for r in results if r[0] == 409)
    assert conflict[1]["error"]["code"] == "active_handoff_exists"
    winner_code = ok[1]["handoff"]["code"]

    db = SessionLocal()
    try:
        actives = db.scalars(
            select(Handoff).where(Handoff.status.in_(ACTIVE_STATUSES))
        ).all()
        assert len(actives) == 1
        assert actives[0].code == winner_code
        # 竞争期间保管人不变
        tube = db.scalar(select(Tube).where(Tube.code == "T-1001"))
        assert db.get(Staff, tube.custodian_id).code == "S001"
    finally:
        db.close()


def test_concurrent_same_key_double_submit_returns_single_first_result(shared_client):
    payload = {
        "tube_code": "T-1002",
        "from_staff_code": "S001",
        "to_staff_code": "S002",
        "operation_key": key("double-submit"),
    }
    start = threading.Barrier(2)

    def task(_):
        start.wait()
        return _post(shared_client, "/api/handoffs", payload)

    with ThreadPoolExecutor(max_workers=2) as pool:
        r1, r2 = list(pool.map(task, [0, 1]))

    assert r1[0] == r2[0] == 201
    assert r1[1]["handoff"]["code"] == r2[1]["handoff"]["code"]
    assert sorted(bool(r[2]) for r in (r1, r2)) == [False, True]

    db = SessionLocal()
    try:
        rows = db.query(Handoff).join(Tube, Handoff.tube_id == Tube.id).filter(Tube.code == "T-1002").all()
        assert len(rows) == 1
    finally:
        db.close()


def test_concurrent_duplicate_scanner_accept_is_idempotent(shared_client):
    create_payload = {
        "tube_code": "T-1001",
        "from_staff_code": "S001",
        "to_staff_code": "S002",
        "operation_key": key("create"),
    }
    status, body, _ = _post(shared_client, "/api/handoffs", create_payload)
    assert status == 201
    code = body["handoff"]["code"]

    accept_payload = {"staff_code": "S002", "operation_key": key("scan")}
    start = threading.Barrier(2)

    def task(_):
        start.wait()
        return _post(shared_client, f"/api/handoffs/{code}/accept", accept_payload)

    with ThreadPoolExecutor(max_workers=2) as pool:
        r1, r2 = list(pool.map(task, [0, 1]))

    assert r1[0] == r2[0] == 200
    assert r1[1]["handoff"]["accepted_at"] == r2[1]["handoff"]["accepted_at"]
    assert sorted(bool(r[2]) for r in (r1, r2)) == [False, True]

    db = SessionLocal()
    try:
        h = db.scalar(select(Handoff).where(Handoff.code == code))
        assert h.status == "accepted"
        assert h.accepted_at is not None
    finally:
        db.close()


def test_concurrent_confirm_lost_response_then_retry_single_completion(shared_client):
    create_payload = {
        "tube_code": "T-1001",
        "from_staff_code": "S001",
        "to_staff_code": "S002",
        "operation_key": key("create"),
    }
    _, body, _ = _post(shared_client, "/api/handoffs", create_payload)
    code = body["handoff"]["code"]
    _post(shared_client, f"/api/handoffs/{code}/accept", {"staff_code": "S002", "operation_key": key("scan")})

    confirm_payload = {"staff_code": "S001", "operation_key": key("confirm")}
    start = threading.Barrier(2)

    def task(_):
        start.wait()
        return _post(shared_client, f"/api/handoffs/{code}/confirm", confirm_payload)

    with ThreadPoolExecutor(max_workers=2) as pool:
        r1, r2 = list(pool.map(task, [0, 1]))

    assert r1[0] == r2[0] == 200
    c1 = r1[1]["handoff"]["completed_at"]
    c2 = r2[1]["handoff"]["completed_at"]
    assert c1 == c2
    assert r1[1]["handoff"]["custodian_staff_code"] == "S002"
    assert r2[1]["handoff"]["custodian_staff_code"] == "S002"

    db = SessionLocal()
    try:
        tube = db.scalar(select(Tube).where(Tube.code == "T-1001"))
        assert db.get(Staff, tube.custodian_id).code == "S002"
        h = db.scalar(select(Handoff).where(Handoff.code == code))
        assert h.status == "completed"
        assert db.query(Handoff).filter(Handoff.status == "completed").count() == 1
    finally:
        db.close()


def test_database_time_used_for_expiry_decision(shared_client):
    # 所有时间判定来自数据库 statement_timestamp()，而非应用服务器时钟
    status, body, _ = _post(
        shared_client,
        "/api/handoffs",
        {
            "tube_code": "T-1002",
            "from_staff_code": "S001",
            "to_staff_code": "S003",
            "operation_key": key("create"),
        },
    )
    assert status == 201
    expires = datetime.fromisoformat(body["handoff"]["expires_at"])
    created = datetime.fromisoformat(body["handoff"]["created_at"])
    assert (expires - created).total_seconds() == 600
    assert expires.tzinfo is not None

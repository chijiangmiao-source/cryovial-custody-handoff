from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select, text

import app.api as api_module
import app.service as service_module
from app.models import ACTIVE_STATUSES, Handoff, Staff, Tube

FROZEN = datetime(2027, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
FROZEN_SQL = "2027-01-01 12:00:00+00"


@pytest.fixture
def frozen_clock(monkeypatch):
    # 冻结服务层与 API 层引用的数据库时钟，实现到期边界的确定性测试
    monkeypatch.setattr(service_module, "db_now", lambda _db: FROZEN)
    monkeypatch.setattr(api_module, "db_now", lambda _db: FROZEN)
    return FROZEN


def key(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


def create(client, tube="T-1001", frm="S001", to="S002", op_key=None):
    op_key = op_key or key("create")
    r = client.post(
        "/api/handoffs",
        json={
            "tube_code": tube,
            "from_staff_code": frm,
            "to_staff_code": to,
            "operation_key": op_key,
        },
    )
    return r, op_key


def _set_expires(db, code: str, sql_literal: str) -> None:
    db.execute(
        text(f"UPDATE handoffs SET expires_at = TIMESTAMPTZ '{sql_literal}' WHERE code = :c"),
        {"c": code},
    )
    db.commit()


def test_boundary_predicate_uses_strict_inequality_in_database(db):
    # 同一语句内 statement_timestamp 固定：等号不成立 => 截止时刻仍有效
    equal = db.execute(text("SELECT statement_timestamp() > statement_timestamp()")).scalar()
    past = db.execute(
        text("SELECT statement_timestamp() > statement_timestamp() - interval '1 microsecond'")
    ).scalar()
    assert equal is False
    assert past is True


def test_at_exactly_deadline_still_valid_on_read(client, db, frozen_clock):
    r, _ = create(client)
    code = r.json()["handoff"]["code"]
    _set_expires(db, code, FROZEN_SQL)  # expires_at == now

    r = client.get(f"/api/handoffs/{code}")
    body = r.json()["handoff"]
    assert r.status_code == 200
    assert body["status"] == "pending"
    assert body["expired"] is False
    assert body["seconds_remaining"] == 0
    assert body["custodian_staff_code"] == "S001"
    # 数据库中未被封闭
    db.expire_all()
    assert db.scalar(select(Handoff.status).where(Handoff.code == code)) == "pending"


def test_one_microsecond_past_deadline_closes_on_read_without_custodian_change(client, db, frozen_clock):
    r, _ = create(client)
    code = r.json()["handoff"]["code"]
    _set_expires(db, code, "2027-01-01 11:59:59.999999+00")  # now > expires_at

    r = client.get(f"/api/handoffs/{code}")
    body = r.json()["handoff"]
    assert body["status"] == "expired"
    assert body["expired"] is True
    assert body["custodian_staff_code"] == "S001"

    db.expire_all()
    assert db.scalar(select(Handoff.status).where(Handoff.code == code)) == "expired"
    tube = db.scalar(select(Tube).where(Tube.code == "T-1001"))
    assert db.get(Staff, tube.custodian_id).code == "S001"


def test_at_exactly_deadline_accept_and_confirm_succeed(client, db, frozen_clock):
    r, _ = create(client)
    code = r.json()["handoff"]["code"]
    _set_expires(db, code, FROZEN_SQL)  # accept 瞬间 now == expires_at

    r = client.post(
        f"/api/handoffs/{code}/accept",
        json={"staff_code": "S002", "operation_key": key("a")},
    )
    assert r.status_code == 200
    assert r.json()["handoff"]["status"] == "accepted"

    # confirm 瞬间同样钉在截止时刻，等号边界仍可完成并原子换保管人
    _set_expires(db, code, FROZEN_SQL)
    r = client.post(
        f"/api/handoffs/{code}/confirm",
        json={"staff_code": "S001", "operation_key": key("c")},
    )
    assert r.status_code == 200
    body = r.json()["handoff"]
    assert body["status"] == "completed"
    assert body["custodian_staff_code"] == "S002"


def test_expired_blocks_accept_and_confirm_then_allows_new_handoff(client, db, frozen_clock):
    r, _ = create(client)
    code = r.json()["handoff"]["code"]
    _set_expires(db, code, "2027-01-01 11:59:59+00")

    r = client.post(
        f"/api/handoffs/{code}/accept",
        json={"staff_code": "S002", "operation_key": key("a")},
    )
    assert r.status_code == 410
    assert r.json()["error"]["code"] == "handoff_expired"

    # 即使此前已被 accept 路径封闭，confirm 也必须再次拦截，不得把过期交接走完
    db.expire_all()
    assert db.scalar(select(Handoff.status).where(Handoff.code == code)) == "expired"
    r = client.post(
        f"/api/handoffs/{code}/confirm",
        json={"staff_code": "S001", "operation_key": key("c")},
    )
    assert r.status_code == 410
    assert r.json()["error"]["code"] == "handoff_expired"

    # 封闭后可重新发起；保管人仍是原转出员
    r2, _ = create(client)
    assert r2.status_code == 201
    assert r2.json()["handoff"]["code"] != code
    assert r2.json()["handoff"]["custodian_staff_code"] == "S001"


def test_expired_failure_is_replayed_with_same_status(client, db, frozen_clock):
    r, _ = create(client)
    code = r.json()["handoff"]["code"]
    _set_expires(db, code, "2027-01-01 11:59:59+00")
    payload = {"staff_code": "S002", "operation_key": key("a")}

    r1 = client.post(f"/api/handoffs/{code}/accept", json=payload)
    r2 = client.post(f"/api/handoffs/{code}/accept", json=payload)
    assert r1.status_code == r2.status_code == 410
    assert r2.headers.get("x-idempotent-replay") == "true"
    assert r1.json() == r2.json()


def test_expiry_does_not_touch_completed_handoff(client, db, frozen_clock):
    r, _ = create(client)
    code = r.json()["handoff"]["code"]
    client.post(
        f"/api/handoffs/{code}/accept",
        json={"staff_code": "S002", "operation_key": key("a")},
    )
    client.post(
        f"/api/handoffs/{code}/confirm",
        json={"staff_code": "S001", "operation_key": key("c")},
    )
    _set_expires(db, code, "2026-01-01 00:00:00+00")  # 远早于冻结时钟

    r = client.get(f"/api/handoffs/{code}")
    body = r.json()["handoff"]
    assert body["status"] == "completed"
    assert body["expired"] is False
    assert body["custodian_staff_code"] == "S002"


def test_expiry_while_accepted_does_not_change_custodian(client, db, frozen_clock):
    r, _ = create(client)
    code = r.json()["handoff"]["code"]
    ak = key("a")
    r = client.post(
        f"/api/handoffs/{code}/accept",
        json={"staff_code": "S002", "operation_key": ak},
    )
    assert r.status_code == 200
    _set_expires(db, code, "2027-01-01 11:59:59+00")

    # 接收员的重发在到期后返回首次成功结果（重放不重新判定）
    r = client.post(
        f"/api/handoffs/{code}/accept",
        json={"staff_code": "S002", "operation_key": ak},
    )
    assert r.status_code == 200
    assert r.headers.get("x-idempotent-replay") == "true"
    assert r.json()["handoff"]["status"] == "accepted"

    # 但确认时按数据库时间判定为已到期，保管人不变
    r = client.post(
        f"/api/handoffs/{code}/confirm",
        json={"staff_code": "S001", "operation_key": key("c")},
    )
    assert r.status_code == 410

    db.expire_all()
    assert db.scalar(select(Handoff.status).where(Handoff.code == code)) == "expired"
    tube = db.scalar(select(Tube).where(Tube.code == "T-1001"))
    assert db.get(Staff, tube.custodian_id).code == "S001"


def test_no_active_handoff_rows_after_expiry(client, db, frozen_clock):
    r, _ = create(client)
    code = r.json()["handoff"]["code"]
    _set_expires(db, code, "2027-01-01 11:59:59+00")
    client.get(f"/api/handoffs/{code}")

    active = db.scalars(select(Handoff).where(Handoff.status.in_(ACTIVE_STATUSES))).all()
    assert active == []

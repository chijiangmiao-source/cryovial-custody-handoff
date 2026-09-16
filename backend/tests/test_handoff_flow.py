from __future__ import annotations

import uuid

from sqlalchemy import select

from app.models import Handoff, Staff, Tube


def key(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


def make_tube(db, code: str, custodian: str = "S001") -> None:
    staff_id = db.scalar(select(Staff.id).where(Staff.code == custodian))
    db.add(Tube(code=code, custodian_id=staff_id))
    db.commit()


def create_handoff(client, tube="T-1001", frm="S001", to="S002", op_key=None):
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


def test_full_happy_path(client):
    r, ck = create_handoff(client)
    assert r.status_code == 201
    code = r.json()["handoff"]["code"]
    assert r.json()["handoff"]["status"] == "pending"
    assert r.json()["handoff"]["custodian_staff_code"] == "S001"

    # 接收员扫码接受
    ak = key("accept")
    r = client.post(f"/api/handoffs/{code}/accept", json={"staff_code": "S002", "operation_key": ak})
    assert r.status_code == 200
    assert r.json()["handoff"]["status"] == "accepted"
    assert r.json()["handoff"]["accepted_at"]

    # 扫码器重发：同键重放首次结果
    r2 = client.post(f"/api/handoffs/{code}/accept", json={"staff_code": "S002", "operation_key": ak})
    assert r2.status_code == 200
    assert r2.headers.get("x-idempotent-replay") == "true"
    assert r2.json()["handoff"]["accepted_at"] == r.json()["handoff"]["accepted_at"]

    # 非转出员不能确认
    r = client.post(
        f"/api/handoffs/{code}/confirm", json={"staff_code": "S002", "operation_key": key("cf")}
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "not_owner"

    # 转出员最终确认
    k = key("confirm")
    r = client.post(f"/api/handoffs/{code}/confirm", json={"staff_code": "S001", "operation_key": k})
    assert r.status_code == 200
    body = r.json()["handoff"]
    assert body["status"] == "completed"
    assert body["completed_at"]
    assert body["custodian_staff_code"] == "S002"

    # 丢响应后重试：重放，且保管人唯一
    r = client.post(f"/api/handoffs/{code}/confirm", json={"staff_code": "S001", "operation_key": k})
    assert r.status_code == 200
    assert r.headers.get("x-idempotent-replay") == "true"

    r = client.get("/api/tubes/T-1001")
    assert r.json()["tube"]["custodian"]["code"] == "S002"
    assert r.json()["tube"]["active_handoff"] is None


def test_create_replay_same_key_returns_first_result(client):
    r1, k = create_handoff(client)
    code = r1.json()["handoff"]["code"]
    r2, _ = create_handoff(client, op_key=k)
    assert r2.status_code == 201
    assert r2.headers.get("x-idempotent-replay") == "true"
    assert r2.json()["handoff"]["code"] == code


def test_same_key_different_params_conflict(client):
    r1, k = create_handoff(client, tube="T-1001")
    assert r1.status_code == 201
    r2, _ = create_handoff(client, tube="T-1002", op_key=k)
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "idempotency_conflict"
    assert "/operation_key" in r2.json()["error"]["fields"]
    # T-1002 不受影响，仍可被正常发起
    r3, _ = create_handoff(client, tube="T-1002")
    assert r3.status_code == 201


def test_only_one_active_handoff_per_tube(client):
    r1, _ = create_handoff(client, to="S002")
    assert r1.status_code == 201
    r2, _ = create_handoff(client, to="S003")
    assert r2.status_code == 409
    body = r2.json()
    assert body["error"]["code"] == "active_handoff_exists"
    assert set(body["error"]["fields"]) == {"/tube_code"}


def test_non_custodian_cannot_create(client, db):
    make_tube(db, "T-9000", custodian="S002")
    r, _ = create_handoff(client, tube="T-9000", frm="S001", to="S003")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "not_custodian"


def test_same_party_rejected(client):
    r, _ = create_handoff(client, frm="S001", to="S001")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "same_party"
    assert "/to_staff_code" in r.json()["error"]["fields"]


def test_wrong_receiver_and_confirm_before_accept(client):
    r, _ = create_handoff(client)
    code = r.json()["handoff"]["code"]

    r = client.post(f"/api/handoffs/{code}/accept", json={"staff_code": "S003", "operation_key": key("a")})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "not_receiver"

    r = client.post(f"/api/handoffs/{code}/confirm", json={"staff_code": "S001", "operation_key": key("c")})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "not_accepted"


def test_validation_errors_use_json_pointer(client):
    r = client.post(
        "/api/handoffs",
        json={"tube_code": "!", "to_staff_code": "S002", "operation_key": "x"},
    )
    assert r.status_code == 422
    fields = r.json()["error"]["fields"]
    assert set(fields) == {"/tube_code", "/from_staff_code", "/operation_key"}


def test_failed_command_is_persisted_and_replayed(client):
    # 错误接收员扫码失败也要记录首次结果，重放返回同一 403
    r, _ = create_handoff(client)
    code = r.json()["handoff"]["code"]
    k = key("bad-accept")
    payload = {"staff_code": "S003", "operation_key": k}
    r1 = client.post(f"/api/handoffs/{code}/accept", json=payload)
    r2 = client.post(f"/api/handoffs/{code}/accept", json=payload)
    assert r1.status_code == r2.status_code == 403
    assert r2.headers.get("x-idempotent-replay") == "true"
    assert r1.json() == r2.json()


def test_stale_completed_handoff_does_not_move_custodian(client, db):
    r, _ = create_handoff(client)
    code = r.json()["handoff"]["code"]
    h = db.scalar(select(Handoff).where(Handoff.code == code))

    client.post(f"/api/handoffs/{code}/accept", json={"staff_code": "S002", "operation_key": key("a")})
    # 模拟库外纠正：保管人已变为 S003，但旧交接仍停留在 accepted
    s003 = db.scalar(select(Staff).where(Staff.code == "S003"))
    tube = db.scalar(select(Tube).where(Tube.code == "T-1001"))
    tube.custodian_id = s003.id
    db.commit()

    r = client.post(f"/api/handoffs/{code}/confirm", json={"staff_code": "S001", "operation_key": key("c")})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "custodian_changed"

    db.expire_all()
    tube = db.scalar(select(Tube).where(Tube.code == "T-1001"))
    custodian = db.get(Staff, tube.custodian_id)
    assert custodian.code == "S003"
    assert db.get(Handoff, h.id).status == "accepted"

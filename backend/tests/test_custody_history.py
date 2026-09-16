from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import pytest
from sqlalchemy import func, select, text

from app.database import SessionLocal
from app.ledger import backfill_baselines
from app.main import seed
from app.models import CustodyEvent, Handoff, Staff, Tube


def key(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


def complete_handoff(client, *, tube="T-1001", frm="S001", to="S002"):
    r = client.post(
        "/api/handoffs",
        json={
            "tube_code": tube,
            "from_staff_code": frm,
            "to_staff_code": to,
            "operation_key": key("create"),
        },
    )
    assert r.status_code == 201, r.text
    code = r.json()["handoff"]["code"]
    r = client.post(f"/api/handoffs/{code}/accept", json={"staff_code": to, "operation_key": key("a")})
    assert r.status_code == 200, r.text
    r = client.post(f"/api/handoffs/{code}/confirm", json={"staff_code": frm, "operation_key": key("c")})
    assert r.status_code == 200, r.text
    return code, datetime.fromisoformat(r.json()["handoff"]["completed_at"])


def history(client, tube, at=None):
    url = f"/api/tubes/{tube}/history"
    if at is not None:
        url += f"?at={quote(at, safe='')}"
    return client.get(url)


def seq_rows(db, tube_code="T-1001"):
    return db.execute(
        text(
            "SELECT ce.seq, ce.kind, sc.code AS custodian, ce.handoff_code "
            "FROM custody_events ce JOIN tubes t ON t.id = ce.tube_id "
            "JOIN staff sc ON sc.id = ce.custodian_id "
            "WHERE t.code = :c ORDER BY ce.seq"
        ),
        {"c": tube_code},
    ).all()


# ---------------------------------------------------------------------------
# 上线基线
# ---------------------------------------------------------------------------

def test_seed_tubes_have_baseline_only(client, db):
    rows = seq_rows(db)
    assert [tuple(r)[:2] for r in rows] == [(0, "baseline")]
    assert rows[0][2] == "S001"  # 基线保管人即上线时现状
    assert rows[0][3] is None     # 基线不对应任何交接码


def test_legacy_data_upgrade_gets_baseline_representing_upgrade_moment(db, client):
    # 模拟旧版本遗留：直接删掉账本事件，冻存管与交接仍在（升级前没有任何保管历史）
    db.execute(text("TRUNCATE custody_events"))
    db.commit()
    seed()  # 启动流程：seed_data_if_needed + backfill_baselines

    with SessionLocal() as s:
        upgrade_at = s.scalar(select(func.statement_timestamp()))

    r = history(client, "T-1001")
    assert r.status_code == 200
    body = r.json()["history"]
    since = datetime.fromisoformat(body["traceable_since"])
    assert body["evidence_available"] is True
    assert body["custodian"]["code"] == "S001"
    assert body["event_seq"] == 0
    assert body["handoff_code"] is None

    # 早于可追溯起点：直接说明无历史证据，不猜测任何保管人
    before = (since - timedelta(seconds=1)).isoformat()
    r = history(client, "T-1001", before)
    assert r.status_code == 200
    body = r.json()["history"]
    assert body["evidence_available"] is False
    assert body["custodian"] is None
    assert body["event_seq"] is None
    assert body["point_in_time"] == before


# ---------------------------------------------------------------------------
# 连续两次合法交接：序号、时间边界、交接码、前后变更项
# ---------------------------------------------------------------------------

def test_two_legal_transfers_boundary_projection(client, db):
    code1, t1 = complete_handoff(client, frm="S001", to="S002")
    code2, t2 = complete_handoff(client, frm="S002", to="S003")
    assert t2 > t1

    rows = seq_rows(db)
    assert [(r[0], r[1], r[2], r[3]) for r in rows] == [
        (0, "baseline", "S001", None),
        (1, "transfer", "S002", code1),
        (2, "transfer", "S003", code2),
    ]

    # 严格早于首个生效时刻：仍是基线保管人
    r = history(client, "T-1001", (t1 - timedelta(microseconds=1)).isoformat())
    h = r.json()["history"]
    assert h["evidence_available"] is True
    assert h["custodian"]["code"] == "S001"
    assert h["event_seq"] == 0
    assert h["previous_change"] is None
    assert h["next_change"]["seq"] == 1 and h["next_change"]["handoff_code"] == code1

    # 边界等于生效时刻：事件已生效（截止时刻语义）
    r = history(client, "T-1001", t1.isoformat())
    h = r.json()["history"]
    assert h["custodian"]["code"] == "S002"
    assert h["event_seq"] == 1
    assert h["handoff_code"] == code1
    assert h["previous_change"]["kind"] == "baseline"
    assert h["next_change"]["seq"] == 2 and h["next_change"]["handoff_code"] == code2

    # 两次变更之间
    mid = t1 + (t2 - t1) / 2
    r = history(client, "T-1001", mid.isoformat())
    h = r.json()["history"]
    assert h["custodian"]["code"] == "S002"
    assert h["event_seq"] == 1

    # 恰好第二次生效时刻
    r = history(client, "T-1001", t2.isoformat())
    h = r.json()["history"]
    assert h["custodian"]["code"] == "S003"
    assert h["event_seq"] == 2
    assert h["handoff_code"] == code2
    assert h["previous_change"]["custodian"]["code"] == "S002"
    assert h["next_change"] is None  # 账本尾部之后尚无变更

    # 时间轴完整且只有指定时刻的事件被标记
    assert [e["seq"] for e in h["timeline"]] == [0, 1, 2]
    active = [e for e in h["timeline"] if e["active_at_point"]]
    assert [e["seq"] for e in active] == [2]
    assert h["timeline"][1]["from_staff"]["code"] == "S001"
    assert h["timeline"][1]["to_staff"]["code"] == "S002"

    # 未来时刻：以最后已知事件投影，不报错
    r = history(client, "T-1001", (t2 + timedelta(days=1)).isoformat())
    assert r.json()["history"]["custodian"]["code"] == "S003"

    # 缺省时刻（数据库当前）投影尾部
    r = history(client, "T-1001")
    assert r.json()["history"]["custodian"]["code"] == "S003"


def test_ledger_tail_always_matches_current_custodian(client, db):
    complete_handoff(client, frm="S001", to="S002")
    tube = db.scalar(select(Tube).where(Tube.code == "T-1001"))
    tail = db.scalars(
        select(CustodyEvent).where(CustodyEvent.tube_id == tube.id).order_by(CustodyEvent.seq.desc())
    ).first()
    custodian = db.get(Staff, tail.custodian_id)
    assert custodian.code == "S002"
    r = client.get("/api/tubes/T-1001")
    assert r.json()["tube"]["custodian"]["code"] == "S002"


# ---------------------------------------------------------------------------
# 失败交接不留事件；响应丢失重放不重复追加
# ---------------------------------------------------------------------------

def test_failed_confirm_leaves_no_event(client, db):
    # 接收员接受后，库外把保管人改成 S003：旧交接确认必须失败（custodian_changed）
    r = client.post(
        "/api/handoffs",
        json={"tube_code": "T-1001", "from_staff_code": "S001", "to_staff_code": "S002",
              "operation_key": key("create")},
    )
    code = r.json()["handoff"]["code"]
    client.post(f"/api/handoffs/{code}/accept", json={"staff_code": "S002", "operation_key": key("a")})

    s003 = db.scalar(select(Staff).where(Staff.code == "S003"))
    tube = db.scalar(select(Tube).where(Tube.code == "T-1001"))
    tube.custodian_id = s003.id
    db.commit()

    r = client.post(f"/api/handoffs/{code}/confirm", json={"staff_code": "S001", "operation_key": key("c")})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "custodian_changed"

    db.expire_all()
    assert len(seq_rows(db)) == 1  # 仅基线，失败不留任何事件
    h = db.scalar(select(Handoff).where(Handoff.code == code))
    assert h.status == "accepted"


def test_expired_handoff_leaves_no_event(client, db):
    r = client.post(
        "/api/handoffs",
        json={"tube_code": "T-1001", "from_staff_code": "S001", "to_staff_code": "S002",
              "operation_key": key("create")},
    )
    code = r.json()["handoff"]["code"]
    db.execute(
        text("UPDATE handoffs SET expires_at = statement_timestamp() - interval '1 second' WHERE code = :c"),
        {"c": code},
    )
    db.commit()
    r = client.post(f"/api/handoffs/{code}/accept", json={"staff_code": "S002", "operation_key": key("a")})
    assert r.status_code == 410
    assert len(seq_rows(db)) == 1


def test_confirm_response_lost_replay_appends_event_once(client, db):
    r = client.post(
        "/api/handoffs",
        json={"tube_code": "T-1001", "from_staff_code": "S001", "to_staff_code": "S002",
              "operation_key": key("create")},
    )
    code = r.json()["handoff"]["code"]
    client.post(f"/api/handoffs/{code}/accept", json={"staff_code": "S002", "operation_key": key("a")})

    confirm_key = key("confirm")
    payload = {"staff_code": "S001", "operation_key": confirm_key}
    r1 = client.post(f"/api/handoffs/{code}/confirm", json=payload)
    r2 = client.post(f"/api/handoffs/{code}/confirm", json=payload)  # 响应丢失后同键重放
    assert r1.status_code == r2.status_code == 200
    assert r1.headers.get("x-idempotent-replay") is None
    assert r2.headers.get("x-idempotent-replay") == "true"
    assert r1.json() == r2.json()

    rows = seq_rows(db)
    assert len(rows) == 2  # 基线 + 恰好一条转移
    assert rows[1][1] == "transfer" and rows[1][3] == code


def test_broken_tail_blocks_append_and_keeps_handoff_open(client, db):
    # 管当前保管人仍是转出员，但账本尾部被库外污染成别人：提交必须整体失败
    r = client.post(
        "/api/handoffs",
        json={"tube_code": "T-1001", "from_staff_code": "S001", "to_staff_code": "S002",
              "operation_key": key("create")},
    )
    code = r.json()["handoff"]["code"]
    client.post(f"/api/handoffs/{code}/accept", json={"staff_code": "S002", "operation_key": key("a")})

    s003 = db.scalar(select(Staff).where(Staff.code == "S003"))
    tube = db.scalar(select(Tube).where(Tube.code == "T-1001"))
    db.execute(
        text("UPDATE custody_events SET custodian_id = :s WHERE tube_id = :t AND seq = 0"),
        {"s": s003.id, "t": tube.id},
    )
    db.commit()

    r = client.post(f"/api/handoffs/{code}/confirm", json={"staff_code": "S001", "operation_key": key("c")})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "custody_chain_broken"
    db.expire_all()
    assert len(seq_rows(db)) == 1  # 没有追加新事件
    assert db.scalar(select(Handoff).where(Handoff.code == code)).status == "accepted"
    assert db.get(Staff, db.get(Tube, tube.id).custodian_id).code == "S001"


# ---------------------------------------------------------------------------
# 只读历史接口：断链/缺基线拒绝；当前查询结果保留
# ---------------------------------------------------------------------------

def test_history_without_baseline_returns_identifiable_error(client, db):
    tube = db.scalar(select(Tube).where(Tube.code == "T-1001"))
    db.execute(text("DELETE FROM custody_events WHERE tube_id = :t"), {"t": tube.id})
    db.commit()

    r = history(client, "T-1001")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "custody_baseline_missing"

    # 页面保留当前查询结果：管码查询不受账本缺失影响
    r = client.get("/api/tubes/T-1001")
    assert r.status_code == 200
    assert r.json()["tube"]["custodian"]["code"] == "S001"


def test_history_with_gap_refuses_misleading_result(client, db):
    # 先合法完成一次交接产生 seq 0/1，再删掉中间事件制造断链
    code1, _ = complete_handoff(client, frm="S001", to="S002")
    tube = db.scalar(select(Tube).where(Tube.code == "T-1001"))
    db.execute(
        text("DELETE FROM custody_events WHERE tube_id = :t AND seq = 1"), {"t": tube.id}
    )
    db.commit()

    r = history(client, "T-1001")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "custody_chain_broken"

    # 当前归属查询仍可用
    r = client.get("/api/tubes/T-1001")
    assert r.status_code == 200
    assert r.json()["tube"]["custodian"]["code"] == "S002"


def test_history_dangling_transfer_refuses(client, db):
    # 首条不是基线 / 转移的转出人与上一保管人不符：断链
    code1, _ = complete_handoff(client, frm="S001", to="S002")
    tube = db.scalar(select(Tube).where(Tube.code == "T-1001"))
    s003 = db.scalar(select(Staff).where(Staff.code == "S003"))
    db.execute(
        text("UPDATE custody_events SET from_staff_id = :s WHERE tube_id = :t AND seq = 1"),
        {"s": s003.id, "t": tube.id},
    )
    db.commit()
    r = history(client, "T-1001")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "custody_chain_broken"


def test_history_read_only_and_input_validation(client):
    # 只读：查询历史不封闭任何交接
    r = client.post(
        "/api/handoffs",
        json={"tube_code": "T-1002", "from_staff_code": "S001", "to_staff_code": "S002",
              "operation_key": key("create")},
    )
    code = r.json()["handoff"]["code"]
    with SessionLocal() as s:
        s.execute(
            text("UPDATE handoffs SET expires_at = statement_timestamp() - interval '1 second' WHERE code = :c"),
            {"c": code},
        )
        s.commit()
    r = history(client, "T-1002")
    assert r.status_code == 200
    with SessionLocal() as s:
        assert s.scalar(select(Handoff.status).where(Handoff.code == code)) == "pending"

    # 非法时刻 / 缺时区 / 未知管码
    r = history(client, "T-1002", "not-a-time")
    assert r.status_code == 422
    assert r.json()["error"]["fields"]["/at"]
    r = history(client, "T-1002", "2026-09-16T22:30:00")
    assert r.status_code == 422
    assert "/at" in r.json()["error"]["fields"]
    r = history(client, "NO-SUCH")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "tube_not_found"


def test_db_enforces_monotonic_seq_per_tube(client, db):
    # 数据库层面兜底：同管重复 seq 无法写入
    tube = db.scalar(select(Tube).where(Tube.code == "T-1001"))
    custodian = tube.custodian_id
    dup = CustodyEvent(
        tube_id=tube.id, seq=0, kind="baseline", custodian_id=custodian,
        effective_at=datetime.now(timezone.utc),
    )
    db.add(dup)
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


def test_backfill_is_idempotent(db):
    n1 = backfill_baselines(db)
    db.commit()
    n2 = backfill_baselines(db)
    db.commit()
    assert (n1, n2) == (0, 0)

#!/usr/bin/env python3
"""
一次性验收脚本：对完整运行栈执行夜班交接故障演练。

覆盖验收点：
  1. 两次创建竞争：同一冻存管最多一个活动交接，失败方不夺走样本；
  2. 扫码器重发：重复扫码重放首次结果（同一操作键并发双发）；
  3. 确认丢响应：同键并发双发只完成一次，completed_at 相同；
  4. 同键异参冲突：409；
  5. 旧交接不能覆盖已完成归属；
  6. 到期只封闭未完成交接，不改变保管人；封闭后可重新发起并完成；
  7. 直连数据库断言：每管活动交接数 ≤ 1，保管人唯一且与页面 API 一致；
  8. 旧数据升级补齐上线基线；连续两次合法交接的时间边界（保管人/序号/交接码/前后变更项）；
  9. 失败交接不留保管事件；确认响应丢失重放不重复追加；注入断链/缺基线时历史接口拒绝，
     当前管码查询结果仍可用；早于可追溯起点明确返回无历史证据。

通过则退出码 0，任一断言失败退出码 1。
"""
from __future__ import annotations

import os
import sys
import threading
import urllib.parse
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import httpx
import psycopg

BACKEND = os.environ.get("BACKEND_URL", "http://backend:8000").rstrip("/")
WEB = os.environ.get("WEB_URL", "http://web").rstrip("/")
# 公开入口（nginx → 安全后端）；用于断言页面访客无法触达验收/重置接口
PUBLIC_API = os.environ.get("PUBLIC_API_URL", WEB).rstrip("/")
DATABASE_URL = os.environ.get("VERIFY_DATABASE_URL", "")
# 验收钩子令牌：后端开启 SAMPLE_ENABLE_TEST_RESET 时必须提供，调用 /api/test/* 需带头
TEST_RESET_TOKEN = os.environ.get("TEST_RESET_TOKEN", "")

PASS = "\033[32m✓\033[0m"
FAIL = "\033[31m✗\033[0m"
INFO = "\033[36m•\033[0m"

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    mark = PASS if condition else FAIL
    print(f"  {mark} {name}" + (f" —— {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(name)


def key(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def post(client: httpx.Client, path: str, payload: dict) -> httpx.Response:
    return client.post(f"{BACKEND}{path}", json=payload, timeout=30)


def get(client: httpx.Client, path: str) -> httpx.Response:
    return client.get(f"{BACKEND}{path}", timeout=30)


def create(client: httpx.Client, *, tube="T-1001", frm="S001", to="S002", op_key=None):
    op_key = op_key or key("create")
    r = post(
        client,
        "/api/handoffs",
        {
            "tube_code": tube,
            "from_staff_code": frm,
            "to_staff_code": to,
            "operation_key": op_key,
        },
    )
    return r, op_key


def wait_backend(client: httpx.Client) -> None:
    for _ in range(60):
        try:
            if get(client, "/api/health").status_code == 200:
                return
        except httpx.TransportError:
            pass
        import time

        time.sleep(1)
    raise RuntimeError("后端服务未在预期时间内就绪")


def reset(client: httpx.Client) -> None:
    headers = {"X-Test-Token": TEST_RESET_TOKEN} if TEST_RESET_TOKEN else {}
    r = client.post(f"{BACKEND}/api/test/reset", headers=headers, timeout=30)
    if r.status_code != 204:
        detail = r.text.strip()[:300]
        if r.status_code == 401:
            hint = "令牌被拒绝：请确认 verify 的 TEST_RESET_TOKEN 与 backend-verify 的 SAMPLE_TEST_RESET_TOKEN 取自同一来源（compose 锚点），且没有复用旧令牌启动的 backend-verify 容器（先 docker compose down 再 run）。"
        elif r.status_code == 404:
            hint = "验收钩子未挂载：BACKEND_URL 必须指向开启了 SAMPLE_ENABLE_TEST_RESET=true 的 backend-verify，而非公开 backend。"
        else:
            hint = "请确认后端以 SAMPLE_ENABLE_TEST_RESET=true 启动且令牌一致。"
        raise RuntimeError(f"重置失败（HTTP {r.status_code}）。{hint} 响应：{detail}")


def main() -> int:
    print(f"{INFO} 后端：{BACKEND}")
    print(f"{INFO} 前端：{WEB}")

    with httpx.Client() as client:
        wait_backend(client)
        reset(client)

        # 0. 前端静态页面由 nginx 提供
        r = client.get(WEB, timeout=10)
        check("前端页面可访问且返回 HTML", r.status_code == 200 and "<div id=\"root\"" in r.text)

        # 0.1 公开入口（nginx → 安全后端）必须可用：健康检查 200，验收钩子未挂载故 404。
        # 若后端在并发启动的 DDL 竞争中崩溃，这里会表现为 502 而非 404，及早失败并给出定位。
        public_health = client.get(f"{PUBLIC_API}/api/health", timeout=30)
        check("公开后端入口可用（/api/health 200）",
              public_health.status_code == 200,
              f"HTTP {public_health.status_code}；若为 502，通常是公开后端启动失败，请查看其日志")
        public_reset_early = client.post(f"{PUBLIC_API}/api/test/reset", timeout=30)
        check("公开后端未挂载验收接口（重置为 404，而非 502）",
              public_reset_early.status_code == 404, f"HTTP {public_reset_early.status_code}")

        # ---------------------------------------------------------------
        print(f"{INFO} 场景 1：两次创建竞争")
        reset(client)
        barrier = threading.Barrier(2)

        def race_create(to_code: str, k: str):
            barrier.wait()
            with httpx.Client() as c:
                return create(c, to=to_code, op_key=k)[0]

        with ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(race_create, "S002", key("race-a"))
            f2 = pool.submit(race_create, "S003", key("race-b"))
            ra, rb = f1.result(), f2.result()

        statuses = sorted((ra.status_code, rb.status_code))
        check("竞争结果为一个 201、一个 409", statuses == [201, 409], f"实际 {statuses}")
        loser = ra if ra.status_code == 409 else rb
        check("失败方错误码为 active_handoff_exists", loser.json()["error"]["code"] == "active_handoff_exists")
        tube = get(client, "/api/tubes/T-1001").json()["tube"]
        check("竞争期间保管人未被夺走（仍为 S001）", tube["custodian"]["code"] == "S001")
        check("只剩一个活动交接", tube["active_handoff"] is not None)

        # ---------------------------------------------------------------
        print(f"{INFO} 场景 2：扫码器重发（同键并发双发接受）")
        reset(client)
        r, _ = create(client)
        code = r.json()["handoff"]["code"]
        akey = key("accept")
        barrier = threading.Barrier(2)

        def race_accept():
            barrier.wait()
            with httpx.Client() as c:
                return post(c, f"/api/handoffs/{code}/accept", {"staff_code": "S002", "operation_key": akey})

        with ThreadPoolExecutor(max_workers=2) as pool:
            a1, a2 = [f.result() for f in [pool.submit(race_accept), pool.submit(race_accept)]]
        check("两次扫码均返回 200", a1.status_code == a2.status_code == 200, f"{a1.status_code}/{a2.status_code}")
        check("一次首执行、一次重放",
              sorted(h.headers.get("x-idempotent-replay") == "true" for h in (a1, a2)) == [False, True])
        check("accepted_at 完全一致",
              a1.json()["handoff"]["accepted_at"] == a2.json()["handoff"]["accepted_at"])

        # ---------------------------------------------------------------
        print(f"{INFO} 场景 3：确认响应丢失后重发（同键并发双发确认）")
        ckey = key("confirm")
        barrier = threading.Barrier(2)

        def race_confirm():
            barrier.wait()
            with httpx.Client() as c:
                return post(c, f"/api/handoffs/{code}/confirm", {"staff_code": "S001", "operation_key": ckey})

        with ThreadPoolExecutor(max_workers=2) as pool:
            c1, c2 = [f.result() for f in [pool.submit(race_confirm), pool.submit(race_confirm)]]
        check("两次确认均返回 200", c1.status_code == c2.status_code == 200, f"{c1.status_code}/{c2.status_code}")
        check("一次首执行、一次重放",
              sorted(h.headers.get("x-idempotent-replay") == "true" for h in (c1, c2)) == [False, True])
        check("completed_at 完全一致（只完成一次）",
              c1.json()["handoff"]["completed_at"] == c2.json()["handoff"]["completed_at"])
        tube = get(client, "/api/tubes/T-1001").json()["tube"]
        check("保管人唯一且为接收员 S002", tube["custodian"]["code"] == "S002")
        check("完成后无活动交接", tube["active_handoff"] is None)

        # ---------------------------------------------------------------
        print(f"{INFO} 场景 4：同键异参冲突")
        shared = key("samekey")
        r1, _ = create(client, tube="T-1002", to="S002", op_key=shared)
        r2, _ = create(client, tube="T-1002", to="S003", op_key=shared)
        check("首次成功", r1.status_code == 201, f"HTTP {r1.status_code}")
        check("异参重放被拒为 409 idempotency_conflict",
              r2.status_code == 409 and r2.json()["error"]["code"] == "idempotency_conflict")
        check("错误字段按 JSON Pointer 汇总", "/operation_key" in r2.json()["error"].get("fields", {}))

        # ---------------------------------------------------------------
        print(f"{INFO} 场景 5：旧交接不能覆盖已完成归属")
        stale = post(client, f"/api/handoffs/{code}/confirm",
                     {"staff_code": "S001", "operation_key": key("stale")})
        check("旧交接再次确认返回 completed 而非再次流转",
              stale.status_code == 200 and stale.json()["handoff"]["status"] == "completed")

        # 完成后非指定接收员（S003）再次扫码：必须 403，不能假成功
        intruder = post(client, f"/api/handoffs/{code}/accept",
                        {"staff_code": "S003", "operation_key": key("intruder")})
        check("完成后非接收员扫码被拒 403 not_receiver",
              intruder.status_code == 403 and intruder.json()["error"]["code"] == "not_receiver",
              f"HTTP {intruder.status_code}")
        # 指定接收员本人重扫：幂等 200 completed
        receiver = post(client, f"/api/handoffs/{code}/accept",
                        {"staff_code": "S002", "operation_key": key("receiver-rescan")})
        check("指定接收员完成后重扫幂等返回 completed",
              receiver.status_code == 200 and receiver.json()["handoff"]["status"] == "completed")

        tube = get(client, "/api/tubes/T-1001").json()["tube"]
        check("保管人仍为 S002", tube["custodian"]["code"] == "S002")

        # ---------------------------------------------------------------
        print(f"{INFO} 场景 6：到期边界——只封闭、不夺管，随后可重新发起")
        reset(client)
        r, _ = create(client)
        code2 = r.json()["handoff"]["code"]
        h0 = get(client, f"/api/handoffs/{code2}").json()["handoff"]
        created = datetime.fromisoformat(h0["created_at"])
        expires = datetime.fromisoformat(h0["expires_at"])
        check("有效期恰为 600 秒", int((expires - created).total_seconds()) == 600)
        check("刚创建时未到期", h0["expired"] is False and h0["seconds_remaining"] >= 599)

        expired = client.post(
            f"{BACKEND}/api/test/handoffs/{code2}/expire",
            headers={"X-Test-Token": TEST_RESET_TOKEN} if TEST_RESET_TOKEN else {},
            timeout=30,
        )
        check("到期模拟成功", expired.status_code == 204, f"HTTP {expired.status_code}")

        h = get(client, f"/api/handoffs/{code2}").json()["handoff"]
        check("交接状态封闭为 expired", h["status"] == "expired")
        check("封闭不改保管人（仍 S001）", h["custodian_staff_code"] == "S001")
        acc_key = key("exp-a")
        con_key = key("exp-c")
        acc = post(client, f"/api/handoffs/{code2}/accept",
                   {"staff_code": "S002", "operation_key": acc_key})
        con = post(client, f"/api/handoffs/{code2}/confirm",
                   {"staff_code": "S001", "operation_key": con_key})
        check("到期后接受被拒 410", acc.status_code == 410, f"HTTP {acc.status_code}")
        check("到期后确认被拒 410", con.status_code == 410, f"HTTP {con.status_code}")
        # 到期失败命令的重放
        acc_replay = post(client, f"/api/handoffs/{code2}/accept",
                          {"staff_code": "S002", "operation_key": acc_key})
        check("到期失败重放返回同一 410",
              acc_replay.status_code == 410 and acc_replay.headers.get("x-idempotent-replay") == "true")
        tube = get(client, "/api/tubes/T-1001").json()["tube"]
        check("失败交接没有夺走样本", tube["custodian"]["code"] == "S001")

        r, _ = create(client)
        check("到期封闭后可以重新发起", r.status_code == 201, f"HTTP {r.status_code}")
        new_code = r.json()["handoff"]["code"]
        post(client, f"/api/handoffs/{new_code}/accept",
             {"staff_code": "S002", "operation_key": key("new-a")})
        done = post(client, f"/api/handoffs/{new_code}/confirm",
                    {"staff_code": "S001", "operation_key": key("new-c")})
        check("重新发起的交接可完成并归属 S002",
              done.status_code == 200 and done.json()["handoff"]["custodian_staff_code"] == "S002")

        # ---------------------------------------------------------------
        print(f"{INFO} 场景 7：字段校验按 JSON Pointer 汇总")
        val = post(client, "/api/handoffs", {"tube_code": "!", "to_staff_code": "S002", "operation_key": "x"})
        check("422 且字段以 JSON Pointer 列出",
              val.status_code == 422 and
              set(val.json()["error"]["fields"]) == {"/tube_code", "/from_staff_code", "/operation_key"})

        # ---------------------------------------------------------------
        print(f"{INFO} 场景 8：验收钩子默认关闭且需令牌（防裸奔）")
        # 演练后端（backend-verify，令牌开启）：无令牌/错令牌必须被拒 401
        no_token = client.post(f"{BACKEND}/api/test/reset", timeout=30)
        check("无令牌调用重置被拒（401；默认关闭时为 404）",
              no_token.status_code in (401, 404), f"HTTP {no_token.status_code}")
        wrong_token = client.post(
            f"{BACKEND}/api/test/reset", headers={"X-Test-Token": "wrong"}, timeout=30
        )
        check("错误令牌调用重置被拒（401；默认关闭时为 404）",
              wrong_token.status_code in (401, 404), f"HTTP {wrong_token.status_code}")
        # 公开入口（页面访客经 nginx 打到安全后端）：钩子根本未挂载，一律 404，
        # 即使带上验收令牌也无法通过公开入口清空数据
        public_reset = client.post(f"{PUBLIC_API}/api/test/reset", timeout=30)
        check("公开入口不暴露重置接口（404）",
              public_reset.status_code == 404, f"HTTP {public_reset.status_code}")
        public_reset_token = client.post(
            f"{PUBLIC_API}/api/test/reset",
            headers={"X-Test-Token": TEST_RESET_TOKEN} if TEST_RESET_TOKEN else {},
            timeout=30,
        )
        check("公开入口即使持验收令牌仍不可清空（404）",
              public_reset_token.status_code == 404, f"HTTP {public_reset_token.status_code}")
        # 持正确令牌经验收后端可正常调用（204）
        guarded = client.post(
            f"{BACKEND}/api/test/handoffs/{new_code}/expire",
            headers={"X-Test-Token": TEST_RESET_TOKEN} if TEST_RESET_TOKEN else {},
            timeout=30,
        )
        check("持正确令牌经验收后端调用成功（204）", guarded.status_code == 204, f"HTTP {guarded.status_code}")

        # ---------------------------------------------------------------
        print(f"{INFO} 场景 9：旧数据升级 → 基线 → 连续两次合法交接 → 时间边界投影")

        # 9.1 旧数据升级：reset 后账本存在基线；直接清空账本（模拟升级前的旧版本库），
        # 再调用仅验收环境挂载的升级钩子，等价于服务带着新账本启动时的 backfill
        reset(client)
        h = get(client, "/api/tubes/T-1001/history").json()["history"]
        check("升级后冻存管自带 seq=0 基线",
              h["evidence_available"] is True and h["event_seq"] == 0 and h["handoff_code"] is None)

        with psycopg.connect(DATABASE_URL) as conn, conn.cursor() as cur:
            cur.execute("TRUNCATE custody_events")
            conn.commit()
        missing = get(client, "/api/tubes/T-1001/history")
        check("无账本时历史接口返回可识别错误 custody_baseline_missing",
              missing.status_code == 409 and
              missing.json()["error"]["code"] == "custody_baseline_missing",
              f"HTTP {missing.status_code}")
        # 当前管码查询结果不受影响（页面保留当前查询）
        cur_tube = get(client, "/api/tubes/T-1001").json()["tube"]
        check("缺基线时当前管码查询仍返回保管人", cur_tube["custodian"]["code"] == "S001")

        # 让基线生效于一个明确晚于“升级前时刻”的时间：先把数据库时钟基线时刻记下
        upgrade_resp = client.post(
            f"{BACKEND}/api/test/backfill-baselines",
            headers={"X-Test-Token": TEST_RESET_TOKEN} if TEST_RESET_TOKEN else {},
            timeout=30,
        )
        check("升级钩子为旧数据补齐基线",
              upgrade_resp.status_code == 200 and upgrade_resp.json()["backfilled"] == 2,
              upgrade_resp.text[:200])
        # 幂等：再次执行不重复补
        upgrade_again = client.post(
            f"{BACKEND}/api/test/backfill-baselines",
            headers={"X-Test-Token": TEST_RESET_TOKEN} if TEST_RESET_TOKEN else {},
            timeout=30,
        )
        check("升级基线补齐幂等（第二次补 0 条）",
              upgrade_again.status_code == 200 and upgrade_again.json()["backfilled"] == 0)

        before_upgrade = urllib.parse.quote("2000-01-01T00:00:00+00:00", safe="")
        h0 = get(client, f"/api/tubes/T-1001/history?at={before_upgrade}").json()["history"]
        check("早于可追溯起点直接说明无历史证据",
              h0["evidence_available"] is False and h0["custodian"] is None and h0["event_seq"] is None)
        check("无证据时不返回交接码/前后变更项",
              h0["handoff_code"] is None and h0["previous_change"] is None and h0["next_change"] is None)

        # 9.2 连续两次合法交接：S001 → S002 → S003
        def complete(tube, frm, to):
            r, _ = create(client, tube=tube, frm=frm, to=to)
            assert r.status_code == 201, f"create {r.status_code} {r.text[:200]}"
            c = r.json()["handoff"]["code"]
            ra = post(client, f"/api/handoffs/{c}/accept",
                      {"staff_code": to, "operation_key": key("h-a")})
            assert ra.status_code == 200, f"accept {ra.status_code}"
            rc = post(client, f"/api/handoffs/{c}/confirm",
                      {"staff_code": frm, "operation_key": key("h-c")})
            assert rc.status_code == 200, f"confirm {rc.status_code} {rc.text[:200]}"
            effective_at = rc.json()["handoff"]["completed_at"]
            return c, effective_at

        code1, t1 = complete("T-1001", "S001", "S002")
        code2, t2 = complete("T-1001", "S002", "S003")

        def history_at(tube, at=None):
            url = f"/api/tubes/{tube}/history"
            if at is not None:
                url += f"?at={at}"
            return get(client, url).json()["history"]

        from datetime import datetime as _dt, timedelta as _td

        def iso(dt, delta_microseconds=0):
            t = _dt.fromisoformat(dt) + _td(microseconds=delta_microseconds)
            return urllib.parse.quote(t.isoformat(), safe="")

        # 9.3 边界前后归属与序号准确（等于生效时刻已生效）
        before1 = history_at("T-1001", iso(t1, -1))
        check("首次变更前 1 微秒：归属 S001 / seq=0",
              before1["custodian"]["code"] == "S001" and before1["event_seq"] == 0)
        check("前一变更项为空、后一变更项指向交接码 1",
              before1["previous_change"] is None
              and before1["next_change"]["seq"] == 1
              and before1["next_change"]["handoff_code"] == code1)

        at1 = history_at("T-1001", iso(t1))
        check("恰好生效时刻 1：归属 S002 / seq=1 / 交接码正确",
              at1["custodian"]["code"] == "S002" and at1["event_seq"] == 1
              and at1["handoff_code"] == code1,
              str((at1["custodian"], at1["event_seq"], at1["handoff_code"])))
        check("seq=1 前一变更项为基线、后一变更项指向交接码 2",
              at1["previous_change"]["kind"] == "baseline"
              and at1["next_change"]["seq"] == 2
              and at1["next_change"]["handoff_code"] == code2)
        check("时间轴含全部事件且仅指定时刻事件被标记",
              [e["seq"] for e in at1["timeline"]] == [0, 1, 2]
              and [e["seq"] for e in at1["timeline"] if e["active_at_point"]] == [1])
        check("时间轴事件含人员变更方向与交接码",
              at1["timeline"][2]["from_staff"]["code"] == "S002"
              and at1["timeline"][2]["to_staff"]["code"] == "S003"
              and at1["timeline"][2]["handoff_code"] == code2)

        at2 = history_at("T-1001", iso(t2))
        check("恰好生效时刻 2：归属 S003 / seq=2",
              at2["custodian"]["code"] == "S003" and at2["event_seq"] == 2
              and at2["handoff_code"] == code2
              and at2["previous_change"]["custodian"]["code"] == "S002"
              and at2["next_change"] is None)
        check("缺省时刻（当前）投影账本尾部 S003",
              history_at("T-1001")["custodian"]["code"] == "S003")

        # 9.4 一次失败交接：接收员接受后模拟到期，确认必须失败且不留事件
        r, _ = create(client, tube="T-1002", frm="S001", to="S002")
        bad_code = r.json()["handoff"]["code"]
        post(client, f"/api/handoffs/{bad_code}/accept",
             {"staff_code": "S002", "operation_key": key("bad-a")})
        client.post(f"{BACKEND}/api/test/handoffs/{bad_code}/expire",
                    headers={"X-Test-Token": TEST_RESET_TOKEN} if TEST_RESET_TOKEN else {}, timeout=30)
        bad_confirm = post(client, f"/api/handoffs/{bad_code}/confirm",
                           {"staff_code": "S001", "operation_key": key("bad-c")})
        check("到期交接确认失败（410）", bad_confirm.status_code == 410, f"HTTP {bad_confirm.status_code}")
        h_bad = history_at("T-1002")
        check("失败交接不留保管事件（账本仍只有基线）",
              [e["seq"] for e in h_bad["timeline"]] == [0]
              and h_bad["custodian"]["code"] == "S001")

        # 9.5 响应丢失重放不重复追加：对 code2 的确认结果用同键再取一次
        replay = post(client, f"/api/handoffs/{code2}/confirm",
                      {"staff_code": "S002", "operation_key": key("replay-check")})
        # completed 后用“新键”只是幂等返回，不会产生新事件；这里直接核对账本行数
        h_after = history_at("T-1001")
        check("幂等返回不重复追加事件（T-1001 仍为 seq 0/1/2）",
              replay.status_code == 200
              and [e["seq"] for e in h_after["timeline"]] == [0, 1, 2])

        # 真正的“响应丢失重放”：新建一条交接，并发双发同一确认键，只允许追加一条事件
        r, _ = create(client, tube="T-1002", frm="S001", to="S003")
        c3 = r.json()["handoff"]["code"]
        post(client, f"/api/handoffs/{c3}/accept",
             {"staff_code": "S003", "operation_key": key("c3-a")})
        ckey = key("c3-c")
        barrier = threading.Barrier(2)

        def race_c3_confirm():
            barrier.wait()
            with httpx.Client() as c:
                return post(c, f"/api/handoffs/{c3}/confirm",
                            {"staff_code": "S001", "operation_key": ckey})

        with ThreadPoolExecutor(max_workers=2) as pool:
            x1, x2 = [f.result() for f in [pool.submit(race_c3_confirm), pool.submit(race_c3_confirm)]]
        check("双发确认均 200 且仅一次首执行",
              x1.status_code == x2.status_code == 200
              and sorted(h.headers.get("x-idempotent-replay") == "true" for h in (x1, x2)) == [False, True])
        h_c3 = history_at("T-1002")
        check("响应丢失重放只追加一条转移事件（基线 + seq=1）",
              [e["seq"] for e in h_c3["timeline"]] == [0, 1]
              and h_c3["custodian"]["code"] == "S003"
              and h_c3["handoff_code"] == c3)

        # 9.6 注入断链：删掉 seq=1 后历史接口必须拒绝，且当前查询仍保留
        with psycopg.connect(DATABASE_URL) as conn, conn.cursor() as cur:
            cur.execute("""
                DELETE FROM custody_events
                WHERE tube_id = (SELECT id FROM tubes WHERE code = 'T-1001') AND seq = 1
            """)
            conn.commit()
        broken = get(client, "/api/tubes/T-1001/history")
        check("注入断链（缺 seq=1）历史接口拒绝 custody_chain_broken",
              broken.status_code == 409 and broken.json()["error"]["code"] == "custody_chain_broken",
              f"HTTP {broken.status_code} {broken.text[:120]}")
        still = get(client, "/api/tubes/T-1001")
        check("断链时页面当前查询结果仍可用（S003）",
              still.status_code == 200 and still.json()["tube"]["custodian"]["code"] == "S003")
        # 指定时刻投影同样拒绝，绝不给出误导结果
        broken_at = get(client, f"/api/tubes/T-1001/history?at={iso(t1)}")
        check("断链后任意时刻投影都拒绝",
              broken_at.status_code == 409
              and broken_at.json()["error"]["code"] == "custody_chain_broken")
        # 非法时刻参数：422 且字段指针为 /at
        bad_at = get(client, "/api/tubes/T-1001/history?at=not-a-time")
        check("非法历史时刻 422 且字段指向 /at",
              bad_at.status_code == 422 and "/at" in bad_at.json()["error"].get("fields", {}))

        # 为结尾的“数据库最终一致性断言”重建一个干净的规范状态：
        # T-1001 完成一次 S001→S002 交接，T-1002 保持基线 S001（断链注入随之清除）
        reset(client)
        r, _ = create(client, tube="T-1001", frm="S001", to="S002")
        canonical_code = r.json()["handoff"]["code"]
        post(client, f"/api/handoffs/{canonical_code}/accept",
             {"staff_code": "S002", "operation_key": key("canon-a")})
        post(client, f"/api/handoffs/{canonical_code}/confirm",
             {"staff_code": "S001", "operation_key": key("canon-c")})


    # ---------------------------------------------------------------
    print(f"{INFO} 数据库最终一致性断言")
    if DATABASE_URL:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT tube_id, count(*) FROM handoffs "
                    "WHERE status IN ('pending','accepted') GROUP BY tube_id HAVING count(*) > 1"
                )
                violations = cur.fetchall()
                check("数据库中不存在同管多个活动交接", violations == [], f"违规 {violations}")

                cur.execute("""
                    SELECT t.code, s.code
                    FROM tubes t JOIN staff s ON s.id = t.custodian_id
                    WHERE t.code IN ('T-1001','T-1002') ORDER BY t.code
                """)
                custodians = dict(cur.fetchall())
                check("T-1001 保管人为 S002", custodians.get("T-1001") == "S002", str(custodians))
                check("T-1002 保管人保持 S001（冲突/失败未夺管）",
                      custodians.get("T-1002") == "S001", str(custodians))

                cur.execute("SELECT status, count(*) FROM handoffs GROUP BY status ORDER BY status")
                print(f"    {INFO} handoffs 状态分布：{dict(cur.fetchall())}")

                # 保管账本不变量：每管 seq 从 0 连续、首条为基线、尾部保管人与当前保管人一致
                cur.execute("""
                    SELECT t.code, ce.seq, ce.kind, count(*) OVER (PARTITION BY ce.tube_id)
                    FROM tubes t JOIN custody_events ce ON ce.tube_id = t.id
                    ORDER BY t.code, ce.seq
                """)
                rows = cur.fetchall()
                ledger: dict[str, list] = {}
                for tube_code, seq, kind, _ in rows:
                    ledger.setdefault(tube_code, []).append((seq, kind))
                ledger_ok = True
                for tube_code, chain in ledger.items():
                    seqs = [s for s, _ in chain]
                    if seqs != list(range(len(seqs))) or chain[0] != (0, "baseline"):
                        ledger_ok = False
                    if any(k != "transfer" for _, k in chain[1:]):
                        ledger_ok = False
                check("保管账本：每管序号从 0 连续且首条为基线、其后均为 transfer",
                      ledger_ok and set(ledger) == {"T-1001", "T-1002"}, str(ledger))

                cur.execute("""
                    SELECT t.code FROM tubes t
                    JOIN custody_events ce ON ce.id = (
                        SELECT id FROM custody_events WHERE tube_id = t.id ORDER BY seq DESC LIMIT 1
                    )
                    WHERE ce.custodian_id <> t.custodian_id
                """)
                mismatches = cur.fetchall()
                check("账本尾部保管人与冻存管当前保管人全部一致", mismatches == [], str(mismatches))

                cur.execute("""
                    SELECT count(*) FROM custody_events ce
                    JOIN handoffs h ON h.id = ce.handoff_id
                    WHERE ce.kind = 'transfer'
                      AND (ce.from_staff_id <> h.from_staff_id OR ce.to_staff_id <> h.to_staff_id
                           OR ce.handoff_code <> h.code)
                """)
                check("每条转移事件与对应交接的双方及交接码一致", cur.fetchone()[0] == 0)
    else:
        print("    （未提供 VERIFY_DATABASE_URL，跳过直连库断言）")

    print()
    if failures:
        print(f"{FAIL} 验收失败：{len(failures)} 项未通过")
        for f in failures:
            print(f"    - {f}")
        return 1
    print(f"{PASS} 验收通过：所有故障场景下页面/API 与数据库一致，冻存管保管人唯一，失败交接未夺走样本")
    return 0


if __name__ == "__main__":
    sys.exit(main())

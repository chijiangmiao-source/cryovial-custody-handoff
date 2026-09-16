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
  7. 直连数据库断言：每管活动交接数 ≤ 1，保管人唯一且与页面 API 一致。

通过则退出码 0，任一断言失败退出码 1。
"""
from __future__ import annotations

import os
import sys
import threading
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
        raise RuntimeError(
            f"重置失败（HTTP {r.status_code}）。后端必须以 SAMPLE_ENABLE_TEST_RESET=true 启动，"
            "且 TEST_RESET_TOKEN 与后端 SAMPLE_TEST_RESET_TOKEN 一致。"
        )


def main() -> int:
    print(f"{INFO} 后端：{BACKEND}")
    print(f"{INFO} 前端：{WEB}")

    with httpx.Client() as client:
        wait_backend(client)
        reset(client)

        # 0. 前端静态页面由 nginx 提供
        r = client.get(WEB, timeout=10)
        check("前端页面可访问且返回 HTML", r.status_code == 200 and "<div id=\"root\"" in r.text)

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

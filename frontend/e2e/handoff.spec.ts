import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

const BASE = process.env.E2E_BASE_URL ?? "http://localhost:5173";
const TEST_TOKEN = process.env.TEST_RESET_TOKEN ?? "test-token";
const TEST_HEADERS = { "X-Test-Token": TEST_TOKEN };

async function reset(api: APIRequestContext) {
  const r = await api.post("/api/test/reset", { headers: TEST_HEADERS });
  expect(r.status()).toBe(204);
}

async function apiCreate(
  api: APIRequestContext,
  opts: { tube?: string; from?: string; to?: string; key?: string } = {},
) {
  const operation_key = opts.key ?? `e2e-create-${Math.random().toString(36).slice(2)}`;
  const r = await api.post("/api/handoffs", {
    data: {
      tube_code: opts.tube ?? "T-1001",
      from_staff_code: opts.from ?? "S001",
      to_staff_code: opts.to ?? "S002",
      operation_key,
    },
  });
  return { r, operation_key, body: r.ok() ? await r.json() : null };
}

async function readCustodian(api: APIRequestContext, tube = "T-1001") {
  const r = await api.get(`/api/tubes/${tube}`);
  expect(r.ok()).toBeTruthy();
  return (await r.json()).tube;
}

async function gotoFresh(page: Page) {
  await page.goto(BASE);
  await expect(page.getByTestId("create-button")).toBeEnabled();
}

test.describe.configure({ mode: "serial" });

test("完整流程 + 扫码器重发：重复扫码重放首次结果，最终唯一保管人", async ({ page, request }) => {
  await reset(request);
  await gotoFresh(page);

  await page.getByTestId("create-button").click();
  await expect(page.getByTestId("accept-code")).toHaveValue(/^[A-Z0-9]{8}$/);
  const code = await page.getByTestId("accept-code").inputValue();
  await expect(page.getByTestId("handoff-status")).toHaveText("待接受");

  // 接收员扫码，扫码器重发：连续两次同一操作键
  await page.getByTestId("accept-button").click();
  await expect(page.getByTestId("handoff-status")).toHaveText("已接受");
  await page.getByTestId("accept-button").click();
  await expect(page.getByTestId("notice")).toContainText(/重放首次结果|accepted|已接受/);

  // 转出员最终确认
  await page.getByTestId("confirm-button").click();
  await expect(page.getByTestId("handoff-status")).toHaveText("已完成");
  await expect(page.getByTestId("custodian")).toHaveText("S002");

  const tube = await readCustodian(request);
  expect(tube.custodian.code).toBe("S002");
  expect(tube.active_handoff).toBeNull();

  // 旧交接不能再改归属：对同一交接再次确认（新操作键）
  const r = await request.post(`/api/handoffs/${code}/confirm`, {
    data: { staff_code: "S001", operation_key: `e2e-stale-${Math.random().toString(36).slice(2)}` },
  });
  expect(r.status()).toBe(200);
  expect((await r.json()).handoff.status).toBe("completed");
  const tube2 = await readCustodian(request);
  expect(tube2.custodian.code).toBe("S002");
});

test("确认响应丢失后重试：只完成一次，completed_at 相同", async ({ page, request }) => {
  await reset(request);
  const { body } = await apiCreate(request);
  const code = body.handoff.code;
  const acceptKey = `e2e-accept-${Math.random().toString(36).slice(2)}`;
  const ra = await request.post(`/api/handoffs/${code}/accept`, {
    data: { staff_code: "S002", operation_key: acceptKey },
  });
  expect(ra.status()).toBe(200);

  // 模拟确认响应丢失后重发：同一操作键连发两次
  const confirmKey = `e2e-confirm-${Math.random().toString(36).slice(2)}`;
  const payload = { staff_code: "S001", operation_key: confirmKey };
  const [c1, c2] = await Promise.all([
    request.post(`/api/handoffs/${code}/confirm`, { data: payload }),
    request.post(`/api/handoffs/${code}/confirm`, { data: payload }),
  ]);
  expect(c1.status()).toBe(200);
  expect(c2.status()).toBe(200);
  const b1 = await c1.json();
  const b2 = await c2.json();
  expect(b1.handoff.completed_at).toBe(b2.handoff.completed_at);
  const replays = [c1, c2].filter((r) => r.headers()["x-idempotent-replay"] === "true");
  expect(replays).toHaveLength(1);

  // 页面经查询进入后展示与数据库一致
  await gotoFresh(page);
  await page.getByTestId("lookup-input").fill("T-1001");
  await page.getByTestId("lookup-button").click();
  await expect(page.getByTestId("lookup-custodian")).toContainText("S002");
  await expect(page.getByTestId("lookup-result")).toContainText("无活动交接");
});

test("两次创建竞争：只有一个活动交接，失败方不夺走样本", async ({ page, request }) => {
  await reset(request);
  const keyA = `race-a-${Math.random().toString(36).slice(2)}`;
  const keyB = `race-b-${Math.random().toString(36).slice(2)}`;
  const [ra, rb] = await Promise.all([
    request.post("/api/handoffs", {
      data: { tube_code: "T-1001", from_staff_code: "S001", to_staff_code: "S002", operation_key: keyA },
    }),
    request.post("/api/handoffs", {
      data: { tube_code: "T-1001", from_staff_code: "S001", to_staff_code: "S003", operation_key: keyB },
    }),
  ]);
  const statuses = [ra.status(), rb.status()].sort();
  expect(statuses).toEqual([201, 409]);
  const failed = ra.status() === 409 ? await ra.json() : await rb.json();
  expect(failed.error.code).toBe("active_handoff_exists");
  expect(failed.error.fields["/tube_code"]).toBeTruthy();

  // 页面上再发起一次也得到冲突提示
  await gotoFresh(page);
  // 接收员改为 S003（与胜出者不同）
  await page.locator("select").nth(1).selectOption("S003");
  await page.getByTestId("create-button").click();
  await expect(page.getByTestId("notice")).toContainText("进行中的交接");
  await expect(page.getByTestId("notice")).toHaveClass(/conflict/);

  const tube = await readCustodian(request);
  expect(tube.custodian.code).toBe("S001");
  expect(tube.active_handoff).not.toBeNull();

  // 同键异参冲突
  const sameKey = `samekey-${Math.random().toString(36).slice(2)}`;
  await request.post("/api/handoffs", {
    data: { tube_code: "T-1002", from_staff_code: "S001", to_staff_code: "S002", operation_key: sameKey },
  });
  const conflict = await request.post("/api/handoffs", {
    data: { tube_code: "T-1002", from_staff_code: "S001", to_staff_code: "S003", operation_key: sameKey },
  });
  expect(conflict.status()).toBe(409);
  expect((await conflict.json()).error.code).toBe("idempotency_conflict");
});

test("到期边界：过期只封闭交接、不改保管人，页面显示已到期，之后可重新发起", async ({ page, request }) => {
  await reset(request);
  await gotoFresh(page);
  await page.getByTestId("create-button").click();
  const code = await page.getByTestId("accept-code").inputValue();

  // 模拟创建后超过十分钟
  const expired = await request.post(`/api/test/handoffs/${code}/expire`, { headers: TEST_HEADERS });
  expect(expired.status()).toBe(204);

  // 接收员再扫码：已到期
  await page.getByTestId("accept-button").click();
  await expect(page.getByTestId("notice")).toContainText("到期");
  await expect(page.getByTestId("notice")).toHaveClass(/expired/);
  await expect(page.getByTestId("handoff-status")).toHaveText("已到期");
  await expect(page.getByTestId("custodian")).toHaveText("S001");

  // 数据库一致：保管人未变、交接已封闭
  const tube = await readCustodian(request);
  expect(tube.custodian.code).toBe("S001");
  expect(tube.active_handoff).toBeNull();
  const h = await request.get(`/api/handoffs/${code}`);
  expect((await h.json()).handoff.status).toBe("expired");

  // 同键再试 accept/confirm 都 410，且不改状态
  for (const step of ["accept", "confirm"]) {
    const r = await request.post(`/api/handoffs/${code}/${step}`, {
      data: { staff_code: step === "accept" ? "S002" : "S001", operation_key: `exp-${step}-${Math.random().toString(36).slice(2)}` },
    });
    expect(r.status()).toBe(410);
  }
  const tube2 = await readCustodian(request);
  expect(tube2.custodian.code).toBe("S001");

  // 到期封闭后可重新发起并完成
  const again = await apiCreate(request);
  expect(again.r.status()).toBe(201);
  const newCode = again.body.handoff.code;
  expect(newCode).not.toBe(code);
  await request.post(`/api/handoffs/${newCode}/accept`, {
    data: { staff_code: "S002", operation_key: `new-a-${Math.random().toString(36).slice(2)}` },
  });
  const done = await request.post(`/api/handoffs/${newCode}/confirm`, {
    data: { staff_code: "S001", operation_key: `new-c-${Math.random().toString(36).slice(2)}` },
  });
  expect((await done.json()).handoff.custodian_staff_code).toBe("S002");
});

test("字段错误按 JSON Pointer 汇总显示", async ({ request }) => {
  await reset(request);
  const r = await request.post("/api/handoffs", {
    data: { tube_code: "!", to_staff_code: "S002", operation_key: "x" },
  });
  expect(r.status()).toBe(422);
  const body = await r.json();
  expect(Object.keys(body.error.fields).sort()).toEqual(["/from_staff_code", "/operation_key", "/tube_code"]);
});

test("跨设备继续：转出员关页后，接收员凭 ?code 链接在新页面扫码接受", async ({ page, browser, request }) => {
  await reset(request);
  const { body } = await apiCreate(request);
  const code = body.handoff.code;

  // 转出员页面关闭；接收员在另一台设备/新上下文直接打开分享链接
  const ctx = await browser.newContext();
  const receiverPage = await ctx.newPage();
  await receiverPage.goto(`${BASE}/?code=${code}`);
  await expect(receiverPage.getByTestId("accept-code")).toHaveValue(code);

  await receiverPage.getByLabel("扫码员工号").selectOption("S002");
  await receiverPage.getByTestId("accept-button").click();
  await expect(receiverPage.getByTestId("handoff-status")).toHaveText("已接受");

  // 手动输入交接码入口也能打开
  await receiverPage.goto(BASE);
  await receiverPage.getByTestId("open-handoff-input").fill(code);
  await receiverPage.getByTestId("open-handoff-button").click();
  await expect(receiverPage.getByTestId("accept-code")).toHaveValue(code);
  await expect(receiverPage.getByTestId("handoff-status")).toHaveText("已接受");
  await ctx.close();
});

test("完成后非指定接收员再次扫码：提示无权而非成功，保管人不变", async ({ page, request }) => {
  await reset(request);
  const { body } = await apiCreate(request);
  const code = body.handoff.code;
  const ra = await request.post(`/api/handoffs/${code}/accept`, {
    data: { staff_code: "S002", operation_key: `s-${Math.random().toString(36).slice(2)}` },
  });
  expect(ra.status()).toBe(200);
  const rc = await request.post(`/api/handoffs/${code}/confirm`, {
    data: { staff_code: "S001", operation_key: `c-${Math.random().toString(36).slice(2)}` },
  });
  expect(rc.status()).toBe(200);

  // S003（非指定接收员）在页面上再次扫码
  await page.goto(`${BASE}/?code=${code}`);
  await expect(page.getByTestId("handoff-status")).toHaveText("已完成");
  await page.getByLabel("扫码员工号").selectOption("S003");
  await page.getByTestId("accept-button").click();
  await expect(page.getByTestId("notice")).toContainText("只有指定的接收员");
  await expect(page.getByTestId("handoff-status")).toHaveText("已完成");
  await expect(page.getByTestId("custodian")).toHaveText("S002");

  const tube = await readCustodian(request);
  expect(tube.custodian.code).toBe("S002");
});

test("默认无令牌时验收钩子不可用于清空数据", async ({ request }) => {
  const r1 = await request.post("/api/test/reset");
  expect([401, 404]).toContain(r1.status());
  const r2 = await request.post("/api/test/reset", { headers: { "X-Test-Token": "guessing" } });
  expect([401, 404]).toContain(r2.status());
});

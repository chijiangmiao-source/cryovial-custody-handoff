# 冷冻样本库 · 夜班冻存管交接

冻存管在夜班交接时，转出员可能提交后关页，扫码器也可能重发。本系统保证：

- **一管最多一个活动交接**：数据库部分唯一索引 + 行级事务锁双重保证，两次创建竞争必有一方得到 409；
- **每步重放返回首次结果**：每个命令带客户端生成的“操作键”，服务端持久化首次状态码与响应，断网/关页/响应丢失后用同一键重试得到完全相同的结果；
- **同键异参冲突**：同一操作键提交不同参数返回 409，且不执行任何业务逻辑；
- **接收员扫码接受后，仍为当前保管人的转出员才能最终确认**；旧交接不能覆盖已变更的归属；交接**完成后非指定接收员再次扫码会被拒为无权**（指定接收员本人重扫幂等返回完成状态）；
- **原子写入**：新保管人与 `completed` 状态在同一事务中提交，提交前对外不可见；
- **十分钟到期**：以**数据库时钟**（`statement_timestamp()`）为唯一时间源，创建 600 秒后到期，**截止时刻（等于到期时间）仍有效**，只封闭未完成交接，**不改变保管人**；
- **字段错误按 JSON Pointer 汇总**（如 `/tube_code`、`/operation_key`）。

## 技术栈

Python 3.13 · FastAPI · SQLAlchemy 2 · PostgreSQL 17 · TypeScript · React 18 · Vite 6 · Docker Compose。

## 目录结构

```
backend/          FastAPI 服务（持久化状态机、事务锁、幂等命令）
  app/main.py       应用入口与演示数据
  app/models.py     Staff / Tube / Handoff / CommandRecord 模型与部分唯一索引
  app/service.py    状态机：创建 / 接受 / 确认 / 到期封闭（行锁、DB 时间）
  app/idempotency.py 操作键：首次结果持久化、重放、同键异参冲突
  app/test_routes.py 验收钩子（仅令牌模式挂载，默认关闭）
  tests/            pytest（含真实多线程并发与到期边界）
frontend/         React + TS 页面（操作键持久化、断网重试、状态展示）
  src/keystore.ts     操作键 localStorage 持久化
  src/api.ts          命令重试（响应丢失不换键）
  src/useCommands.ts  命令分发 / 待处理操作重试
  e2e/                Playwright 端到端场景
verify/           一次性验收服务（黑盒并发演练 + 直连数据库核对）
docker-compose.yml
```

## 快速启动（Docker Compose）

```bash
# 启动数据库、后端、前端；前端宿主机端口默认 8080
docker compose up --build

# 自定义前端端口
WEB_PORT=9000 docker compose up --build
```

打开 <http://localhost:8080>（或 `$WEB_PORT`）。页面操作流程：

1. **转出员发起交接**：输入管码、目标接收员与操作键，生成交接码；页面展示可发送给接收员的**分享链接**（`/?code=交接码`）；
2. **接收员扫码接受**：即使转出员已经**关页或换设备**，接收员也可在任意设备直接打开分享链接、扫描二维码，或在页面顶部“凭交接码继续”入口输入/扫描交接码；
3. **转出员最终确认**：仅当接收员已接受、且转出员仍是当前保管人时可确认。

内置演示数据：员工 `S001 张敏`（转出/保管人）、`S002 李强`、`S003 王芳`；冻存管 `T-1001`、`T-1002`，初始保管人均为 `S001`。

### 一次性验收服务（默认配置即可直接完成）

默认 `docker compose up` 只对外提供**安全形态**：公开后端 `backend` 不挂载任何验收/重置接口
（`SAMPLE_ENABLE_TEST_RESET=false`，`/api/test/*` 一律 404），任何能访问页面的人都无法清空交接与保管记录。

验收能力内置在同一 `docker-compose.yml` 的 `verify` profile 中，与对外服务**物理隔离**：

- `backend-verify`：只在 compose 内网可达、**不发布宿主机端口**、开启钩子并强制 `X-Test-Token`；
- `verify`：跑完即退出的一次性服务，演练流量打内网 `backend-verify`，并额外断言**经 nginx 的公开入口**
  即使携带令牌也无法触达重置接口。

无需任何额外文件，一条命令即可完成验收（`run` 会自动激活 `verify` profile 并拉起 db/web/两个后端）：

```bash
docker compose run --rm verify        # 真实制造重复扫码、确认丢响应、两次创建竞争、
                                      # 同键异参、完成后非接收员扫码、到期边界等；退出码 0 即通过
docker compose down                   # 验收后清理
```

可通过 `TEST_RESET_TOKEN` 指定令牌（验收后端与 verify 两端必须一致，默认仅本地用的 `verify-local-token`）：

```bash
TEST_RESET_TOKEN=$(openssl rand -hex 16) docker compose run --rm verify
```

> 安全模型：页面访客 → nginx → 公开 `backend`（钩子未挂载，404）；`backend-verify` 不暴露端口、
> 仅内网 + 令牌可达。**面向真实数据的环境请保持默认 `up`，不要运行 verify profile，也不要发布 backend-verify 端口。**
>
> 验收钩子 `/api/test/reset`、`/api/test/handoffs/{code}/expire` 仅在
> `SAMPLE_ENABLE_TEST_RESET=true` 且配置了 `SAMPLE_TEST_RESET_TOKEN` 时挂载；
> 未开启时返回 404（路由不存在），开启但令牌缺失/错误时返回 401。

## 故障复现（手动）

以下用 `curl` 复现关键故障场景（`$B=http://localhost:8080/api`，经过 nginx 同源代理）。
先重置数据（验收环境需带令牌，且后端以 `SAMPLE_ENABLE_TEST_RESET=true` 启动）：

```bash
T="$TEST_RESET_TOKEN"   # 与后端 SAMPLE_TEST_RESET_TOKEN 一致；默认 compose 不开放该接口
curl -s -X POST $B/test/reset -H "X-Test-Token: $T"
```

### 1. 两次创建竞争：一管只有一个活动交接

```bash
curl -s -X POST $B/handoffs -H 'Content-Type: application/json' \
  -d '{"tube_code":"T-1001","from_staff_code":"S001","to_staff_code":"S002","operation_key":"race-a-0001"}' &
curl -s -X POST $B/handoffs -H 'Content-Type: application/json' \
  -d '{"tube_code":"T-1001","from_staff_code":"S001","to_staff_code":"S003","operation_key":"race-b-0002"}' &
wait
# 结果：一个 201，一个 409 active_handoff_exists；保管人仍为 S001
```

### 2. 扫码器重发：同一操作键重放首次结果

```bash
# 用上一步 201 返回的 code
curl -s -D - -X POST $B/handoffs/<CODE>/accept -H 'Content-Type: application/json' \
  -d '{"staff_code":"S002","operation_key":"scan-fixed-key"}'
# 再发一次（扫码器重发 / 关页后重试）
curl -s -D - -X POST $B/handoffs/<CODE>/accept -H 'Content-Type: application/json' \
  -d '{"staff_code":"S002","operation_key":"scan-fixed-key"}'
# 第二次带响应头 x-idempotent-replay: true，accepted_at 与首次完全一致
```

### 3. 确认响应丢失：同键重试，只完成一次

```bash
curl -s -X POST $B/handoffs/<CODE>/confirm -H 'Content-Type: application/json' \
  -d '{"staff_code":"S001","operation_key":"confirm-fixed-key"}' &
curl -s -X POST $B/handoffs/<CODE>/confirm -H 'Content-Type: application/json' \
  -d '{"staff_code":"S001","operation_key":"confirm-fixed-key"}' &
wait
# 两次都返回 200，completed_at 相同，一个首执行一个重放；保管人原子变为 S002
```

### 4. 同键异参冲突

```bash
curl -s -X POST $B/handoffs -H 'Content-Type: application/json' \
  -d '{"tube_code":"T-1002","from_staff_code":"S001","to_staff_code":"S002","operation_key":"same-key"}'
curl -s -X POST $B/handoffs -H 'Content-Type: application/json' \
  -d '{"tube_code":"T-1002","from_staff_code":"S001","to_staff_code":"S003","operation_key":"same-key"}'
# 第二次：409 idempotency_conflict，fields 指向 /operation_key，业务不执行
```

### 5. 旧交接不能覆盖已完成归属

完成后用**新操作键**再次确认，只返回当前 `completed` 状态，保管人不再变化；
若库外纠正导致保管人已变更而旧交接仍停留在 `accepted`，确认返回 409 `custodian_changed`。

### 6. 到期边界：截止时刻仍有效，过期只封闭不夺管

```bash
# 正常发起后，把到期时刻移到数据库当前时间之前 1 秒（验收钩子，需令牌）
curl -s -X POST $B/test/handoffs/<CODE>/expire -H "X-Test-Token: $T"
curl -s $B/handoffs/<CODE>                 # status=expired，custodian 仍 S001
curl -s -X POST $B/handoffs/<CODE>/accept -H 'Content-Type: application/json' \
  -d '{"staff_code":"S002","operation_key":"expired-accept"}'   # 410 handoff_expired
# 到期封闭后可用新操作键重新发起；截止时刻（now == expires_at）接受/确认仍然成功
```

### 7. 完成后非接收员再次扫码：提示无权

```bash
# 交接已完成后，非指定接收员 S003 再扫同一交接码 → 403，而非“接受成功”
curl -s -X POST $B/handoffs/<CODE>/accept -H 'Content-Type: application/json' \
  -d '{"staff_code":"S003","operation_key":"intruder-scan"}'
# {"error":{"code":"not_receiver", ...}}；保管人不变。指定接收员 S002 本人重扫则幂等返回 completed
```

### 8. 关页/换设备后凭码继续

交接码即凭证。转出员创建后可直接关页：接收员打开 `http://<host>:<port>/?code=<CODE>`
（或扫码二维码、在页面顶部输入交接码）即可在任意设备接受，无需停留在发起页面。

页面上：断网或响应丢失时，操作会进入“待处理操作”列表并保留操作键，网络恢复后点“重试”即可；
结果区会显示 **已接受 / 已完成 / 已到期 / 冲突**。

## 正确性设计要点

### 状态机与锁（`backend/app/service.py`）

状态：`pending → accepted → completed`，异常分支 `→ expired`。

- 所有命令先按固定顺序（冻存管行 → 交接行）执行 `SELECT … FOR UPDATE OF`，避免跨事务死锁；
- 创建时锁管并复查该管所有交接，发现活动交接即 409；PostgreSQL **部分唯一索引**
  `ux_handoffs_active_tube`（`status IN ('pending','accepted')` 时 `tube_id` 唯一）作为最后兜底；
- 确认时在同一事务内校验“转出员仍是当前保管人”，随后同时写 `tubes.custodian_id = 接收员` 与
  `handoffs.status = 'completed'`，原子提交；
- **接受（accept）的身份校验先于状态短路**：即使交接已 `completed`，非指定接收员扫码仍返回
  403 `not_receiver`；只有指定接收员本人重扫才幂等返回当前完成状态，避免“完成后谁扫都成功”；
- 到期判定为严格不等号 `now > expires_at`，因此**等于**截止时刻仍有效；仅把未完成状态置为
  `expired`，绝不更新保管人。所有 `now` 都取自 `statement_timestamp()`。

### 幂等命令（`backend/app/idempotency.py`）

- 每个操作键一行 `command_records`（`operation_key` 唯一），保存参数指纹、首次状态码与响应体；
- 并发双发时，第二个事务在唯一行上阻塞至首个事务提交，随后读到并**重放首次结果**（含 4xx 状态码，
  响应头 `X-Idempotent-Replay: true`）；
- 参数指纹不同 → 409 `idempotency_conflict`；
- 首次执行业务失败（如到期 410、无权 403）也会持久化，重放返回同一错误，不会因重试改判。

### 前端操作键与交接码入口（`frontend/src`）

- 操作键在发起前生成并写入 `localStorage`，断网、关页、重开都保留；
- `postCommand` 在网络错误/502/503/504 时用**同一个操作键**重试（响应可能已到服务端，换键才会造成重复）；
- 业务错误（冲突、到期、无权）视为服务端已有定论，命令标记已决，不再占用待处理列表；
- **交接码即凭证**：页面顶部“凭交接码继续”支持手动输入/扫码，并支持 `?code=<交接码>` 深链与复制分享链接，
  转出员关页或换设备后接收员仍可在任意设备继续接受。

## 本地开发与测试（不用 Docker）

需要 Python 3.13、Node 20+、PostgreSQL 17。

```bash
# 后端
python3.13 -m venv .venv && . .venv/bin/activate
pip install -r backend/requirements.txt
export SAMPLE_DATABASE_URL='postgresql+psycopg://USER@/sample_db?host=/var/run/postgresql'
cd backend
uvicorn app.main:app --reload --port 8000

# 前端（另开终端，/api 代理到 8000）
cd frontend && npm install && npm run dev

# 后端测试（真实多线程并发 + 冻结时钟的到期边界）
pytest

# 前端单元测试 / 类型检查
npm run test
npx tsc --noEmit

# 端到端：后端需以令牌开启验收钩子，前端 e2e 默认带 X-Test-Token: test-token
export SAMPLE_ENABLE_TEST_RESET=true
export SAMPLE_TEST_RESET_TOKEN=test-token
uvicorn app.main:app --reload --port 8000 &   # backend 目录下
npx playwright install chromium
npx playwright test
```

> 验收钩子 `/api/test/reset`、`/api/test/handoffs/{code}/expire` 仅在
> `SAMPLE_ENABLE_TEST_RESET=true` 且 `SAMPLE_TEST_RESET_TOKEN` 非空时挂载；
> 未开启返回 404，开启但令牌缺失/错误返回 401。**生产环境必须保持默认关闭。**

## API 摘要

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/handoffs` | 转出员发起（管码、转出员、接收员、操作键）→ 201 |
| POST | `/api/handoffs/{code}/accept` | 接收员扫码接受（工号、操作键） |
| POST | `/api/handoffs/{code}/confirm` | 转出员最终确认（工号、操作键） |
| GET | `/api/handoffs/{code}` | 查询交接（读路径顺带封闭到期交接） |
| GET | `/api/tubes/{code}` | 查询冻存管当前保管人与活动交接 |
| GET | `/api/staff`、`/api/health` | 名册 / 健康检查 |

错误响应统一为：

```json
{ "error": { "code": "active_handoff_exists", "message": "…", "fields": { "/tube_code": "…" } } }
```

业务错误码：`validation_error`、`active_handoff_exists`、`not_custodian`、`same_party`、
`not_receiver`、`not_owner`、`not_accepted`、`custodian_changed`、`handoff_expired`、
`idempotency_conflict` 等。

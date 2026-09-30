# 博物馆展柜温度暴露审定服务

对博物馆展柜在指定**审定窗口**内采集到的温度读数进行暴露审定：按时间排序后，
以“每条读数只覆盖到下一条读数”的采样保持语义切分区段，识别 **正常 / 超限 / 未知**
区段，计算**最长连续超限时长**，并给出审定结论；支持读数幂等提交、修订替换、
证书版本化签发（快照不可变）。

- 技术栈：Python 3.11 · FastAPI · SQLite（WAL 模式）· Pydantic v2
- 无外部服务依赖，`docker compose up -d` 一键启动并自动初始化数据库

---

## 1. 审定规则

设审定窗口为 `[start_at, end_at]`，允许温度闭区间 `[min, max]`，
最长连续超限时长 `T_exceed`，最大采样间隔 `T_gap`。

### 区段语义

1. 读数按时间戳升序排列。第 `i` 条读数覆盖半开区间 `[t_i, t_{i+1})`，
   区间内温度取读数 `i` 的温度（采样保持，直到下一条读数）。
2. 相邻读数间隔 **严格大于** `T_gap` 时，该区间整段记为 **未知**；
   恰好等于 `T_gap` 仍算有效。
3. 窗口起点到首条读数、末条读数之后到窗口终点这两段**边界无读数**区域，记为 **未知**。
4. 有效区间内：温度超出 `[min, max]`（等于边界不算超限）记为 **超限**，否则 **正常**。
5. 相邻且状态相同的区段会合并输出。

> **关于窗口终点读数**：读数提交的窗口是闭区间 `[start_at, end_at]`，
> 允许恰好在 `end_at` 时刻提交一条读数，它只作为“覆盖终止采样”
> （结束上一条读数的覆盖，自身区段时长为 0）。这样当首读在起点、
> 末读在终点、所有采样间隔合法且温度全部在区间内时，可以得到没有
> 未知区段的“合格”结论；输出的温度区段始终是半开 `[start_at, end_at)`。

### 结论判定（按优先级）

| 条件 | 结论 | `verdict` |
|---|---|---|
| 最长连续超限时长 **严格大于** `T_exceed` | 不合格 | `fail` |
| 未超限（或恰好等于限值），但存在未知区段 | 待定 | `pending` |
| 无超限超时、无未知区段 | 合格 | `pass` |

“最长连续超限”指相邻的超限区段时长之和，被正常/未知区段打断后重新累计。
即使存在未知区段，只要超限超时，结论仍为不合格。

### 读数提交、幂等与修订

读数以 `(reading_id, revision)` 标识，同一窗口内：

- **相同 ID + 相同修订号重复提交**：内容（时间戳、温度）完全一致 → 幂等返回
  （`effect: idempotent`）；内容不一致 → **409 内容冲突**。
- **更高修订号**：替换该 ID 的当前读数并立即重算（`effect: updated`），
  旧修订号在历史表中标记为 `superseded`。
- **旧修订号**（低于当前修订号）→ **409 拒绝**。
- 同一窗口内不同读数不允许同一时间戳 → **422 拒收**。
- 时间戳超出 `[start_at, end_at]` → **422 拒收**；所有时间戳必须带时区。

### 签发与并发

- 签发把**当前已提交状态**（窗口参数 + 全部活动读数 + 审定结果）固化为不可变快照，
  形成一个证书版本（`version` 从 1 递增）。
- 读数修订后再次签发产生**新版本**；旧版本证书与快照永久保留、内容不变。
- 数据库开启 **WAL**，写事务使用 `BEGIN IMMEDIATE` + `busy_timeout`：
  签发（读）与修订（写）并发时，以事务为边界取同一已提交状态——
  修订先提交则纳入本次签发，否则等签发完成后再生效，不会出现脏读或快照撕裂。

---

## 2. 启动

### Docker Compose（推荐，一键启动）

```bash
docker compose up -d --build
docker compose logs -f          # 查看日志
docker compose down             # 停止
```

启动后自动建表初始化（幂等）。SQLite 数据保存在命名卷 `audit-data`
（容器内 `/data/audit.db`），容器重建不丢数据。

健康检查：

```bash
curl http://localhost:8000/health
# {"status":"ok"}
```

### 本地直接运行（不用 Docker）

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 配置（环境变量）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `AUDIT_DB_PATH` | `./audit.db` | SQLite 数据库文件路径；Compose 中为 `/data/audit.db` |
| `AUDIT_DB_TIMEOUT` | `10` | 写锁等待秒数（对应 SQLite `busy_timeout` 毫秒数 ×1000） |
| `LOG_LEVEL` | `info` | 日志级别（透传给运行环境） |

### 地址与端口

- 服务地址：`http://localhost:8000`（Compose 映射 `8000:8000`，可在
  `docker-compose.yml` 的 `ports` 修改宿主机端口）
- Swagger 交互文档：<http://localhost:8000/docs>
- OpenAPI Schema：<http://localhost:8000/openapi.json>
- 健康检查：`GET /health`

---

## 3. HTTP API

所有请求/响应均为 JSON；时间戳必须是带时区的 ISO-8601（如
`2026-10-01T00:00:00Z` 或 `2026-10-01T08:00:00+08:00`，统一按时刻处理并以 UTC 输出）。

### 窗口

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/v1/windows` | 创建审定窗口（窗口倒置/温度区间倒置返回 422） |
| `GET` | `/api/v1/windows?limit=&offset=` | 窗口列表（分页） |
| `GET` | `/api/v1/windows/{id}` | 窗口详情：活动读数 + 当前审定 + 最新证书 |
| `GET` | `/api/v1/windows/{id}/evaluation` | 只看当前审定结果 |

创建窗口请求体：

```json
{
  "name": "展柜A-2026-10-01",
  "start_at": "2026-10-01T00:00:00Z",
  "end_at":   "2026-10-01T01:00:00Z",
  "allowed_temp_min": 18.0,
  "allowed_temp_max": 22.0,
  "max_continuous_exceed_seconds": 300,
  "max_sample_interval_seconds": 600
}
```

### 读数

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/v1/windows/{id}/readings` | 提交/幂等重放/修订一条读数，响应含重算结果 |
| `GET` | `/api/v1/windows/{id}/readings` | 当前活动读数列表（按时间升序） |

读数请求体：

```json
{ "reading_id": "r-01", "revision": 1,
  "measured_at": "2026-10-01T00:10:00Z", "temperature": 22.4 }
```

响应中的 `effect` 为 `created` / `updated` / `idempotent`。

### 审定结果结构

```json
{
  "verdict": "fail",
  "normal_segments":   [{"start":"...","end":"...","status":"normal",  "duration_seconds":60.0}],
  "exceeded_segments": [{"start":"...","end":"...","status":"exceeded","duration_seconds":420.0}],
  "unknown_segments":  [{"start":"...","end":"...","status":"unknown", "duration_seconds":3120.0}],
  "max_continuous_exceeded_seconds": 420.0,
  "max_exceed_seconds": 300.0,
  "has_exceeded": true,
  "has_unknown": true,
  "reading_count": 5
}
```

### 签发证书

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/v1/windows/{id}/certificates/issue` | 签发（可带 `{"note":"..."}`，也可空 body） |
| `GET` | `/api/v1/windows/{id}/certificates` | 该窗口全部证书版本（按版本升序） |
| `GET` | `/api/v1/certificates/{id}` | 按证书主键获取，`snapshot` 为签发时不可变快照 |
| `POST` | `/api/v1/certificates/revision-impact-report` | 对同一窗口旧版/新版两张证书生成修订影响报告 |

证书含 `id`、`window_id`、`version`、`verdict`、`verdict_label_zh`
（合格/待定/不合格）、`issued_at`、`note`、`snapshot`。

### 修订影响报告

输入同一审定窗口、按版本先后排列的两张**已签发**证书，报告**只依据两张证书
签发时固化的不可变快照**，不读取当前活动读数；既有证书内容与其它查询响应
完全不变。

请求体：

```json
{ "old_certificate_id": 1, "new_certificate_id": 2 }
```

响应结构：

- `window` / `old_certificate` / `new_certificate`：窗口与两版证书引用（版本、结论、签发时间）。
- `reading_diff`：按 `reading_id` 对齐两版快照读数，逐字段比较修订号、采样时间、温度：
  - `added_readings`：新版新增读数；`removed_readings`：新版缺失的旧版读数；
  - `changed_readings`：修订号/时间戳/温度任一变化，给出 `before`/`after`
    及三个 `*_changed` 标记；`unchanged_reading_ids`：完全未变的读数 ID。
- `changed_segments`：把两版审定时间线按**所有区段边界**（窗口起止 + 两版
  全部原始区段边界）切成半开区间对齐后，**仅输出状态发生变化的区间**。每段含：
  - `before` / `after`：变化前后状态（`normal`/`exceeded`/`unknown` 及中文标签）；
  - 各侧 `evidence`：支撑该状态的读数——采样保持区间给出 `from_reading`
    （读数 ID、修订号、时间、温度），采样间隔超限区间另给 `to_reading`；
  - 未知成因：`sample_gap`（相邻读数间隔严格大于 `max_sample_interval_seconds`）、
    `boundary_gap`（窗口起点到首读 / 末读到终点的边界空白）、
    `no_readings`（窗口内完全没有有效读数，前后边界空白同时存在、无法进一步区分）。
  - 相邻且两侧状态与证据（读数 ID+修订号、未知成因）均相同的区间已合并。
- `summary`：结论变化前后、`max_continuous_exceeded_seconds` 前后值与差值
  （即最长连续超限时长的变化）、状态转移计数、变化区域内各状态时长、中文结论。
- `basis.source = "certificate_snapshots"`：声明数据依据。

最小调用：

```bash
curl -sS http://localhost:8000/api/v1/certificates/revision-impact-report \
  -H 'Content-Type: application/json' \
  -d '{"old_certificate_id":1,"new_certificate_id":2}'
```

拒绝约定：

- `404`：旧版或新版证书不存在（错误信息标明是哪一张）；
- `409`：两张证书属于不同审定窗口；版本未按先后排列（旧版版本号不低于新版）；
  新旧为同一张证书；
- `422`：请求体缺字段或 ID 非法。

### 错误约定

- `404`：窗口/证书不存在
- `409`：同 ID+修订号内容冲突；旧修订号覆盖；修订影响报告的窗口不一致/版本倒置/同一证书
- `422`：无效窗口、窗口外读数、同刻不同读数、缺时区/字段非法等

---

## 4. 调用示例

服务启动后：

```bash
# curl 全流程（创建窗口 → 提交/幂等/冲突/修订 → 审定 → 两次签发 → 修订影响报告）
bash examples/curl-examples.sh

# Python 全流程（仅标准库，演示三种结论、拒收、修订、版本快照、修订影响报告）
python3 examples/python_client.py
# 或指定地址：
BASE_URL=http://127.0.0.1:8000 python3 examples/python_client.py
```

最小示例：

```bash
curl -sS http://localhost:8000/api/v1/windows \
  -H 'Content-Type: application/json' -d '{
    "start_at":"2026-10-01T00:00:00Z","end_at":"2026-10-01T00:10:00Z",
    "allowed_temp_min":18,"allowed_temp_max":22,
    "max_continuous_exceed_seconds":300,"max_sample_interval_seconds":600}'

curl -sS http://localhost:8000/api/v1/windows/1/readings \
  -H 'Content-Type: application/json' \
  -d '{"reading_id":"a","revision":1,"measured_at":"2026-10-01T00:00:00Z","temperature":20.1}'

curl -sS http://localhost:8000/api/v1/windows/1/evaluation
curl -sS -X POST http://localhost:8000/api/v1/windows/1/certificates/issue \
  -H 'Content-Type: application/json' -d '{"note":"首签"}'
```

---

## 5. 测试

```bash
pip install -r requirements.txt pytest
PYTHONPATH=. python -m pytest tests/ -q
```

- `tests/test_evaluation.py`：审定算法纯函数测试（未知/超限/合格、
  边界等于、时区归一、区段无缝拼接等）。
- `tests/test_api.py`：HTTP 集成测试（幂等、冲突、修订替换、拒收、
  证书版本快照、签发/修订并发串行化）。
- `tests/test_report.py`：修订影响报告纯函数测试（读数增改删、
  时间线边界对齐、边界空白/采样间隔超限/无读数三种未知成因、相邻区间合并）。
- `tests/test_report_api.py`：报告 HTTP API 集成测试（端到端报告、
  404/409/422 拒绝、报告只随快照不随当前活动读数变化）。

## 6. 目录结构

```
app/
  config.py        # 环境变量配置
  database.py      # SQLite 连接、WAL、建表、写事务（BEGIN IMMEDIATE）
  evaluation.py    # 核心审定算法（纯函数）
  models.py        # 结论枚举/常量
  schemas.py       # Pydantic 请求/响应模型
  services.py      # 窗口、读数、审定、签发业务逻辑
  main.py          # FastAPI 路由
examples/
  curl-examples.sh
  python_client.py
tests/
Dockerfile
docker-compose.yml
requirements.txt
```

# 素材发行授权核验 HTTP API

为内容团队构建的素材发行授权核验服务：登记素材及其发行授权（**地区、渠道、带时区的左闭右开时段、可发行次数**），对携带多个素材的发行申请做**整体核验、原子占用**，并支持已通过申请的查询与**一次性撤销（释放占用）**。

- 技术栈：Python 3.11 · FastAPI · SQLAlchemy 2 · PostgreSQL 16 · Pydantic v2
- 并发安全：数据库行锁 + 通道咨询锁 + 条件更新三重保障，**高并发下绝不超额**
- 一键启动：`docker compose up`，自动等待数据库就绪并初始化表结构

---

## 1. 快速开始（Docker Compose）

```bash
# 可选：复制环境变量配置并按需修改
cp .env.example .env

# 构建并一键启动（自动建表初始化）
docker compose up --build
```

启动后：

| 服务 | 地址 |
| --- | --- |
| API 服务 | <http://localhost:8000> |
| 交互式文档 Swagger UI | <http://localhost:8000/docs> |
| ReDoc 文档 | <http://localhost:8000/redoc> |
| 健康检查 | `GET http://localhost:8000/health` |
| PostgreSQL | `localhost:5432`（宿主机映射，容器内为 `db:5432`） |

停止：

```bash
docker compose down          # 停止，保留数据卷
docker compose down -v       # 停止并删除数据库数据卷（清空全部数据）
```

---

## 2. 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `POSTGRES_USER` | `licensing` | 数据库用户名 |
| `POSTGRES_PASSWORD` | `licensing` | 数据库密码 |
| `POSTGRES_DB` | `licensing` | 数据库名 |
| `DB_HOST_PORT` | `5432` | PostgreSQL 映射到宿主机的端口 |
| `API_HOST_PORT` | `8000` | API 映射到宿主机的端口 |
| `DATABASE_URL` | `postgresql+psycopg://licensing:licensing@db:5432/licensing` | 应用侧数据库连接串（compose 中由上面三个变量拼装） |
| `DB_WAIT_TIMEOUT` | `30` | 启动时等待数据库就绪的最长秒数 |
| `DB_POOL_SIZE` | `10` | 连接池常驻连接数 |
| `DB_MAX_OVERFLOW` | `20` | 连接池溢出连接数 |

数据库表在应用启动事件中通过 `Base.metadata.create_all` 幂等创建，**无需手工执行迁移**。

### 不用 Docker 的本地运行方式

```bash
pip install -r requirements.txt
export DATABASE_URL="postgresql+psycopg://用户:密码@主机:5432/licensing"
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

---

## 3. 接口一览

基准路径：`/api/v1`

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/materials` | 登记素材（业务编号唯一） |
| `GET` | `/materials/{id}` | 查询素材 |
| `POST` | `/authorizations` | 为素材登记一条授权 |
| `GET` | `/authorizations/{id}` | 查询授权（含已用/剩余次数） |
| `PATCH` | `/authorizations/{id}` | 启用/停用授权（仅影响新申请） |
| `POST` | `/authorizations/migrate` | **授权退役：原授权未撤销申请一次性改挂到替代授权** |
| `POST` | `/distributions` | **提交发行申请：整体核验并原子占用** |
| `GET` | `/distributions/{id}` | 查询发行申请（含各项命中授权） |
| `POST` | `/distributions/{id}/revoke` | **撤销申请（仅可一次），释放占用次数** |

### 核验与占用规则

1. 匹配维度：素材、`region`（地区）、`channel`（渠道）、发行时刻。
2. 时段语义：**左闭右开** `[starts_at, ends_at)`，命中条件
   `starts_at <= occur_at < ends_at`；所有时间必须携带时区（如 `+08:00`）。
3. 次数：授权含 `max_count` 可发行次数；每次整体通过的申请对命中授权占用 1 次。
4. **重叠授权选择**：当一个素材存在多条匹配授权时，按
   **到期时间 `ends_at` 升序、到期相同再按授权编号 `id` 升序**，
   跳过已停用/无余量者，选择第一条可用授权。
5. **整体判定**：申请中的每个素材都必须有「匹配、启用、有余量」的授权，
   申请才整体通过并落库占用；**任一项不满足则整体拒绝、列明每项原因、不扣减任何次数**。
6. **停用语义**：停用的授权不参与新申请匹配，但**不影响已通过的申请**；
   已通过申请仍可查询、可撤销。
7. **撤销**：已通过申请仅可成功撤销一次；撤销后逐项释放占用次数，
   申请记录保留且状态变为 `revoked`，仍可查询。重复撤销返回 `409`。

### 授权退役改挂规则

授权需要退役时，运营用**原授权编号 + 替代授权编号**，把原授权承载的
**全部未撤销（`approved`）发行申请一次性改挂**到替代授权：

1. 两端授权编号必须**不同**，且**素材、地区、渠道完全一致**。
2. 替代授权必须处于 **`active` 启用**状态；其时段（左闭右开）必须
   **覆盖每条待迁移申请的发行时刻**；且**剩余次数足够一次性承接
   全部迁入占用**。
3. **任一条件不满足则整体拒绝**：不改动任何申请归属，原/替代两端
   次数与状态均不变（原授权不会被停用）。
4. **成功后**：原授权置为 `inactive`（停用），其占用次数按迁移条数
   迁出（归零的部分即迁移条数）；替代授权按迁移条数增加占用；
   返回 `migrated_count` 与两端授权（含各自剩余次数）。
5. **已撤销申请不迁移**：`revoked` 申请的明细仍归属原授权、状态不变。
   原授权若已无未撤销申请，迁移数为 `0`，原授权仍会被停用（幂等，
   可重复调用）。
6. 改挂后的申请在查询接口中显示为**替代授权**；仍可通过原
   `POST /distributions/{id}/revoke` 接口撤销，**撤销只释放替代授权
   一次**，原授权次数不受影响。

### 错误响应格式

所有业务错误统一为：

```json
{
  "error": {
    "code": "distribution_rejected",
    "message": "授权核验未通过，未占用任何发行次数",
    "details": [
      {
        "item_index": 1,
        "material_id": 8,
        "reason": "quota_exhausted",
        "message": "匹配授权（id=4）可发行次数已用尽：3/3"
      }
    ]
  }
}
```

| HTTP | code | 触发场景 |
| --- | --- | --- |
| 422 | 请求体校验错误（FastAPI 默认格式） | 非法时段（`starts_at >= ends_at`）、时间无时区、次数非正整数等 |
| 409 | `duplicate_material` | 素材业务编号已存在 |
| 422 | `unknown_material` | 登记授权/发行申请引用了未知素材编号 |
| 422 | `duplicate_material_items` | 同一发行申请中携带重复素材 |
| 422 | `distribution_rejected` | 核验未通过；`details` 按项列明原因（见下） |
| 422 | `migration_rejected` | 授权改挂核验未通过；`details` 列明原因（见下），归属与次数均不变 |
| 409 | `already_revoked` | 对已撤销申请重复撤销 |
| 404 | `not_found` | 路径中的素材/授权/申请编号不存在（改挂时原/替代授权任一不存在） |

`distribution_rejected` 的逐项原因 `reason`：

- `no_matching_authorization`：没有同时匹配地区、渠道与发行时刻的授权
- `authorization_inactive`：匹配的授权均已停用
- `quota_exhausted`：匹配授权的可发行次数已用尽

`migration_rejected` 的原因 `reason`（`details` 中附带两端授权编号）：

- `same_authorization`：原授权与替代授权编号相同
- `dimension_mismatch`：两端素材、地区或渠道不一致
- `replacement_inactive`：替代授权未启用
- `period_not_covered`：替代授权时段未覆盖某些申请的发行时刻
- `insufficient_quota`：替代授权余量不足以一次性承接全部迁入占用

---

## 4. 调用示例（curl）

```bash
# 1) 登记两个素材
curl -s -X POST http://localhost:8000/api/v1/materials \
  -H 'Content-Type: application/json' \
  -d '{"code": "VIDEO-0001", "name": "秋季宣传片"}'

curl -s -X POST http://localhost:8000/api/v1/materials \
  -H 'Content-Type: application/json' \
  -d '{"code": "IMG-0007", "name": "封面图"}'

# 2) 登记授权：中国大陆 / web 渠道 / 10 月（左闭右开，带时区）/ 3 次
curl -s -X POST http://localhost:8000/api/v1/authorizations \
  -H 'Content-Type: application/json' \
  -d '{
    "material_id": 1,
    "region": "CN",
    "channel": "web",
    "starts_at": "2026-10-01T00:00:00+08:00",
    "ends_at":   "2026-11-01T00:00:00+08:00",
    "max_count": 3
  }'

curl -s -X POST http://localhost:8000/api/v1/authorizations \
  -H 'Content-Type: application/json' \
  -d '{
    "material_id": 2,
    "region": "CN",
    "channel": "web",
    "starts_at": "2026-10-01T00:00:00+08:00",
    "ends_at":   "2026-11-01T00:00:00+08:00",
    "max_count": 3
  }'

# 3) 提交发行申请（两个素材同时通过才占用）
curl -s -X POST http://localhost:8000/api/v1/distributions \
  -H 'Content-Type: application/json' \
  -d '{
    "material_ids": [1, 2],
    "region": "CN",
    "channel": "web",
    "occur_at": "2026-10-04T15:30:00+08:00"
  }'
# 201：返回申请 id 及每个素材命中的授权（含剩余次数）

# 4) 查询已通过的申请
curl -s http://localhost:8000/api/v1/distributions/1

# 5) 撤销（仅可一次），释放占用次数
curl -s -X POST http://localhost:8000/api/v1/distributions/1/revoke
# 再撤销一次 -> 409 already_revoked

# 6) 停用授权（仅影响之后的新申请）
curl -s -X PATCH http://localhost:8000/api/v1/authorizations/1 \
  -H 'Content-Type: application/json' -d '{"status": "inactive"}'
curl -s -X PATCH http://localhost:8000/api/v1/authorizations/1 \
  -H 'Content-Type: application/json' -d '{"status": "active"}'

# 7) 授权退役改挂：把授权 1 承载的全部未撤销申请一次性改挂到授权 3
curl -s -X POST http://localhost:8000/api/v1/authorizations/migrate \
  -H 'Content-Type: application/json' \
  -d '{"source_authorization_id": 1, "replacement_authorization_id": 3}'
# 200：{"migrated_count": 2,
#       "source_authorization":      {... "used_count": 0, "remaining": 3, "status": "inactive"},
#       "replacement_authorization": {... "used_count": 2, "remaining": 8, "status": "active"}}

# 改挂后查询申请显示替代授权；仍可用原接口撤销，撤销只释放替代授权一次
curl -s http://localhost:8000/api/v1/distributions/1
curl -s -X POST http://localhost:8000/api/v1/distributions/1/revoke
```

成功的发行申请响应示例：

```json
{
  "id": 1,
  "region": "CN",
  "channel": "web",
  "occur_at": "2026-10-04T15:30:00+08:00",
  "status": "approved",
  "created_at": "2026-10-04T07:30:00Z",
  "revoked_at": null,
  "items": [
    {
      "item_index": 0,
      "material_id": 1,
      "authorization": {
        "id": 1, "material_id": 1, "region": "CN", "channel": "web",
        "starts_at": "2026-10-01T00:00:00+08:00",
        "ends_at": "2026-11-01T00:00:00+08:00",
        "max_count": 3, "used_count": 1, "remaining": 2,
        "status": "active",
        "created_at": "2026-10-04T07:00:00Z"
      }
    }
  ]
}
```

---

## 5. 并发控制设计

所有写事务在单个数据库事务内完成，并遵守统一的全局加锁顺序，
**从根上杜绝跨事务死锁**：

```
通道咨询锁 pg_advisory_xact_lock(region+channel)
        → 申请行锁（distributions，id 升序）
        → 授权行锁（authorizations，id 升序）
        → 明细行锁（distribution_items，id 升序）
```

1. **通道咨询锁 `pg_advisory_xact_lock`**：按 `region+channel` 分桶，
   同一通道内的提交、撤销、改挂由此串行判定，消除「各自读到有余量、
   先后扣减导致超额」以及「改挂与撤销各读到一半旧状态」的竞态；
   事务提交/回滚时自动释放（同事务内重入同把锁安全）。
2. **行锁 `SELECT ... FOR UPDATE`**：申请行锁串行化对同一申请的并发
   撤销（恰为一次 `200`、一次 `409`）；授权行始终按 `id` 全局升序
   加锁；改挂与撤销还会锁定相关**明细行**，固定「明细 → 授权」的
   当前归属——并发下撤销要么在改挂前提交（释放原授权），要么等待改挂
   提交后释放替代授权，绝不会重复释放或释放错授权。
3. **条件更新**：提交扣次 `WHERE used_count < max_count`、改挂迁入
   `WHERE used_count <= max_count - n` / 迁出 `WHERE used_count >= n`、
   撤销释放 `WHERE used_count >= 1`，并断言影响行数，作为超额/重复
   释放的最后防线。

核验/改挂不通过即 `ROLLBACK`，不产生任何次数或归属变化。任意时刻
都有不变量：`used_count` 恒等于归属该授权且处于 `approved` 状态的
占用明细数，且 `0 <= used_count <= max_count`。

---

## 6. 测试

测试套件基于真实 PostgreSQL（非模拟），通过 FastAPI TestClient 发起 HTTP 请求：

- `tests/test_api.py`：43 项端到端用例，覆盖左闭右开边界、地区/渠道匹配、
  次数耗尽、撤销释放、停用语义、整体拒绝原子性、重复素材、未知编号、
  重复撤销、重叠授权选择顺序等。
- `tests/test_migrate.py`：63 项授权退役改挂用例，覆盖正常改挂与次数搬移、
  查询显示替代授权、改挂后撤销只释放替代授权一次、已撤销不迁移、幂等空迁移、
  多素材批量、两端相同/维度不一致/替代停用/时段不覆盖（含左闭右开边界）/
  余量不足五类拒绝的原子性、404/422 等。
- `tests/test_concurrency.py`：并发安全用例（20/30 并发抢占、多素材批量、
  撤销与新申请交错、并发双重撤销、**改挂与撤销并发、改挂与提交并发、
  并发重复改挂**），断言绝不超额、不重复释放且账实相符。

```bash
pip install -r requirements.txt httpx
# 指向任意可用 PostgreSQL 16
export DATABASE_URL="postgresql+psycopg://licensing@localhost:5432/licensing"
python3 -m tests.test_api
python3 -m tests.test_migrate
python3 -m tests.test_concurrency
```

> 测试会先 `DROP` 再重建全部表，请勿指向含生产数据的数据库。

## 7. 项目结构

```
.
├── Dockerfile
├── docker-compose.yml
├── .env.example
├── requirements.txt
├── README.md
├── app/
│   ├── main.py        # FastAPI 路由与异常处理
│   ├── config.py      # 环境变量配置
│   ├── database.py    # 引擎/会话/启动建表
│   ├── models.py      # Material / Authorization / Distribution(+Item)
│   ├── schemas.py     # 请求响应模型与时段/时区校验
│   ├── errors.py      # 业务错误码
│   └── services.py    # 核验、原子占用、撤销（核心事务逻辑）
└── tests/
    ├── test_api.py
    ├── test_migrate.py
    └── test_concurrency.py
```

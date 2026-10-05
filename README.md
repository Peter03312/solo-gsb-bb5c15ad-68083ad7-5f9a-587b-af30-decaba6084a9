# 素材发行授权核验 HTTP API

为内容团队构建的素材发行授权核验服务：登记素材及其发行授权（**地区、渠道、带时区的左闭右开时段、可发行次数**），对携带多个素材的发行申请做**整体核验、原子占用**，支持已通过申请的查询与**一次性撤销（释放占用）**，支持授权退役时**一次性改挂**；并支持发行方按素材、地区、渠道、带时区发行时刻与 1～30 分钟有效期**预留发行额度**，到期前确认即把预留原子转为一笔已通过申请。

- 技术栈：Python 3.11 · FastAPI · SQLAlchemy 2 · PostgreSQL 16 · Pydantic v2
- 并发安全：数据库行锁 + 通道咨询锁 + 条件更新三重保障，**高并发下绝不超额、不重复释放**
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
| `RESERVATION_MIN_TTL_MINUTES` | `1` | 预留有效期下限（分钟，闭区间） |
| `RESERVATION_MAX_TTL_MINUTES` | `30` | 预留有效期上限（分钟，闭区间） |

数据库表在应用启动事件中通过 `Base.metadata.create_all` 幂等创建（预留相关
新表 `reservations` / `reservation_items` 与授权新列 `reserved_count`
同样自动创建），**无需手工执行迁移**。

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
| `POST` | `/reservations` | **预留发行额度：整体核验、逐项占额，返回预留编号与到期时刻** |
| `GET` | `/reservations` | 预留列表（可按 `status` 过滤 pending/confirmed/cancelled/expired） |
| `GET` | `/reservations/{id}` | 查询单个预留（含逐项授权与确认后挂接的申请） |
| `POST` | `/reservations/{id}/confirm` | **到期前确认：原子转一笔已通过申请（重复确认返回同一申请）** |
| `POST` | `/reservations/{id}/cancel` | **取消预留（仅可一次），释放预留额度** |

### 核验与占用规则

1. 匹配维度：素材、`region`（地区）、`channel`（渠道）、发行时刻。
2. 时段语义：**左闭右开** `[starts_at, ends_at)`，命中条件
   `starts_at <= occur_at < ends_at`；所有时间必须携带时区（如 `+08:00`）。
3. 次数：授权含 `max_count` 可发行次数；每次整体通过的申请对命中授权占用 1 次。
   未到期、待确认的预留同样占额（见下文「额度预留」），即新申请能否通过
   取决于 `used_count + reserved_count < max_count`。
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
7. **与预留的互斥**：原授权存在未到期、待确认预留（`reserved_count>0`）
   时改挂**整体拒绝**（`source_has_pending_reservations`），须等待这些
   预留确认/取消/过期后再改挂；替代授权**自身的未到期预留占额计入其
   迁入余量**。已确认预留转出的正式申请与普通申请一样随明细正常迁移。

### 额度预留规则

发行方可凭**素材编号列表、地区、渠道、带时区发行时刻、有效期
（`ttl_minutes`，服务端配置的闭区间内，默认 1～30 分钟）**预留发行额度：

1. **匹配与选择顺序沿用正式申请**：同样的素材/地区/渠道/左闭右开时刻
   匹配，重叠授权同样按 `ends_at` 升序、`id` 升序选第一条启用且有余量
   的授权；**所有素材同时通过才生成预留编号**，逐项锁定授权、写入到期
   时刻（创建时刻 + `ttl_minutes`），响应中逐项回传命中授权与各授权的
   **可预留余量 `available`**。
2. **任一项失败均不占额并列明原因**：错误码 `reservation_rejected`，
   `details` 逐项给出 `no_matching_authorization` /
   `authorization_inactive` / `quota_exhausted`（消息中同时给出
   正式占用/预留占用/总额度）；重复素材、未知编号在占额前直接拒绝。
3. **占额口径**：授权新增 `reserved_count`（未到期待确认预留的逐项占额）。
   - `used_count` 与 `remaining = max_count - used_count`
     **语义与旧版完全兼容**，仍只统计已通过（未撤销）正式申请；
   - 新增 `available = max_count - used_count - reserved_count`，
     即新申请与新预留此刻还能使用的可预留余量；
   - **未到期预留同时压低新申请与其他预留的余量**。
4. **到期即释放**：到期语义为 `expires_at <= now`（**到期时刻确认视为
   逾期**）。采用惰性清理——通道写事务与授权/预留查询在判定前会把该通道
   到期的 pending 预留原子置为 `expired` 并逐项释放 `reserved_count`，
   无需后台任务、时钟到点即释放，释放后额度立即可被复用。
5. **确认（`POST /reservations/{id}/confirm`）**：到期前确认把预留
   **原子转为一笔已通过申请**（新建 `approved` 发行申请与逐项明细，
   同一事务内 `reserved_count -= 1`、`used_count += 1`，净占用不变）。
   - 预留时逐项选中的授权被固定，**确认不再重新匹配**——因此授权停用
     不影响已有预留的确认；
   - **重复确认返回同一申请**（同一 `distribution_id`，200 幂等，
     不重复占额）；该申请与普通申请一样可用
     `POST /distributions/{id}/revoke` 撤销，撤销只释放 `used_count`。
6. **取消（`POST /reservations/{id}/cancel`）与确认互斥**：
   - 取消成功逐项释放 `reserved_count`，仅可一次；**重复取消返回 409
     `reservation_already_cancelled`，不再释放额度**；
   - 取消后确认 → 409 `reservation_cancelled`；
     已确认后取消 → 409 `reservation_already_confirmed`；
   - 过期后确认 → 409 `reservation_expired`；过期后取消幂等返回该
     `expired` 预留（额度早已释放，不重复释放）。
7. **四态查询**：`pending`（待确认）、`confirmed`（已确认）、
   `cancelled`（已取消）、`expired`（已过期）；
   `GET /reservations?status=pending` 等按状态过滤（过滤前会先清理到期
   预留，pending 列表不会残留已到期记录）。

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
| 422 | `reservation_rejected` | 预留核验未通过/TTL 越界；整体不占额，`details` 列明原因 |
| 422 | `migration_rejected` | 授权改挂核验未通过；`details` 列明原因（见下），归属与次数均不变 |
| 409 | `already_revoked` | 对已撤销申请重复撤销 |
| 409 | `reservation_expired` | 预留已过期（含到期时刻确认）；确认被拒绝，额度已释放 |
| 409 | `reservation_cancelled` | 对已取消预留执行确认（取消与确认互斥） |
| 409 | `reservation_already_confirmed` | 对已确认预留执行取消 |
| 409 | `reservation_already_cancelled` | 对已取消预留重复取消（不再释放额度） |
| 404 | `not_found` | 路径中的素材/授权/申请/预留编号不存在（改挂时原/替代授权任一不存在） |

`distribution_rejected` / `reservation_rejected` 的逐项原因 `reason`
（两类接口核验口径一致）：

- `no_matching_authorization`：没有同时匹配地区、渠道与发行时刻的授权
- `authorization_inactive`：匹配的授权均已停用（停用仅影响新申请与新预留）
- `quota_exhausted`：匹配授权的可发行次数已用尽（消息给出
  正式占用/预留占用/总额度）
- `invalid_ttl`：仅预留请求，`ttl_minutes` 不在服务端配置区间内

`migration_rejected` 的原因 `reason`（`details` 中附带两端授权编号）：

- `same_authorization`：原授权与替代授权编号相同
- `dimension_mismatch`：两端素材、地区或渠道不一致
- `replacement_inactive`：替代授权未启用
- `period_not_covered`：替代授权时段未覆盖某些申请的发行时刻
- `insufficient_quota`：替代授权余量（计入其自身未到期预留占额后）不足以一次性承接全部迁入占用
- `source_has_pending_reservations`：原授权存在未到期、待确认预留，改挂整体拒绝

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

# 8) 预留发行额度（10 分钟有效；所有素材同时通过才生成预留编号）
curl -s -X POST http://localhost:8000/api/v1/reservations \
  -H 'Content-Type: application/json' \
  -d '{
    "material_ids": [1, 2],
    "region": "CN",
    "channel": "web",
    "occur_at": "2026-10-04T15:30:00+08:00",
    "ttl_minutes": 10
  }'
# 201：返回预留 id、expires_at、逐项命中授权（含 available 可预留余量）

# 9) 到期前确认：原子转为一笔已通过申请；重复确认返回同一申请
curl -s -X POST http://localhost:8000/api/v1/reservations/1/confirm

# 10) 取消预留（仅可一次，与确认互斥）
curl -s -X POST http://localhost:8000/api/v1/reservations/1/cancel
# 再取消一次 -> 409 reservation_already_cancelled
# 已取消/已过期后确认 -> 409 reservation_cancelled / reservation_expired

# 11) 查询：单个预留 / 按状态过滤列表（pending|confirmed|cancelled|expired）
curl -s http://localhost:8000/api/v1/reservations/1
curl -s "http://localhost:8000/api/v1/reservations?status=pending"
```

成功的预留响应示例（pending；确认后 `status=confirmed` 并带 `distribution`）：

```json
{
  "id": 1,
  "region": "CN",
  "channel": "web",
  "occur_at": "2026-10-04T15:30:00+08:00",
  "expires_at": "2026-10-05T02:22:40.561101Z",
  "status": "pending",
  "created_at": "2026-10-05T02:12:40.561101Z",
  "confirmed_at": null,
  "cancelled_at": null,
  "distribution_id": null,
  "distribution": null,
  "items": [
    {
      "item_index": 0,
      "material_id": 1,
      "authorization": {
        "id": 1, "material_id": 1, "region": "CN", "channel": "web",
        "starts_at": "2026-10-01T00:00:00+08:00",
        "ends_at": "2026-11-01T00:00:00+08:00",
        "max_count": 3, "used_count": 0, "remaining": 3,
        "reserved_count": 1, "available": 1,
        "status": "active",
        "created_at": "2026-10-04T07:00:00Z"
      }
    }
  ]
}
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

所有写事务在单个数据库事务内完成，并遵守统一的全局加锁约定，
**从根上杜绝跨事务死锁**：

```
通道咨询锁 pg_advisory_xact_lock(region+channel)   ← 所有写事务第一加锁点
        → 对象行锁（distributions / reservations，id 升序）
        → 相关授权行锁（authorizations，一律 id 升序）
        → 明细行锁（distribution_items / reservation_items，id 升序）
```

同一通道内的写事务由咨询锁完全串行，因此对象行与明细行、授权行的加锁
先后随操作类型略有不同（如撤销先锁申请再锁明细、最后锁授权；改挂先锁
两端授权再锁申请与明细），但**同一类行永远按 id 升序加锁、且全部发生在
咨询锁之后**，不会出现跨事务相反等待环。

1. **通道咨询锁 `pg_advisory_xact_lock`**：按 `region+channel` 分桶，
   同一通道内的提交、预留、确认、取消、撤销、改挂由此串行判定，
   消除「各自读到有余量、先后扣减导致超额」以及「确认/取消/改挂各读到
   一半旧状态」的竞态；事务提交/回滚时自动释放（同事务内重入同把锁安全）。
   预留确认转出的正式申请继承预留的通道维度，因此不会跨桶加锁。
2. **行锁 `SELECT ... FOR UPDATE`**：预留/申请行锁串行化对同一对象的
   并发确认/取消/撤销（幂等成功与 409 互斥各恰一次）；授权行始终按 `id`
   全局升序加锁；改挂与撤销/确认还会锁定相关明细行，固定「明细 → 授权」
   的当前归属——并发下要么在对方提交前执行（释放原授权），要么等待对方
   提交后执行新归属，绝不会重复释放或释放错授权。
3. **条件更新**：提交/预留扣次 `WHERE used_count + reserved_count < max_count`、
   预留确认 `reserved_count-1 / used_count+1`（各带前置条件）、
   取消与到期释放 `WHERE reserved_count >= 1`、改挂迁入
   `WHERE used_count + reserved_count <= max_count - n` /
   迁出 `WHERE used_count >= n`、撤销释放 `WHERE used_count >= 1`，
   并断言影响行数；并发事务在 FOR UPDATE 等待后读到的是对方已提交的
   新版本，条件不满足即回滚并按最新状态走幂等/互斥分支，
   作为超额/重复释放的最后防线。

核验/改挂/预留不通过即 `ROLLBACK`，不产生任何次数或归属变化。任意时刻
都有不变量：`used_count` 恒等于归属该授权且处于 `approved` 状态的
正式占用明细数、`reserved_count` 恒等于归属该授权且处于 `pending`
状态的预留明细数，且 `0 <= used_count`、`0 <= reserved_count`、
`used_count + reserved_count <= max_count`。

**到期释放（惰性清理 sweep）**：不依赖后台定时任务。每个通道写事务
拿到咨询锁后的第一步，以及授权查询、预留查询/列表，都会在独立或当前
事务内把该通道 `expires_at <= now` 的 pending 预留（预留行 id 升序
`FOR UPDATE` → 授权 id 升序 `FOR UPDATE`）原子置为 `expired` 并逐项
条件释放。因此到期时刻的确认必在清理之后被判定为逾期，释放出的额度
立即可被新申请/新预留使用。

---

## 6. 测试

测试套件基于真实 PostgreSQL（非模拟），通过 FastAPI TestClient 发起 HTTP 请求：

- `tests/test_api.py`：端到端用例，覆盖左闭右开边界、地区/渠道匹配、
  次数耗尽（含预留占额口径）、撤销释放、停用语义、整体拒绝原子性、
  重复素材、未知编号、重复撤销、重叠授权选择顺序等。
- `tests/test_migrate.py`：授权退役改挂用例，覆盖正常改挂与次数搬移、
  查询显示替代授权、改挂后撤销只释放替代授权一次、已撤销不迁移、幂等空迁移、
  多素材批量、两端相同/维度不一致/替代停用/时段不覆盖（含左闭右开边界）/
  余量不足/源有待确认预留拒绝的原子性、404/422 等。
- `tests/test_reservations.py`：**额度预留功能用例（93 项）**，覆盖
  创建整体核验与逐项占额、available 余量口径、确认原子转申请与幂等重复确认、
  取消互斥与单次释放、到期时刻边界与惰性释放、四态查询与列表过滤、
  停用不影响已有预留确认、多素材原子拒绝、改挂与预留的互斥/协同、
  TTL 1～30 与各类参数校验。
- `tests/test_concurrency.py`：并发安全用例（20/30 并发抢占、多素材批量、
  撤销与新申请交错、并发双重撤销、**改挂与撤销并发、改挂与提交并发、
  并发重复改挂**），断言绝不超额、不重复释放且账实相符。
- `tests/test_reservation_concurrency.py`：**预留并发安全用例（9 个场景）**：
  并发预留抢占、申请与预留混合并发、同一预留并发确认、确认/取消并发互斥、
  并发重复取消、确认后撤销、**改挂与预留并发**、多素材批量预留、
  到期后并发确认/取消与额度复用，以数据库实际账
  （used/reserved 与各状态明细数）断言不超额、不重复释放。

```bash
pip install -r requirements.txt httpx
# 指向任意可用 PostgreSQL 16（预留特性同样兼容 PostgreSQL 15）
export DATABASE_URL="postgresql+psycopg://licensing@localhost:5432/licensing"
python3 -m tests.test_api
python3 -m tests.test_migrate
python3 -m tests.test_concurrency
python3 -m tests.test_reservations
python3 -m tests.test_reservation_concurrency
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
│   ├── config.py      # 环境变量配置（含预留 TTL 区间）
│   ├── database.py    # 引擎/会话/启动建表
│   ├── models.py      # Material / Authorization / Distribution(+Item)
│   │                  #   / Reservation(+Item)
│   ├── schemas.py     # 请求响应模型与时段/时区/TTL 校验
│   ├── errors.py      # 业务错误码
│   └── services.py    # 核验、原子占用、撤销、改挂、预留/确认/取消/到期清理
└── tests/
    ├── test_api.py
    ├── test_migrate.py
    ├── test_concurrency.py
    ├── test_reservations.py
    └── test_reservation_concurrency.py
```

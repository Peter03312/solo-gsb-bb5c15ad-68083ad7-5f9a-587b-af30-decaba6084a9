"""额度预留端到端功能测试（真实 PostgreSQL + TestClient）。

覆盖：
- 预留创建：整体核验、逐项授权选择（沿用重叠选择顺序）、返回预留编号/
  到期时刻/逐项授权/可预留余量；任一项失败整体拒绝、不占任何额度并列明原因；
- 余量口径：未到期预留同时压低新申请与其他预留的 available；
- 确认：到期前确认原子转一笔已通过申请；重复确认返回同一申请；
  授权停用不影响已有预留确认；
- 取消：与确认互斥、仅释放一次、重复取消 409、取消后确认 409；
- 过期：到期时刻确认视为逾期；过期即释放（读/写惰性清理）；
  过期后确认拒绝、取消幂等；
- 查询四态区分 + 状态过滤列表；
- 改挂：原授权存在未到期待确认预留时整体拒绝，取消/确认后放行；
  替代授权自身预留占额计入迁入余量；
- 校验：ttl 1～30 分钟（可由环境变量配置）、重复素材、未知编号、
  无时区时刻、404 等。

运行：
    DATABASE_URL=postgresql+psycopg://licensing@127.0.0.1:5439/licensing \
        python3 -m tests.test_reservations
"""
import os
import sys
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg://licensing@127.0.0.1:5439/licensing",
)

from app.database import Base, SessionLocal, engine, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app import models  # noqa: E402

CST = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 4, 12, 0, tzinfo=CST)
OCCUR = "2026-10-04T15:30:00+08:00"

passed = 0
failed = 0


def check(name: str, cond: bool, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


def force_expire(reservation_id: int, *, seconds_ago: float = 1.0) -> None:
    """直接把 expires_at 改到过去，模拟 TTL 到期（服务端用当前时间判定）。"""
    db = SessionLocal()
    try:
        row = db.get(models.Reservation, reservation_id)
        row.expires_at = datetime.now(timezone.utc) - timedelta(
            seconds=seconds_ago
        )
        db.commit()
    finally:
        db.close()


def main() -> int:
    Base.metadata.drop_all(bind=engine)
    init_db()

    with TestClient(app) as c:
        def new_material(code):
            return c.post(
                "/api/v1/materials", json={"code": code, "name": code}
            ).json()["id"]

        def new_auth(mid, count, region, channel="web", max_ends=True):
            return c.post(
                "/api/v1/authorizations",
                json={
                    "material_id": mid,
                    "region": region,
                    "channel": channel,
                    "starts_at": (NOW - timedelta(days=1)).isoformat(),
                    "ends_at": (
                        NOW + timedelta(days=1 if max_ends else 0)
                    ).isoformat(),
                    "max_count": count,
                },
            ).json()["id"]

        def reserve(mids, region, ttl=10, channel="web", occur=OCCUR):
            return c.post(
                "/api/v1/reservations",
                json={
                    "material_ids": list(mids),
                    "region": region,
                    "channel": channel,
                    "occur_at": occur,
                    "ttl_minutes": ttl,
                },
            )

        def auth(aid):
            return c.get(f"/api/v1/authorizations/{aid}").json()

        m1 = new_material("r1")
        m2 = new_material("r2")
        m3 = new_material("r3")

        print("== 创建预留：整体核验 + 逐项占额 + 返回字段 ==")
        a1 = new_auth(m1, 2, "CN")
        r = reserve([m1], "CN")
        check("reserve 201", r.status_code == 201, r.text)
        body = r.json()
        check("reserve id", isinstance(body["id"], int), r.text)
        check("reserve pending", body["status"] == "pending", r.text)
        check(
            "expires_at ~= now+10min",
            abs(
                (
                    datetime.fromisoformat(body["expires_at"])
                    .astimezone(timezone.utc)
                    - datetime.now(timezone.utc)
                    - timedelta(minutes=10)
                ).total_seconds()
            )
            < 60,
            body["expires_at"],
        )
        check(
            "reserve item auth selected",
            body["items"][0]["authorization"]["id"] == a1,
            r.text,
        )
        check(
            "item echoes material",
            body["items"][0]["material_id"] == m1
            and body["items"][0]["item_index"] == 0,
            r.text,
        )
        check("distribution_id none", body["distribution_id"] is None, r.text)
        g = auth(a1)
        check(
            "reserved_count=1 used=0 remaining=2 available=1",
            (g["reserved_count"], g["used_count"], g["remaining"], g["available"])
            == (1, 0, 2, 1),
            str(g),
        )

        print("== 未到期预留参与新申请与其他预留的余量判定 ==")
        # 正式直接申请还能用最后 1 次
        d = c.post(
            "/api/v1/distributions",
            json={
                "material_ids": [m1],
                "region": "CN",
                "channel": "web",
                "occur_at": OCCUR,
            },
        )
        check("direct apply uses last slot", d.status_code == 201, d.text)
        g = auth(a1)
        check(
            "used=1 reserved=1 available=0",
            (g["used_count"], g["reserved_count"], g["available"]) == (1, 1, 0),
            str(g),
        )
        # 再来一次直接申请：正式+预留已占满 → 拒绝
        d2 = c.post(
            "/api/v1/distributions",
            json={
                "material_ids": [m1],
                "region": "CN",
                "channel": "web",
                "occur_at": OCCUR,
            },
        )
        check(
            "direct apply blocked by reservation",
            d2.status_code == 422
            and d2.json()["error"]["details"][0]["reason"]
            == "quota_exhausted",
            d2.text,
        )
        # 第二个预留同样被未到期预留 + 正式占满而拒绝
        r2 = reserve([m1], "CN")
        check(
            "second reserve blocked",
            r2.status_code == 422
            and r2.json()["error"]["details"][0]["reason"]
            == "quota_exhausted",
            r2.text,
        )
        g = auth(a1)
        check(
            "rejected reserve holds nothing",
            (g["used_count"], g["reserved_count"]) == (1, 1),
            str(g),
        )

        print("== 确认：原子转一笔已通过申请 ==")
        cf = c.post(f"/api/v1/reservations/{body['id']}/confirm")
        check("confirm 200", cf.status_code == 200, cf.text)
        cb = cf.json()
        check("confirmed", cb["status"] == "confirmed", cb)
        check(
            "confirmed_at set", cb["confirmed_at"] is not None, str(cb)
        )
        check(
            "distribution attached",
            cb["distribution_id"] is not None
            and cb["distribution"] is not None
            and cb["distribution"]["id"] == cb["distribution_id"],
            str(cb),
        )
        dist = cb["distribution"]
        check("distribution approved", dist["status"] == "approved", str(dist))
        check(
            "distribution item auth",
            dist["items"][0]["authorization"]["id"] == a1,
            str(dist),
        )
        check(
            "distribution dimensions echo reservation",
            dist["region"] == "CN"
            and dist["channel"] == "web"
            and datetime.fromisoformat(dist["occur_at"]).astimezone(timezone.utc)
            == datetime.fromisoformat(OCCUR).astimezone(timezone.utc),
            str(dist),
        )
        g = auth(a1)
        check(
            "reserved->used transfer: used=2 reserved=0 available=0",
            (g["used_count"], g["reserved_count"], g["available"]) == (2, 0, 0),
            str(g),
        )
        # 同一笔申请也可在正式接口查询
        gd = c.get(f"/api/v1/distributions/{cb['distribution_id']}")
        check("distribution queryable", gd.status_code == 200, gd.text)

        print("== 重复确认返回同一申请（幂等，不重复占额） ==")
        cf2 = c.post(f"/api/v1/reservations/{body['id']}/confirm")
        check("repeat confirm 200", cf2.status_code == 200, cf2.text)
        check(
            "same distribution id",
            cf2.json()["distribution_id"] == cb["distribution_id"],
            cf2.text,
        )
        g = auth(a1)
        check(
            "no double consume",
            (g["used_count"], g["reserved_count"]) == (2, 0),
            str(g),
        )
        # 已确认不可取消（互斥）
        cc = c.post(f"/api/v1/reservations/{body['id']}/cancel")
        check(
            "cancel confirmed -> 409",
            cc.status_code == 409
            and cc.json()["error"]["code"] == "reservation_already_confirmed",
            cc.text,
        )
        # 确认产生的正式申请仍可撤销，撤销只释放正式占用一次
        rv = c.post(f"/api/v1/distributions/{cb['distribution_id']}/revoke")
        check("revoke confirmed distribution", rv.status_code == 200, rv.text)
        g = auth(a1)
        check(
            "revoke releases used only: used=1 reserved=0",
            (g["used_count"], g["reserved_count"]) == (1, 0),
            str(g),
        )

        print("== 取消：释放一次、互斥确认、重复取消 409 ==")
        r3 = reserve([m1], "CN", ttl=20)
        check("reserve for cancel 201", r3.status_code == 201, r3.text)
        rid3 = r3.json()["id"]
        check(
            "reserved before cancel", auth(a1)["reserved_count"] == 1
        )
        cc1 = c.post(f"/api/v1/reservations/{rid3}/cancel")
        check("cancel 200", cc1.status_code == 200, cc1.text)
        check(
            "status cancelled + cancelled_at",
            cc1.json()["status"] == "cancelled"
            and cc1.json()["cancelled_at"] is not None,
            cc1.text,
        )
        g = auth(a1)
        check(
            "reserved released after cancel",
            (g["reserved_count"], g["available"]) == (0, 1),
            str(g),
        )
        cc2 = c.post(f"/api/v1/reservations/{rid3}/cancel")
        check(
            "repeat cancel 409 no re-release",
            cc2.status_code == 409
            and cc2.json()["error"]["code"] == "reservation_already_cancelled",
            cc2.text,
        )
        check(
            "reserved still 0", auth(a1)["reserved_count"] == 0
        )
        cf3 = c.post(f"/api/v1/reservations/{rid3}/confirm")
        check(
            "confirm after cancel -> 409",
            cf3.status_code == 409
            and cf3.json()["error"]["code"] == "reservation_cancelled",
            cf3.text,
        )

        print("== 过期：到期即释放；到期时刻确认视为逾期 ==")
        r4 = reserve([m1], "CN", ttl=10)
        rid4 = r4.json()["id"]
        check("reserved holds", auth(a1)["reserved_count"] == 1)
        force_expire(rid4)
        # 授权查询触发惰性清理：到期即释放
        g = auth(a1)
        check(
            "expired released on auth read",
            (g["reserved_count"], g["available"]) == (0, 1),
            str(g),
        )
        gr = c.get(f"/api/v1/reservations/{rid4}")
        check(
            "reservation status expired",
            gr.status_code == 200 and gr.json()["status"] == "expired",
            gr.text,
        )
        # 过期后确认拒绝
        cf4 = c.post(f"/api/v1/reservations/{rid4}/confirm")
        check(
            "confirm expired -> 409 reservation_expired",
            cf4.status_code == 409
            and cf4.json()["error"]["code"] == "reservation_expired",
            cf4.text,
        )
        # 过期后取消幂等返回 expired，不再释放
        cc4 = c.post(f"/api/v1/reservations/{rid4}/cancel")
        check(
            "cancel expired idempotent 200 expired",
            cc4.status_code == 200 and cc4.json()["status"] == "expired",
            cc4.text,
        )
        check("no negative reserved", auth(a1)["reserved_count"] == 0)

        # 到期时刻边界：恰在到期时刻到达的确认必须判逾期（409 且已释放）
        r4b = reserve([m1], "CN", ttl=10)
        rid4b = r4b.json()["id"]
        db = SessionLocal()
        try:
            db.get(models.Reservation, rid4b).expires_at = datetime.now(
                timezone.utc
            )
            db.commit()
        finally:
            db.close()
        cfb = c.post(f"/api/v1/reservations/{rid4b}/confirm")
        check(
            "confirm exactly at expiry -> 409",
            cfb.status_code == 409
            and cfb.json()["error"]["code"] == "reservation_expired",
            cfb.text,
        )
        check(
            "boundary expiry released",
            auth(a1)["reserved_count"] == 0,
        )

        # 过期释放后额度可被新预留/新申请复用
        r4c = reserve([m1], "CN", ttl=10)
        check(
            "re-reserve after expiry release",
            r4c.status_code == 201,
            r4c.text,
        )
        rid4c = r4c.json()["id"]
        check(
            "re-reserved holds again", auth(a1)["reserved_count"] == 1
        )
        force_expire(rid4c)
        # 一次新的直接申请（写事务 sweep）也应触发释放并成功
        da = c.post(
            "/api/v1/distributions",
            json={
                "material_ids": [m1],
                "region": "CN",
                "channel": "web",
                "occur_at": OCCUR,
            },
        )
        check(
            "apply after expiry sweep 201",
            da.status_code == 201,
            da.text,
        )
        c.post(f"/api/v1/distributions/{da.json()['id']}/revoke")

        print("== 授权停用不影响已有预留的确认 ==")
        r5 = reserve([m1], "CN", ttl=10)
        rid5 = r5.json()["id"]
        c.patch(f"/api/v1/authorizations/{a1}", json={"status": "inactive"})
        cf5 = c.post(f"/api/v1/reservations/{rid5}/confirm")
        check(
            "confirm under inactive auth still 200",
            cf5.status_code == 200 and cf5.json()["status"] == "confirmed",
            cf5.text,
        )
        # 停用期间新预留必须被拒（authorization_inactive）
        r5b = reserve([m1], "CN", ttl=10)
        check(
            "new reserve while inactive rejected",
            r5b.status_code == 422
            and r5b.json()["error"]["details"][0]["reason"]
            == "authorization_inactive",
            r5b.text,
        )
        c.patch(f"/api/v1/authorizations/{a1}", json={"status": "active"})

        print("== 多素材整体原子：任一失败不占任何额度并列明原因 ==")
        # 独立通道 MX：m1/m2 各 1 容量；先占满 m2；m3 无任何授权。
        ax1 = new_auth(m1, 1, "MX")
        ax2 = new_auth(m2, 1, "MX")
        fill = c.post(
            "/api/v1/distributions",
            json={
                "material_ids": [m2],
                "region": "MX",
                "channel": "web",
                "occur_at": OCCUR,
            },
        )
        check("fill m2 quota", fill.status_code == 201, fill.text)
        before_ax1 = auth(ax1)["reserved_count"]
        before_ax2 = auth(ax2)["reserved_count"]
        rmix = reserve([m1, m2, m3], "MX", ttl=5)
        check("mixed reserve 422", rmix.status_code == 422, rmix.text)
        details = rmix.json()["error"]["details"]
        check(
            "code reservation_rejected",
            rmix.json()["error"]["code"] == "reservation_rejected",
            rmix.text,
        )
        # m1 余量足够（失败原因不在 m1）；m2 正式 1 笔已占满；m3 无授权
        reasons = {d["item_index"]: d["reason"] for d in details}
        check(
            "two failing items",
            len(details) == 2,
            str(details),
        )
        check("m2 quota_exhausted", reasons.get(1) == "quota_exhausted", str(details))
        check("m3 no match", reasons.get(2) == "no_matching_authorization", str(details))
        check(
            "nothing reserved on reject",
            auth(ax1)["reserved_count"] == before_ax1
            and auth(ax2)["reserved_count"] == before_ax2,
            f"{before_ax1}/{before_ax2}",
        )

        # 两素材同时通过：先释放 m2 的正式占用，再各自占额、逐项授权
        c.post(f"/api/v1/distributions/{fill.json()['id']}/revoke")
        rmix2 = reserve([m1, m2], "MX", ttl=5)
        check("two-material reserve 201", rmix2.status_code == 201, rmix2.text)
        b2 = rmix2.json()
        check(
            "both items present",
            {it["material_id"] for it in b2["items"]} == {m1, m2},
            rmix2.text,
        )
        check(
            "both authorizations charged",
            auth(ax1)["reserved_count"] == 1 and auth(ax2)["reserved_count"] == 1,
        )
        # 确认多素材预留：一笔申请含两项，两端各转一次
        cfm = c.post(f"/api/v1/reservations/{b2['id']}/confirm")
        check("confirm multi 200", cfm.status_code == 200, cfm.text)
        cfm_b = cfm.json()
        check(
            "one distribution with two items",
            len(cfm_b["distribution"]["items"]) == 2,
            cfm.text,
        )
        check(
            "multi confirm transfers reserved->used",
            auth(ax1)["reserved_count"] == 0 and auth(ax2)["reserved_count"] == 0,
        )

        print("== 重叠授权选择顺序沿用（到期早优先，其次 id 小） ==")
        early = new_auth(m1, 10, "OV")
        late = c.post(
            "/api/v1/authorizations",
            json={
                "material_id": m1,
                "region": "OV",
                "channel": "web",
                "starts_at": (NOW - timedelta(days=2)).isoformat(),
                "ends_at": (NOW + timedelta(days=2)).isoformat(),
                "max_count": 10,
            },
        ).json()["id"]
        rs = reserve([m1], "OV", ttl=5)
        chosen = rs.json()["items"][0]["authorization"]["id"]
        check(
            "reserve chooses earliest expiry",
            chosen == early,
            f"chosen={chosen} expected={early}",
        )

        print("== 查询四态与列表过滤 ==")
        # 至此状态：confirmed / cancelled / expired 都已出现；
        # 在独立通道再造一条 pending。
        new_auth(m1, 5, "LS")
        rp = reserve([m1], "LS", ttl=30)
        check("pending created", rp.status_code == 201, rp.text)
        for st in ("pending", "confirmed", "cancelled", "expired"):
            lst = c.get(f"/api/v1/reservations?status={st}")
            check(
                f"list filter {st}",
                lst.status_code == 200
                and all(x["status"] == st for x in lst.json()["items"])
                and lst.json()["count"] >= 1,
                lst.text,
            )
        alllst = c.get("/api/v1/reservations")
        total = alllst.json()["count"]
        summed = sum(
            c.get(f"/api/v1/reservations?status={st}").json()["count"]
            for st in ("pending", "confirmed", "cancelled", "expired")
        )
        check("list total = sum of statuses", total == summed, f"{total}/{summed}")
        # pending 列表查询也会清理刚到期的记录
        force_expire(rp.json()["id"])
        lstp = c.get("/api/v1/reservations?status=pending")
        check(
            "expired excluded from pending after list sweep",
            all(x["id"] != rp.json()["id"] for x in lstp.json()["items"]),
            lstp.text,
        )
        check(
            "it shows under expired",
            any(
                x["id"] == rp.json()["id"]
                for x in c.get("/api/v1/reservations?status=expired").json()[
                    "items"
                ]
            ),
        )

        print("== 改挂与预留互斥/协同 ==")
        # 源授权有待确认预留 → 整体拒绝，两端次数状态不变
        src = new_auth(m3, 5, "MG")
        for _ in range(2):
            c.post(
                "/api/v1/distributions",
                json={
                    "material_ids": [m3],
                    "region": "MG",
                    "channel": "web",
                    "occur_at": OCCUR,
                },
            )
        dst = c.post(
            "/api/v1/authorizations",
            json={
                "material_id": m3,
                "region": "MG",
                "channel": "web",
                "starts_at": (NOW - timedelta(days=2)).isoformat(),
                "ends_at": (NOW + timedelta(days=2)).isoformat(),
                "max_count": 10,
            },
        ).json()["id"]
        rmg = reserve([m3], "MG", ttl=20)
        check("reserve on migration source", rmg.status_code == 201, rmg.text)
        rmid = rmg.json()["id"]
        mg = c.post(
            "/api/v1/authorizations/migrate",
            json={
                "source_authorization_id": src,
                "replacement_authorization_id": dst,
            },
        )
        check(
            "migrate rejected with pending reservation",
            mg.status_code == 422
            and mg.json()["error"]["details"][0]["reason"]
            == "source_has_pending_reservations",
            mg.text,
        )
        check("source still active", auth(src)["status"] == "active")
        check(
            "source counts unchanged",
            (auth(src)["used_count"], auth(src)["reserved_count"]) == (2, 1),
        )
        # 取消预留后改挂放行；已确认的预留（正式申请）随明细一起迁移
        c.post(f"/api/v1/reservations/{rmid}/cancel")
        mg2 = c.post(
            "/api/v1/authorizations/migrate",
            json={
                "source_authorization_id": src,
                "replacement_authorization_id": dst,
            },
        )
        check("migrate after cancel 200", mg2.status_code == 200, mg2.text)
        check(
            "migrated 2 and source inactive",
            mg2.json()["migrated_count"] == 2
            and mg2.json()["source_authorization"]["status"] == "inactive",
            mg2.text,
        )

        # 替代授权自身预留占额必须计入迁入余量：目标 max=3，已有预留 2，
        # 待迁入 2 → 3-0-2=1 < 2 → insufficient_quota。
        # 预留按「到期早优先」会优先落到源授权，故先临时停用源授权，
        # 让两条预留锁定替代授权，再恢复源授权。
        mm1 = new_material("mm1")
        sa = new_auth(mm1, 5, "MQ")
        ta = c.post(
            "/api/v1/authorizations",
            json={
                "material_id": mm1,
                "region": "MQ",
                "channel": "web",
                "starts_at": (NOW - timedelta(days=2)).isoformat(),
                "ends_at": (NOW + timedelta(days=2)).isoformat(),
                "max_count": 3,
            },
        ).json()["id"]
        for _ in range(2):
            c.post(
                "/api/v1/distributions",
                json={
                    "material_ids": [mm1],
                    "region": "MQ",
                    "channel": "web",
                    "occur_at": OCCUR,
                },
            )
        c.patch(f"/api/v1/authorizations/{sa}", json={"status": "inactive"})
        for _ in range(2):
            rq = reserve([mm1], "MQ", ttl=20)
            check(
                "reserve pinned to target",
                rq.status_code == 201
                and rq.json()["items"][0]["authorization"]["id"] == ta,
                rq.text,
            )
        c.patch(f"/api/v1/authorizations/{sa}", json={"status": "active"})
        check(
            "target reserved_count=2",
            auth(ta)["reserved_count"] == 2
            and auth(sa)["reserved_count"] == 0,
        )
        mgq = c.post(
            "/api/v1/authorizations/migrate",
            json={
                "source_authorization_id": sa,
                "replacement_authorization_id": ta,
            },
        )
        check(
            "migrate insufficient due to target reservations",
            mgq.status_code == 422
            and mgq.json()["error"]["details"][0]["reason"]
            == "insufficient_quota",
            mgq.text,
        )
        # 预留过期释放后，改挂即可成功（验证迁移目标余量口径）
        lst = c.get("/api/v1/reservations?status=pending").json()["items"]
        for item in lst:
            if item["items"][0]["authorization"]["id"] == ta:
                force_expire(item["id"])
        sweep = c.get(f"/api/v1/authorizations/{ta}")
        check("target reservations expired", auth(ta)["reserved_count"] == 0, sweep.text)
        mgq2 = c.post(
            "/api/v1/authorizations/migrate",
            json={
                "source_authorization_id": sa,
                "replacement_authorization_id": ta,
            },
        )
        check(
            "migrate succeeds after target reservations release",
            mgq2.status_code == 200 and mgq2.json()["migrated_count"] == 2,
            mgq2.text,
        )

        print("== 请求校验与 404 ==")
        check(
            "ttl 31 rejected",
            reserve([m1], "CN", ttl=31).status_code == 422
        )
        check(
            "ttl 0 rejected (422)",
            reserve([m1], "CN", ttl=0).status_code == 422
        )
        check(
            "duplicate materials 422",
            reserve([m1, m1], "CN").json()["error"]["code"]
            == "duplicate_material_items",
        )
        check(
            "unknown material 422",
            reserve([999999], "CN").json()["error"]["code"]
            == "unknown_material",
        )
        check(
            "naive occur_at 422",
            c.post(
                "/api/v1/reservations",
                json={
                    "material_ids": [m1],
                    "region": "CN",
                    "channel": "web",
                    "occur_at": "2026-10-04T15:30:00",
                    "ttl_minutes": 5,
                },
            ).status_code
            == 422,
        )
        check(
            "no matching authorization",
            reserve([m1], "ZZ").json()["error"]["details"][0]["reason"]
            == "no_matching_authorization",
        )
        check("get unknown reservation 404", c.get("/api/v1/reservations/999999").status_code == 404)
        check(
            "confirm unknown 404",
            c.post("/api/v1/reservations/999999/confirm").status_code == 404,
        )
        check(
            "cancel unknown 404",
            c.post("/api/v1/reservations/999999/cancel").status_code == 404,
        )
        check(
            "bad status filter 422",
            c.get("/api/v1/reservations?status=nope").status_code == 422,
        )

    print(f"\n结果：{passed} 通过，{failed} 失败")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

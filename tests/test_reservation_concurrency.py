"""额度预留并发安全测试：并发提交/预留/确认/取消/撤销/改挂不得超额或重复释放。

全部场景基于真实 PostgreSQL，以数据库实际账（used_count/reserved_count
与各状态明细数）为准校验不变量：

- 任一授权恒有 used_count == 归属它的 approved 正式明细数；
- reserved_count == 归属它的 pending 预留明细数；
- 0 <= used_count + reserved_count <= max_count。

运行：
    DATABASE_URL=postgresql+psycopg://licensing@127.0.0.1:5439/licensing \
        python3 -m tests.test_reservation_concurrency
"""
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg://licensing@127.0.0.1:5439/licensing",
)

from fastapi.testclient import TestClient  # noqa: E402

from app.database import Base, SessionLocal, engine, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app import models  # noqa: E402

CST = timezone(timedelta(hours=8))
NOW = datetime(2026, 11, 1, 12, 0, tzinfo=CST)
OCCUR = NOW.isoformat()


def make_material(client, code):
    return client.post(
        "/api/v1/materials", json={"code": code, "name": code}
    ).json()["id"]


def make_auth(client, mid, region, count, channel="web"):
    return client.post(
        "/api/v1/authorizations",
        json={
            "material_id": mid,
            "region": region,
            "channel": channel,
            "starts_at": (NOW - timedelta(days=1)).isoformat(),
            "ends_at": (NOW + timedelta(days=1)).isoformat(),
            "max_count": count,
        },
    ).json()["id"]


def assert_books_balance(auth_ids, label=""):
    """账实相符：used/reserved 分别等于 approved 明细数 / pending 预留明细数。"""
    db = SessionLocal()
    try:
        for aid in auth_ids:
            auth = db.get(models.Authorization, aid)
            used = auth.used_count
            reserved = auth.reserved_count
            assert 0 <= used, f"{label} 授权{aid} used 负：{used}"
            assert 0 <= reserved, f"{label} 授权{aid} reserved 负：{reserved}"
            assert used + reserved <= auth.max_count, (
                f"{label} 授权{aid} 超额：{used}+{reserved}>{auth.max_count}"
            )
            approved_items = db.scalar(
                select(func.count())
                .select_from(models.DistributionItem)
                .join(
                    models.Distribution,
                    models.DistributionItem.distribution_id
                    == models.Distribution.id,
                )
                .where(
                    models.DistributionItem.authorization_id == aid,
                    models.Distribution.status == "approved",
                )
            )
            pending_items = db.scalar(
                select(func.count())
                .select_from(models.ReservationItem)
                .join(
                    models.Reservation,
                    models.ReservationItem.reservation_id
                    == models.Reservation.id,
                )
                .where(
                    models.ReservationItem.authorization_id == aid,
                    models.Reservation.status == "pending",
                )
            )
            assert used == approved_items, (
                f"{label} 授权{aid} used={used} 但 approved 明细="
                f"{approved_items}，账实不符"
            )
            assert reserved == pending_items, (
                f"{label} 授权{aid} reserved={reserved} 但 pending 预留明细="
                f"{pending_items}，账实不符"
            )
    finally:
        db.close()


def scenario_reserve_race() -> None:
    print("== 场景1：容量 5，25 个并发预留，恰好 5 个成功 ==")
    client = TestClient(app)
    mid = make_material(client, "rc1")
    aid = make_auth(client, mid, "RC1", 5)

    def one(i):
        c = TestClient(app)
        r = c.post(
            "/api/v1/reservations",
            json={
                "material_ids": [mid],
                "region": "RC1",
                "channel": "web",
                "occur_at": OCCUR,
                "ttl_minutes": 10,
            },
        )
        return r.status_code, r.json()

    with ThreadPoolExecutor(max_workers=25) as pool:
        results = [f.result() for f in as_completed([pool.submit(one, i) for i in range(25)])]

    ok = [r for code, r in results if code == 201]
    rej = [r for code, r in results if code == 422]
    assert len(ok) == 5, f"成功预留应为 5，实际 {len(ok)}"
    assert len(rej) == 20, f"拒绝应为 20，实际 {len(rej)}"
    assert all(
        r["error"]["details"][0]["reason"] == "quota_exhausted" for r in rej
    ), [r["error"]["details"][0]["reason"] for r in rej]
    body = client.get(f"/api/v1/authorizations/{aid}").json()
    assert body["reserved_count"] == 5 and body["used_count"] == 0, body
    assert_books_balance([aid], "scenario1")
    print("  PASS  并发预留 5 成功 / 20 拒绝，reserved=5（无超额）")


def scenario_mixed_apply_reserve_race() -> None:
    print("== 场景2：容量 10，15 直接申请 + 15 预留并发，正式+预留合计恰好 10 ==")
    client = TestClient(app)
    mid = make_material(client, "rc2")
    aid = make_auth(client, mid, "RC2", 10)

    def apply(i):
        c = TestClient(app)
        return c.post(
            "/api/v1/distributions",
            json={
                "material_ids": [mid],
                "region": "RC2",
                "channel": "web",
                "occur_at": OCCUR,
            },
        ).status_code

    def reserve(i):
        c = TestClient(app)
        return c.post(
            "/api/v1/reservations",
            json={
                "material_ids": [mid],
                "region": "RC2",
                "channel": "web",
                "occur_at": OCCUR,
                "ttl_minutes": 10,
            },
        ).status_code

    with ThreadPoolExecutor(max_workers=30) as pool:
        futs = [pool.submit(apply, i) for i in range(15)]
        futs += [pool.submit(reserve, i) for i in range(15)]
        codes = [f.result() for f in as_completed(futs)]

    body = client.get(f"/api/v1/authorizations/{aid}").json()
    total_held = body["used_count"] + body["reserved_count"]
    assert codes.count(201) == 10, f"成功合计应为 10，实际 {codes.count(201)}"
    assert total_held == 10, f"正式+预留应恰好 10，实际 {total_held}"
    assert_books_balance([aid], "scenario2")
    print(
        f"  PASS  成功 10（正式 {body['used_count']} + "
        f"预留 {body['reserved_count']}），20 个被拒，合计不超额"
    )


def scenario_confirm_race() -> None:
    print("== 场景3：同一预留并发确认 20 次，只生成一笔申请、只转一次占额 ==")
    client = TestClient(app)
    mid = make_material(client, "rc3")
    aid = make_auth(client, mid, "RC3", 5)
    rid = client.post(
        "/api/v1/reservations",
        json={
            "material_ids": [mid],
            "region": "RC3",
            "channel": "web",
            "occur_at": OCCUR,
            "ttl_minutes": 10,
        },
    ).json()["id"]

    def confirm(i):
        c = TestClient(app)
        r = c.post(f"/api/v1/reservations/{rid}/confirm")
        return r.status_code, r.json()

    with ThreadPoolExecutor(max_workers=20) as pool:
        outs = [f.result() for f in as_completed([pool.submit(confirm, i) for i in range(20)])]

    assert all(code == 200 for code, _ in outs), outs
    dist_ids = {b["distribution_id"] for _, b in outs}
    assert dist_ids == {outs[0][1]["distribution_id"]} and len(dist_ids) == 1, dist_ids
    body = client.get(f"/api/v1/authorizations/{aid}").json()
    assert (body["used_count"], body["reserved_count"]) == (1, 0), body
    assert_books_balance([aid], "scenario3")
    print("  PASS  20 次并发确认全部 200 且返回同一申请，used=1 reserved=0")


def scenario_confirm_cancel_race() -> None:
    print("== 场景4：同一预留并发确认与取消，恰一方生效且占额不重复释放 ==")
    client = TestClient(app)
    mid = make_material(client, "rc4")
    aid = make_auth(client, mid, "RC4", 60)
    outcomes = []
    for k in range(30):
        rid = client.post(
            "/api/v1/reservations",
            json={
                "material_ids": [mid],
                "region": "RC4",
                "channel": "web",
                "occur_at": OCCUR,
                "ttl_minutes": 10,
            },
        ).json()["id"]

        def confirm():
            c = TestClient(app)
            return c.post(f"/api/v1/reservations/{rid}/confirm").status_code

        def cancel():
            c = TestClient(app)
            return c.post(f"/api/v1/reservations/{rid}/cancel").status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            cf = pool.submit(confirm)
            xf = pool.submit(cancel)
            cc, xc = cf.result(), xf.result()
        # 互斥：成功(200)与拒绝(409) 各一个。
        assert sorted([cc, xc]) == [200, 409], f"cc={cc} xc={xc}"
        # 输家再查/再操作也不得造成二次变化。
        final = client.get(f"/api/v1/reservations/{rid}").json()
        assert final["status"] in ("confirmed", "cancelled"), final
        outcomes.append(final["status"])
        assert_books_balance([aid], f"scenario4 rid={rid}")

    body = client.get(f"/api/v1/authorizations/{aid}").json()
    # 30 条预留要么确认（used）要么取消（释放），无 pending 残留。
    assert body["reserved_count"] == 0, body
    assert body["used_count"] == outcomes.count("confirmed"), (
        body,
        outcomes.count("confirmed"),
    )
    assert body["used_count"] <= 60, body
    assert_books_balance([aid], "scenario4-final")
    print(
        f"  PASS  30 组确认/取消并发均恰一方生效"
        f"（确认 {outcomes.count('confirmed')} / 取消 {outcomes.count('cancelled')}），"
        "账实相符，额度未重复释放"
    )


def scenario_cancel_double_race() -> None:
    print("== 场景5：并发重复取消，额度只释放一次 ==")
    client = TestClient(app)
    mid = make_material(client, "rc5")
    aid = make_auth(client, mid, "RC5", 3)
    rid = client.post(
        "/api/v1/reservations",
        json={
            "material_ids": [mid],
            "region": "RC5",
            "channel": "web",
            "occur_at": OCCUR,
            "ttl_minutes": 10,
        },
    ).json()["id"]

    def cancel():
        c = TestClient(app)
        return c.post(f"/api/v1/reservations/{rid}/cancel").status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        codes = list(pool.map(lambda _: cancel(), range(2)))
    assert sorted(codes) == [200, 409], codes
    body = client.get(f"/api/v1/authorizations/{aid}").json()
    assert body["reserved_count"] == 0 and body["used_count"] == 0, body
    assert_books_balance([aid], "scenario5")
    print("  PASS  并发双取消恰一次 200 一次 409，额度仅释放一次")


def scenario_confirm_then_revoke_race() -> None:
    print("== 场景6：确认转申请与该申请撤销并发，最终账实相符 ==")
    client = TestClient(app)
    mid = make_material(client, "rc6")
    aid = make_auth(client, mid, "RC6", 10)
    rids = []
    for _ in range(10):
        rids.append(
            client.post(
                "/api/v1/reservations",
                json={
                    "material_ids": [mid],
                    "region": "RC6",
                    "channel": "web",
                    "occur_at": OCCUR,
                    "ttl_minutes": 10,
                },
            ).json()["id"]
        )

    def confirm(rid):
        c = TestClient(app)
        r = c.post(f"/api/v1/reservations/{rid}/confirm")
        return r.status_code, (r.json()["distribution_id"] if r.status_code == 200 else None)

    # 先全部确认，再对其正式申请并发撤销（确认与撤销也可交错，这里串行确认、
    # 并发撤销以覆盖「预留转来的申请撤销只释放 used 一次」）。
    with ThreadPoolExecutor(max_workers=10) as pool:
        conf = [f.result() for f in as_completed([pool.submit(confirm, rid) for rid in rids])]
    assert all(code == 200 for code, _ in conf), conf
    dist_ids = [did for _, did in conf]

    def revoke(did):
        c = TestClient(app)
        return c.post(f"/api/v1/distributions/{did}/revoke").status_code

    with ThreadPoolExecutor(max_workers=10) as pool:
        rcodes = [f.result() for f in as_completed([pool.submit(revoke, did) for did in dist_ids])]
    assert rcodes.count(200) == 10, rcodes
    body = client.get(f"/api/v1/authorizations/{aid}").json()
    assert (body["used_count"], body["reserved_count"]) == (0, 0), body
    assert_books_balance([aid], "scenario6")
    print("  PASS  10 预留全部确认后并发撤销 10 次成功，used/reserved 归零")


def scenario_migrate_with_pending_race() -> None:
    print("== 场景7：改挂与预留并发：有待确认预留时改挂绝不搬移其占额 ==")
    client = TestClient(app)
    mid = make_material(client, "rc7")
    src = make_auth(client, mid, "RC7", 20)
    dst = client.post(
        "/api/v1/authorizations",
        json={
            "material_id": mid,
            "region": "RC7",
            "channel": "web",
            "starts_at": (NOW - timedelta(days=2)).isoformat(),
            "ends_at": (NOW + timedelta(days=2)).isoformat(),
            "max_count": 50,
        },
    ).json()["id"]
    for _ in range(10):
        assert (
            client.post(
                "/api/v1/distributions",
                json={
                    "material_ids": [mid],
                    "region": "RC7",
                    "channel": "web",
                    "occur_at": OCCUR,
                },
            ).status_code
            == 201
        )

    def migrate():
        c = TestClient(app)
        r = c.post(
            "/api/v1/authorizations/migrate",
            json={
                "source_authorization_id": src,
                "replacement_authorization_id": dst,
            },
        )
        return r.status_code, r.json()

    def reserve(i):
        c = TestClient(app)
        r = c.post(
            "/api/v1/reservations",
            json={
                "material_ids": [mid],
                "region": "RC7",
                "channel": "web",
                "occur_at": OCCUR,
                "ttl_minutes": 10,
            },
        )
        return r.status_code, r.json()

    with ThreadPoolExecutor(max_workers=16) as pool:
        mf = pool.submit(migrate)
        rf = [pool.submit(reserve, i) for i in range(15)]
        mcode, mbody = mf.result()
        routs = [f.result() for f in rf]

    # 改挂成功时迁移条数只能是 10（预留占额绝不被搬走）；
    # 若有预留抢先落到源授权（reserved>0），改挂必须整体拒绝。
    db = SessionLocal()
    try:
        s = db.get(models.Authorization, src)
        t = db.get(models.Authorization, dst)
        src_status = s.status
        s_used, s_res = s.used_count, s.reserved_count
        t_used, t_res = t.used_count, t.reserved_count
    finally:
        db.close()

    if mcode == 200:
        assert mbody["migrated_count"] == 10, mbody
        assert src_status == "inactive"
        # 改挂成功意味着源上无 pending 预留：所有预留要么落到目标（源停用后
        # 新预留仍可匹配目标），要么被拒；源 reserved 必为 0。
        assert s_res == 0, (s_used, s_res)
        assert s_used == 0
        assert t_used == 10, (t_used, t_res)
    else:
        assert (
            mbody["error"]["details"][0]["reason"]
            == "source_has_pending_reservations"
        ), mbody
        assert src_status == "active"
        assert s_used == 10
    assert_books_balance([src, dst], "scenario7")
    print(
        f"  PASS  改挂={mcode}（迁移 {mbody.get('migrated_count')}），"
        f"源 used={s_used}/reserved={s_res}，目标 used={t_used}/reserved={t_res}，"
        "预留占额未被搬走、账实相符"
    )


def scenario_multi_material_reserve_race() -> None:
    print("== 场景8：3 素材批量预留各 4 容量，30 并发，恰好 4 个整体成功 ==")
    client = TestClient(app)
    ids = []
    for code in ("r8a", "r8b", "r8c"):
        mid = make_material(client, code)
        aid = make_auth(client, mid, "RC8", 4, channel="app")
        ids.append((mid, aid))

    def one(i):
        c = TestClient(app)
        return c.post(
            "/api/v1/reservations",
            json={
                "material_ids": [mid for mid, _ in ids],
                "region": "RC8",
                "channel": "app",
                "occur_at": OCCUR,
                "ttl_minutes": 10,
            },
        ).status_code

    with ThreadPoolExecutor(max_workers=15) as pool:
        codes = list(pool.map(one, range(30)))
    assert codes.count(201) == 4, codes.count(201)
    assert codes.count(422) == 26, codes.count(422)
    for mid, aid in ids:
        body = client.get(f"/api/v1/authorizations/{aid}").json()
        assert body["reserved_count"] == 4 and body["used_count"] == 0, body
    assert_books_balance([aid for _, aid in ids], "scenario8")
    print("  PASS  批量预留恰好 4 整体成功，三授权 reserved 各=4")


def scenario_expiry_releases_under_load() -> None:
    print("== 场景9：预留到期后并发确认/取消，恰为一次释放且状态一致 ==")
    client = TestClient(app)
    mid = make_material(client, "rc9")
    aid = make_auth(client, mid, "RC9", 10)
    # 先占满正式+预留：10 个预留全部置过期。
    rids = []
    for _ in range(10):
        rids.append(
            client.post(
                "/api/v1/reservations",
                json={
                    "material_ids": [mid],
                    "region": "RC9",
                    "channel": "web",
                    "occur_at": OCCUR,
                    "ttl_minutes": 10,
                },
            ).json()["id"]
        )
    db = SessionLocal()
    try:
        for rid in rids:
            db.get(models.Reservation, rid).expires_at = datetime.now(
                timezone.utc
            ) - timedelta(seconds=1)
        db.commit()
    finally:
        db.close()

    def action(rid, kind):
        c = TestClient(app)
        if kind == "confirm":
            return c.post(f"/api/v1/reservations/{rid}/confirm").status_code
        return c.post(f"/api/v1/reservations/{rid}/cancel").status_code

    with ThreadPoolExecutor(max_workers=20) as pool:
        futs = []
        for i, rid in enumerate(rids):
            futs.append(pool.submit(action, rid, "confirm"))
            futs.append(pool.submit(action, rid, "cancel"))
        codes = [f.result() for f in as_completed(futs)]

    # 过期后：确认全部 409；取消为 200（幂等 expired）或 409（重复），
    # 但绝不能出现 200 confirmed，也绝不能把 reserved 减成负数。
    assert 201 not in codes and 200 in codes, codes
    body = client.get(f"/api/v1/authorizations/{aid}").json()
    assert (body["reserved_count"], body["used_count"]) == (0, 0), body
    for rid in rids:
        st = client.get(f"/api/v1/reservations/{rid}").json()["status"]
        assert st == "expired", (rid, st)
    assert_books_balance([aid], "scenario9")

    # 释放出的 10 个名额立即可用。
    def apply(i):
        c = TestClient(app)
        return c.post(
            "/api/v1/distributions",
            json={
                "material_ids": [mid],
                "region": "RC9",
                "channel": "web",
                "occur_at": OCCUR,
            },
        ).status_code

    with ThreadPoolExecutor(max_workers=15) as pool:
        ac = [f.result() for f in as_completed([pool.submit(apply, i) for i in range(15)])]
    assert ac.count(201) == 10 and ac.count(422) == 5, ac
    assert_books_balance([aid], "scenario9-reuse")
    print("  PASS  过期并发确认/取消不重复释放，释放后 10 名额可复用，账实相符")


def main() -> int:
    Base.metadata.drop_all(bind=engine)
    init_db()
    with TestClient(app):
        scenario_reserve_race()
        scenario_mixed_apply_reserve_race()
        scenario_confirm_race()
        scenario_confirm_cancel_race()
        scenario_cancel_double_race()
        scenario_confirm_then_revoke_race()
        scenario_migrate_with_pending_race()
        scenario_multi_material_reserve_race()
        scenario_expiry_releases_under_load()
    print("\n全部预留并发场景通过：未超额、未重复释放、账实相符。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

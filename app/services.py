"""核心业务逻辑：登记 / 核验占用 / 查询 / 撤销 / 授权退役改挂 / 额度预留。

并发安全
========
所有写事务遵守统一的全局加锁顺序，杜绝跨事务死锁：

    **通道咨询锁（region+channel）→ 申请/预留行（id 升序）
    → 授权行（id 升序）→ 明细行（id 升序）**

额度模型
--------
授权 ``max_count`` 同时承载两类占用：

- ``used_count``：已通过（未撤销）正式申请的逐项占用，**字段语义与旧版
  完全一致**；``remaining = max_count - used_count`` 也保持旧语义；
- ``reserved_count``：未到期、待确认预留（pending）的逐项占额；
- 可预留余量 ``available = max_count - used_count - reserved_count``。

未到期预留参与新申请及其他预留的余量判定：新的直接发行申请与新预留
都要求 ``used_count + reserved_count < max_count``。预留确认时占额由
``reserved_count`` 原子转入 ``used_count``（净占用不变）；取消或过期
则释放 ``reserved_count``。

到期释放
--------
过期状态采用惰性清理（sweep）：每个通道写事务拿到咨询锁后的第一步，
以及授权查询/预留查询/列表查询时，都会把该通道下
``expires_at <= now`` 的 pending 预留原子置为 expired 并逐项释放
``reserved_count``。因此「到期即释放」，且到期时刻的确认必在 sweep
之后被判定为逾期。

重叠授权选择沿用正式申请的既有顺序：到期时间 ``ends_at`` 升序、
相同再按授权 id 升序，跳过停用/无余量者。
"""
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from zlib import crc32

from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app import models
from app.config import settings
from app.errors import (
    AlreadyRevoked,
    DistributionRejected,
    DuplicateMaterialItems,
    MigrationRejected,
    MaterialCodeTaken,
    ReservationAlreadyCancelled,
    ReservationAlreadyConfirmed,
    ReservationCancelled,
    ReservationExpired,
    ReservationRejected,
    ResourceNotFound,
    UnknownMaterial,
)
from app.schemas import (
    AuthorizationCreate,
    AuthorizationMigrationRequest,
    DistributionCreate,
    MaterialCreate,
    ReservationCreate,
)


# ---------------------------------------------------------------- 登记素材
def create_material(db: Session, data: MaterialCreate) -> models.Material:
    material = models.Material(code=data.code, name=data.name)
    db.add(material)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise MaterialCodeTaken(data.code)
    db.commit()
    db.refresh(material)
    return material


def get_material(db: Session, material_id: int) -> models.Material:
    material = db.get(models.Material, material_id)
    if material is None:
        raise ResourceNotFound("素材", material_id)
    return material


# ------------------------------------------------------------- 登记授权
def create_authorization(
    db: Session, data: AuthorizationCreate
) -> models.Authorization:
    if db.get(models.Material, data.material_id) is None:
        raise UnknownMaterial([data.material_id])

    auth = models.Authorization(
        material_id=data.material_id,
        region=data.region,
        channel=data.channel,
        starts_at=data.starts_at,
        ends_at=data.ends_at,
        max_count=data.max_count,
    )
    db.add(auth)
    db.commit()
    db.refresh(auth)
    return auth


def get_authorization(db: Session, authorization_id: int) -> models.Authorization:
    auth = db.get(models.Authorization, authorization_id)
    if auth is None:
        raise ResourceNotFound("授权", authorization_id)
    # 查询前先清理该授权所在通道的过期预留，保证 used/reserved/available
    # 反映「到期即释放」后的最新余量（独立只读事务，不影响读已提交快照）。
    sweep_bucket(db, auth.region, auth.channel)
    db.commit()
    db.refresh(auth)
    return auth


def set_authorization_status(
    db: Session, authorization_id: int, status: str
) -> models.Authorization:
    auth = db.get(models.Authorization, authorization_id)
    if auth is None:
        raise ResourceNotFound("授权", authorization_id)
    auth.status = status
    db.commit()
    db.refresh(auth)
    return auth


# ------------------------------------------------------- 通道锁与过期清理
def _bucket_lock_id(region: str, channel: str) -> int:
    """region+channel → 单个有符号 32 位整数（pg_advisory_xact_lock 参数）。"""
    raw = crc32(f"{region}\x1f{channel}".encode("utf-8"))
    return raw - 0x100000000 if raw >= 0x80000000 else raw


def _acquire_bucket_lock(db: Session, region: str, channel: str) -> None:
    """第一加锁点：同 region+channel 的全部写事务由此串行判定。"""
    db.execute(
        text("SELECT pg_advisory_xact_lock(:lk)"),
        {"lk": _bucket_lock_id(region, channel)},
    )


def _release_reservations(
    db: Session, rows: list[models.Reservation]
) -> dict[int, int]:
    """释放给定预留行（调用方须持通道锁且已锁定这些预留行）。

    逐项把对应授权的 ``reserved_count`` 条件减 1（每个授权在单个预留中
    至多出现一次，素材已去重），返回 ``{authorization_id: 释放条数}``。
    """
    released: dict[int, int] = defaultdict(int)
    for resv in rows:
        for item in resv.items:
            result = db.execute(
                update(models.Authorization)
                .where(
                    models.Authorization.id == item.authorization_id,
                    models.Authorization.reserved_count >= 1,
                )
                .values(
                    reserved_count=models.Authorization.reserved_count - 1
                )
            )
            if result.rowcount != 1:
                db.rollback()
                raise RuntimeError(
                    f"预留释放失败：授权 {item.authorization_id} "
                    "reserved_count 异常"
                )
            released[item.authorization_id] += 1
    return released


def sweep_bucket(
    db: Session, region: str, channel: str, now: datetime | None = None
) -> int:
    """把某通道下所有到期 pending 预留原子置为 expired 并释放占额。

    必须在持有该通道咨询锁时调用（写事务），或在仅含本清理操作的
    独立事务中先取锁再调用（读接口）。到期语义：``expires_at <= now``。
    """
    now = now or datetime.now(timezone.utc)
    expired_rows = list(
        db.scalars(
            select(models.Reservation)
            .where(
                models.Reservation.region == region,
                models.Reservation.channel == channel,
                models.Reservation.status == "pending",
                models.Reservation.expires_at <= now,
            )
            .order_by(models.Reservation.id.asc())
            .with_for_update()
            .options(
                selectinload(models.Reservation.items).selectinload(
                    models.ReservationItem.authorization
                )
            )
        )
    )
    if not expired_rows:
        return 0

    # 预留行已按 id 升序锁定；其明细所涉授权统一按 id 升序加行锁，
    # 遵守全局「授权行 id 升序」加锁顺序。
    auth_ids = sorted(
        {
            item.authorization_id
            for resv in expired_rows
            for item in resv.items
        }
    )
    db.execute(
        select(models.Authorization.id)
        .where(models.Authorization.id.in_(auth_ids))
        .order_by(models.Authorization.id.asc())
        .with_for_update()
    )

    _release_reservations(db, expired_rows)
    for resv in expired_rows:
        resv.status = "expired"
    return len(expired_rows)


def sweep_bucket_in_txn(
    db: Session, region: str, channel: str
) -> int:
    """读接口使用：独立事务内取通道锁 → 清理过期预留 → 提交。"""
    _acquire_bucket_lock(db, region, channel)
    n = sweep_bucket(db, region, channel)
    db.commit()
    return n


# ------------------------------------------------- 共用：素材与授权核验
def _validate_material_ids(
    db: Session, material_ids: list[int]
) -> None:
    """重复素材 / 未知编号校验，不通过直接抛错（不占任何额度）。"""
    counts = Counter(material_ids)
    duplicated = sorted(mid for mid, n in counts.items() if n > 1)
    if duplicated:
        raise DuplicateMaterialItems(duplicated)

    found_ids = set(
        db.scalars(
            select(models.Material.id).where(
                models.Material.id.in_(material_ids)
            )
        )
    )
    unknown = sorted(mid for mid in material_ids if mid not in found_ids)
    if unknown:
        raise UnknownMaterial(unknown)


def _lock_candidates(
    db: Session,
    material_ids: list[int],
    region: str,
    channel: str,
    occur: datetime,
) -> list[models.Authorization]:
    """按授权 id 升序锁定某通道/时刻下全部候选授权（含停用/无余量者）。"""
    return list(
        db.scalars(
            select(models.Authorization)
            .where(
                models.Authorization.region == region,
                models.Authorization.channel == channel,
                # 左闭右开：[starts_at, ends_at)
                models.Authorization.starts_at <= occur,
                models.Authorization.ends_at > occur,
                models.Authorization.material_id.in_(material_ids),
            )
            .order_by(models.Authorization.id.asc())
            .with_for_update()
        )
    )


def _choose_authorizations(
    material_ids: list[int],
    locked_rows: list[models.Authorization],
) -> tuple[dict[int, models.Authorization], list[dict]]:
    """逐项按重叠选择顺序挑授权；任一不满足收集原因。

    余量判定统一为 ``used_count + reserved_count < max_count``：
    未到期预留对新申请与新预留都占额，两者口径一致。
    """
    grouped: dict[int, list[models.Authorization]] = {
        mid: [] for mid in material_ids
    }
    for auth in locked_rows:
        grouped[auth.material_id].append(auth)

    reasons: list[dict] = []
    chosen: dict[int, models.Authorization] = {}
    for index, mid in enumerate(material_ids):
        candidates = sorted(
            grouped[mid],
            key=lambda a: (a.ends_at, a.id),
        )
        if not candidates:
            reasons.append(
                {
                    "item_index": index,
                    "material_id": mid,
                    "reason": "no_matching_authorization",
                    "message": "不存在同时匹配地区、渠道与发行时刻的授权",
                }
            )
            continue

        active = [a for a in candidates if a.status == "active"]
        if not active:
            reasons.append(
                {
                    "item_index": index,
                    "material_id": mid,
                    "reason": "authorization_inactive",
                    "message": "匹配的授权均已停用（停用仅影响新申请与新预留）",
                }
            )
            continue

        # 候选已按 (ends_at, id) 升序，取第一个「正式占用 + 未到期预留
        # 占用」合计未达总额度的授权。
        with_quota = next(
            (
                a
                for a in active
                if a.used_count + a.reserved_count < a.max_count
            ),
            None,
        )
        if with_quota is None:
            detail = active[0]
            reasons.append(
                {
                    "item_index": index,
                    "material_id": mid,
                    "reason": "quota_exhausted",
                    "message": (
                        f"匹配授权（id={detail.id}）可发行次数已用尽："
                        f"正式 {detail.used_count}/"
                        f"预留 {detail.reserved_count}/"
                        f"总额 {detail.max_count}"
                    ),
                }
            )
            continue
        chosen[mid] = with_quota

    return chosen, reasons


# ------------------------------------------------------- 发行核验与占用
def create_distribution(
    db: Session, data: DistributionCreate
) -> models.Distribution:
    # 1) 重复素材 / 未知编号（与旧版一致，先整体拒绝）。
    material_ids = list(data.material_ids)
    _validate_material_ids(db, material_ids)

    try:
        occur = data.occur_at
        # 2) 通道咨询锁（所有写事务的第一加锁点）。
        _acquire_bucket_lock(db, data.region, data.channel)

        # 3) 先释放本通道到期预留：到期即释放，释放后额度可被本申请使用。
        sweep_bucket(db, data.region, data.channel)

        # 4) 按授权 id 升序锁定候选行，重叠选择在 Python 中完成。
        locked_rows = _lock_candidates(
            db, material_ids, data.region, data.channel, occur
        )

        # 5) 逐项判定（未到期预留占额参与余量判定），任一失败整体回滚。
        chosen, reasons = _choose_authorizations(
            material_ids, locked_rows
        )
        if reasons:
            db.rollback()
            raise DistributionRejected(reasons)

        # 6) 全部通过：条件更新占用正式次数（防超额的最后防线，
        #    条件同时计入 reserved_count）。
        for auth in chosen.values():
            result = db.execute(
                update(models.Authorization)
                .where(
                    models.Authorization.id == auth.id,
                    models.Authorization.used_count
                    + models.Authorization.reserved_count
                    < models.Authorization.max_count,
                )
                .values(used_count=models.Authorization.used_count + 1)
            )
            if result.rowcount != 1:
                db.rollback()
                raise DistributionRejected(
                    [
                        {
                            "material_id": auth.material_id,
                            "reason": "quota_exhausted",
                            "message": (
                                f"授权 id={auth.id} 余量不足，"
                                "并发占用冲突，本次申请整体拒绝"
                            ),
                        }
                    ]
                )

        # 7) 落库申请及逐项占用记录。
        distribution = models.Distribution(
            region=data.region,
            channel=data.channel,
            occur_at=data.occur_at,
            status="approved",
        )
        db.add(distribution)
        db.flush()
        for index, mid in enumerate(material_ids):
            db.add(
                models.DistributionItem(
                    distribution_id=distribution.id,
                    material_id=mid,
                    authorization_id=chosen[mid].id,
                    item_index=index,
                )
            )
        db.commit()
    except DistributionRejected:
        raise
    except Exception:
        db.rollback()
        raise

    return get_distribution(db, distribution_id=distribution.id)


# ------------------------------------------------------------- 查询/撤销
def _load_distribution(
    db: Session, distribution_id: int
) -> models.Distribution:
    return db.scalar(
        select(models.Distribution)
        .where(models.Distribution.id == distribution_id)
        .options(
            selectinload(models.Distribution.items).selectinload(
                models.DistributionItem.material
            ),
            selectinload(models.Distribution.items).selectinload(
                models.DistributionItem.authorization
            ),
        )
    )


def get_distribution(db: Session, distribution_id: int) -> models.Distribution:
    distribution = _load_distribution(db, distribution_id)
    if distribution is None:
        raise ResourceNotFound("发行申请", distribution_id)
    return distribution


def revoke_distribution(
    db: Session, distribution_id: int
) -> models.Distribution:
    try:
        # 先读取申请的通道维度（不加锁），以便第一步即获取通道咨询锁。
        head = db.execute(
            select(
                models.Distribution.region,
                models.Distribution.channel,
                models.Distribution.status,
            ).where(models.Distribution.id == distribution_id)
        ).first()
        if head is None:
            raise ResourceNotFound("发行申请", distribution_id)
        region, channel, _status = head

        # 第一加锁点：该申请所在通道的咨询锁。与进行中的提交/确认/取消/
        # 改挂事务互斥，杜绝跨事务死锁（所有写事务一律先取咨询锁）。
        _acquire_bucket_lock(db, region, channel)

        # 撤销正式申请不依赖预留状态，但顺手清理本通道到期预留，
        # 使释放后的余额判定始终基于最新账（sweep 不影响本申请归属）。
        sweep_bucket(db, region, channel)

        # 锁定申请行，串行化对同一申请的并发撤销并复查状态。
        distribution = db.scalar(
            select(models.Distribution)
            .where(models.Distribution.id == distribution_id)
            .with_for_update()
        )
        if distribution.status == "revoked":
            raise AlreadyRevoked(distribution_id)

        # 锁定本申请的全部占用明细行：与改挂事务的明细归属更新互斥，
        # 固定「明细 → 授权」的当前归属。
        items = list(
            db.scalars(
                select(models.DistributionItem)
                .where(
                    models.DistributionItem.distribution_id
                    == distribution_id
                )
                .order_by(models.DistributionItem.id.asc())
                .with_for_update()
            )
        )
        auth_ids = sorted({item.authorization_id for item in items})

        # 按授权 id 升序加锁，与核验/改挂/预留事务保持同一全局行锁顺序。
        if auth_ids:
            db.execute(
                select(models.Authorization.id)
                .where(models.Authorization.id.in_(auth_ids))
                .order_by(models.Authorization.id.asc())
                .with_for_update()
            )

        # 条件 ``used_count >= 1`` 作为不重复释放/账实相符的最后防线。
        for auth_id in auth_ids:
            result = db.execute(
                update(models.Authorization)
                .where(
                    models.Authorization.id == auth_id,
                    models.Authorization.used_count >= 1,
                )
                .values(
                    used_count=models.Authorization.used_count - 1
                )
            )
            if result.rowcount != 1:
                db.rollback()
                raise RuntimeError(
                    f"撤销释放失败：授权 {auth_id} used_count 异常"
                )

        distribution.status = "revoked"
        distribution.revoked_at = datetime.now(timezone.utc)
        db.commit()
    except (ResourceNotFound, AlreadyRevoked, MigrationRejected):
        raise
    except Exception:
        db.rollback()
        raise

    return get_distribution(db, distribution_id=distribution_id)


# --------------------------------------------------------- 授权退役改挂
def migrate_authorization(
    db: Session, data: AuthorizationMigrationRequest
) -> dict:
    """原授权退役：把其承载的全部 **未撤销** 申请明细一次性改挂到替代授权。

    前置条件（任一不满足则整体拒绝，申请归属与两端次数均不变）：

    - 两端编号不同且授权均存在；
    - 素材、地区、渠道三个维度完全一致；
    - 替代授权为启用状态（active）；
    - 替代授权时段左闭右开覆盖每条待迁移申请的 ``occur_at``；
    - 替代授权余量（扣减其自身未到期预留后）足够承接全部迁入占用；
    - **原授权存在未到期（pending）预留时整体拒绝**：预留逐项锁定了
      原授权额度，退役改挂必须等待这些预留被确认/取消/过期后再进行。

    成功后：原授权停用（inactive）、占用次数按迁移数迁出（其
    reserved_count 必为 0）；替代授权按迁移数增加 used_count；
    改挂明细的 ``authorization_id`` 指向替代授权。
    """
    source_id = data.source_authorization_id
    target_id = data.replacement_authorization_id

    if source_id == target_id:
        raise MigrationRejected(
            [
                {
                    "reason": "same_authorization",
                    "message": "原授权与替代授权必须为不同编号",
                    "source_authorization_id": source_id,
                    "replacement_authorization_id": target_id,
                }
            ]
        )

    def _reject(reason: str, message: str) -> MigrationRejected:
        return MigrationRejected(
            [
                {
                    "reason": reason,
                    "message": message,
                    "source_authorization_id": source_id,
                    "replacement_authorization_id": target_id,
                }
            ]
        )

    # 不加锁读取，仅用于存在性/维度判断与确定咨询锁桶。
    source = db.get(models.Authorization, source_id)
    if source is None:
        raise ResourceNotFound("授权", source_id)
    target = db.get(models.Authorization, target_id)
    if target is None:
        raise ResourceNotFound("授权", target_id)

    try:
        # 1) 维度不一致直接拒绝，不触碰任何锁。
        if source.region != target.region or source.channel != target.channel:
            raise _reject(
                "dimension_mismatch",
                "原授权与替代授权的地区、渠道不一致，不可改挂",
            )
        if source.material_id != target.material_id:
            raise _reject(
                "dimension_mismatch",
                "原授权与替代授权的素材不一致，不可改挂",
            )

        _acquire_bucket_lock(db, source.region, source.channel)

        # 先清理到期预留（释放两端可能的过期占额），再判定 pending 预留。
        sweep_bucket(db, source.region, source.channel)

        # 2) 按 id 升序锁定两端授权行（统一全局行锁顺序，避免死锁）。
        first_id, second_id = sorted((source_id, target_id))
        db.execute(
            select(models.Authorization.id)
            .where(models.Authorization.id.in_([first_id, second_id]))
            .order_by(models.Authorization.id.asc())
            .with_for_update()
        )
        db.refresh(source)
        db.refresh(target)

        # 3) 原授权存在未到期待确认预留时整体拒绝（sweep 之后
        #    reserved_count>0 即确为 pending 预留占额）。
        if source.reserved_count > 0:
            raise _reject(
                "source_has_pending_reservations",
                f"原授权（id={source_id}）存在未到期待确认预留 "
                f"（{source.reserved_count} 项占额），"
                "须等待其确认/取消/过期后方可改挂",
            )

        # 4) 替代授权必须启用。
        if target.status != "active":
            raise _reject(
                "replacement_inactive",
                f"替代授权（id={target_id}）未处于启用状态",
            )

        # 5) 锁定原授权承载的全部未撤销申请的申请行（id 升序）。
        dist_rows = list(
            db.scalars(
                select(models.Distribution)
                .join(
                    models.DistributionItem,
                    models.DistributionItem.distribution_id
                    == models.Distribution.id,
                )
                .where(
                    models.DistributionItem.authorization_id == source_id,
                    models.Distribution.status == "approved",
                )
                .order_by(models.Distribution.id.asc())
                .with_for_update()
            )
        )
        dist_ids = [d.id for d in dist_rows]

        items: list[models.DistributionItem] = []
        occur_times: list[datetime] = []
        if dist_ids:
            items = list(
                db.scalars(
                    select(models.DistributionItem)
                    .where(
                        models.DistributionItem.distribution_id.in_(dist_ids),
                        models.DistributionItem.authorization_id
                        == source_id,
                    )
                    .order_by(models.DistributionItem.id.asc())
                    .with_for_update()
                )
            )
            occur_map = {d.id: d.occur_at for d in dist_rows}
            occur_times = [occur_map[it.distribution_id] for it in items]

        n = len(items)

        # 6) 逐条校验替代授权时段覆盖发行时刻（左闭右开）。
        uncovered = sorted(
            {
                it.id
                for it, occur_at in zip(items, occur_times)
                if not (target.starts_at <= occur_at < target.ends_at)
            }
        )
        if uncovered:
            raise _reject(
                "period_not_covered",
                f"替代授权（id={target_id}）时段未覆盖 {len(uncovered)} 条"
                "申请的发行时刻（需满足 starts_at <= occur_at < ends_at）",
            )

        # 7) 余量必须足够一次性承接全部迁入占用；替代授权自身的未到期
        #    预留同样占额，必须计入。
        if target.max_count - target.used_count - target.reserved_count < n:
            raise _reject(
                "insufficient_quota",
                f"替代授权（id={target_id}）余量不足：待迁入 {n} 条，"
                f"正式占用 {target.used_count}、预留占用 "
                f"{target.reserved_count}、总额 {target.max_count}",
            )

        # 8) 一次性搬移明细归属与次数（条件更新作为防超额最后防线）。
        if n:
            moved = db.execute(
                update(models.DistributionItem)
                .where(
                    models.DistributionItem.id.in_([it.id for it in items]),
                    models.DistributionItem.authorization_id == source_id,
                )
                .values(authorization_id=target_id)
            )
            if moved.rowcount != n:
                db.rollback()
                raise RuntimeError(
                    f"改挂明细数异常：期望 {n}，实际 {moved.rowcount}"
                )

            dec = db.execute(
                update(models.Authorization)
                .where(
                    models.Authorization.id == source_id,
                    models.Authorization.used_count >= n,
                )
                .values(
                    used_count=models.Authorization.used_count - n
                )
            )
            if dec.rowcount != 1:
                db.rollback()
                raise RuntimeError(
                    f"原授权 {source_id} 迁出次数异常（used_count < {n}）"
                )

            inc = db.execute(
                update(models.Authorization)
                .where(
                    models.Authorization.id == target_id,
                    models.Authorization.used_count
                    + models.Authorization.reserved_count
                    <= models.Authorization.max_count - n,
                )
                .values(
                    used_count=models.Authorization.used_count + n
                )
            )
            if inc.rowcount != 1:
                db.rollback()
                raise RuntimeError(
                    f"替代授权 {target_id} 余量不足，迁入 {n} 条失败"
                )

        source.status = "inactive"
        db.commit()
    except MigrationRejected:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise

    db.refresh(source)
    db.refresh(target)
    return {
        "migrated_count": n,
        "source_authorization": source,
        "replacement_authorization": target,
    }


# =============================================================== 额度预留
def create_reservation(
    db: Session, data: ReservationCreate
) -> models.Reservation:
    """预留发行额度：整体核验通过才逐项占额并生成预留编号。

    匹配维度、重叠授权选择顺序与正式申请完全一致；余量判定计入未到期
    预留（其他 pending 预留与本预留之外的所有占额）。任一素材不满足
    则整体拒绝、不占任何额度，``details`` 逐项列明原因。
    """
    if not (
        settings.reservation_min_ttl_minutes
        <= data.ttl_minutes
        <= settings.reservation_max_ttl_minutes
    ):
        raise ReservationRejected(
            [
                {
                    "reason": "invalid_ttl",
                    "message": (
                        "预留有效期（分钟）必须在 "
                        f"{settings.reservation_min_ttl_minutes}～"
                        f"{settings.reservation_max_ttl_minutes} 之间："
                        f"{data.ttl_minutes}"
                    ),
                    "ttl_minutes": data.ttl_minutes,
                }
            ]
        )

    material_ids = list(data.material_ids)
    _validate_material_ids(db, material_ids)

    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(minutes=data.ttl_minutes)

    try:
        _acquire_bucket_lock(db, data.region, data.channel)

        # 先释放到期预留（到期即释放），再参与余量判定。
        sweep_bucket(db, data.region, data.channel, now=now)

        locked_rows = _lock_candidates(
            db, material_ids, data.region, data.channel, data.occur_at
        )
        chosen, reasons = _choose_authorizations(
            material_ids, locked_rows
        )
        if reasons:
            db.rollback()
            raise ReservationRejected(reasons)

        # 逐项预留占额：reserved_count 条件 +1（防超额最后防线）。
        for auth in chosen.values():
            result = db.execute(
                update(models.Authorization)
                .where(
                    models.Authorization.id == auth.id,
                    models.Authorization.used_count
                    + models.Authorization.reserved_count
                    < models.Authorization.max_count,
                )
                .values(
                    reserved_count=models.Authorization.reserved_count + 1
                )
            )
            if result.rowcount != 1:
                db.rollback()
                raise ReservationRejected(
                    [
                        {
                            "material_id": auth.material_id,
                            "reason": "quota_exhausted",
                            "message": (
                                f"授权 id={auth.id} 余量不足，"
                                "并发预留冲突，本次预留整体拒绝"
                            ),
                        }
                    ]
                )

        reservation = models.Reservation(
            region=data.region,
            channel=data.channel,
            occur_at=data.occur_at,
            expires_at=expires_at,
            status="pending",
        )
        db.add(reservation)
        db.flush()
        for index, mid in enumerate(material_ids):
            db.add(
                models.ReservationItem(
                    reservation_id=reservation.id,
                    material_id=mid,
                    authorization_id=chosen[mid].id,
                    item_index=index,
                )
            )
        db.commit()
    except ReservationRejected:
        raise
    except Exception:
        db.rollback()
        raise

    return get_reservation(db, reservation.id)


def _reservation_load_options():
    return (
        selectinload(models.Reservation.items).selectinload(
            models.ReservationItem.material
        ),
        selectinload(models.Reservation.items).selectinload(
            models.ReservationItem.authorization
        ),
        selectinload(models.Reservation.distribution)
        .selectinload(models.Distribution.items)
        .selectinload(models.DistributionItem.authorization),
    )


def _load_reservation(
    db: Session, reservation_id: int, *, for_update: bool = False
) -> models.Reservation | None:
    stmt = (
        select(models.Reservation)
        .where(models.Reservation.id == reservation_id)
        .options(*_reservation_load_options())
    )
    if for_update:
        stmt = stmt.with_for_update()
    return db.scalar(stmt)


def get_reservation(db: Session, reservation_id: int) -> models.Reservation:
    exists = db.get(models.Reservation, reservation_id)
    if exists is None:
        raise ResourceNotFound("预留", reservation_id)
    # 查询前清理其通道的过期预留，使状态/余量与「到期即释放」一致。
    sweep_bucket_in_txn(db, exists.region, exists.channel)
    # 驱逐身份缓存后按完整关系链重新加载（含确认后挂接的正式申请）。
    db.expunge(exists)
    reservation = _load_reservation(db, reservation_id)
    if reservation is None:  # 理论上不会发生
        raise ResourceNotFound("预留", reservation_id)
    return reservation


def list_reservations(
    db: Session, status: str | None = None, limit: int = 100
) -> list[models.Reservation]:
    """预留列表查询（可按状态过滤）。

    返回前对所有涉及通道逐个清理过期预留（每通道独立事务取锁），
    保证过滤 ``pending`` 时不再包含已到期记录。
    """
    buckets = list(
        db.execute(
            select(
                models.Reservation.region, models.Reservation.channel
            ).distinct()
        )
    )
    for region, channel in buckets:
        sweep_bucket_in_txn(db, region, channel)

    stmt = (
        select(models.Reservation)
        .order_by(models.Reservation.id.desc())
        .limit(limit)
        .options(*_reservation_load_options())
    )
    if status is not None:
        stmt = stmt.where(models.Reservation.status == status)
    return list(db.scalars(stmt))


def confirm_reservation(
    db: Session, reservation_id: int
) -> models.Reservation:
    """到期前确认：把预留原子转为一笔已通过申请。

    - 幂等：重复确认返回同一笔申请（200，同一 reservation/distribution）；
    - 取消与确认互斥：已取消确认 → 409 reservation_cancelled；
    - 到期时刻及之后确认视为逾期：sweep 后状态为 expired
      → 409 reservation_expired；
    - 授权停用不影响已有预留的确认（确认沿用预留时逐项选中的授权，
      不再重新匹配、不检查授权 status）。
    """
    head = db.get(models.Reservation, reservation_id)
    if head is None:
        raise ResourceNotFound("预留", reservation_id)
    region, channel = head.region, head.channel

    # 第一步独立事务清理到期预留并提交：即使随后因取消/逾期而拒绝确认，
    # 「到期即释放」也已经持久化，不会被错误回滚带回 pending。
    sweep_bucket_in_txn(db, region, channel)

    try:
        _acquire_bucket_lock(db, region, channel)

        # 预留行锁先于授权行获取并持有到事务结束，确保对同一预留的并发
        # 确认/取消在此严格串行：后到者拿到锁时读到的必是先行者已提交的
        # 最终状态（confirmed/cancelled/expired），不会重复转换占额。
        reservation = _load_reservation(db, reservation_id, for_update=True)

        # 拿到行锁后再做一次到期判定：恰在到期时刻到达的确认必须视为
        # 逾期（本行锁将其与并发取消/确认串行化，二者互斥）。
        if (
            reservation.status == "pending"
            and reservation.expires_at <= datetime.now(timezone.utc)
        ):
            # 按授权 id 升序加锁，条件释放预留占额（防并发取消/确认
            # 已先一步释放时重复扣减）。
            exp_auth_ids = sorted(
                {it.authorization_id for it in reservation.items}
            )
            db.execute(
                select(models.Authorization.id)
                .where(models.Authorization.id.in_(exp_auth_ids))
                .order_by(models.Authorization.id.asc())
                .with_for_update()
            )
            exp_released = 0
            for auth_id in exp_auth_ids:
                exp_released += (
                    db.execute(
                        update(models.Authorization)
                        .where(
                            models.Authorization.id == auth_id,
                            models.Authorization.reserved_count >= 1,
                        )
                        .values(
                            reserved_count=(
                                models.Authorization.reserved_count - 1
                            )
                        )
                    ).rowcount
                    or 0
                )
            if exp_released == len(exp_auth_ids):
                reservation.status = "expired"
                db.commit()
            else:
                # 并发的取消/确认已先一步转走占额：回滚后读取最新状态。
                db.rollback()
                db.expunge_all()
                latest = db.get(models.Reservation, reservation_id)
                if latest is not None and latest.status == "confirmed":
                    return get_reservation(db, reservation_id)
                if latest is not None and latest.status == "cancelled":
                    raise ReservationCancelled(reservation_id)
                if latest is not None and latest.status == "expired":
                    raise ReservationExpired(reservation_id)
                raise RuntimeError(
                    f"预留逾期释放异常：期望 {len(exp_auth_ids)} 项，"
                    f"实际 {exp_released} 项"
                )
            raise ReservationExpired(reservation_id)

        if reservation.status == "confirmed":
            # 幂等：不重复转申请、不重复占额。
            db.commit()
            return get_reservation(db, reservation_id)
        if reservation.status == "cancelled":
            db.rollback()
            raise ReservationCancelled(reservation_id)
        if reservation.status == "expired":
            db.rollback()
            raise ReservationExpired(reservation_id)

        # pending 且未到期：锁定逐项授权（id 升序），
        # reserved_count → used_count 原子转换，净占用不变。
        items = sorted(reservation.items, key=lambda it: it.id)
        auth_ids = sorted({it.authorization_id for it in items})
        db.execute(
            select(models.Authorization.id)
            .where(models.Authorization.id.in_(auth_ids))
            .order_by(models.Authorization.id.asc())
            .with_for_update()
        )

        # 条件转换：仅当授权上确有未被并发事务转走的预留占额时才成功。
        # 正常路径每个授权在本预留中恰好一条明细，影响行数应为 1。
        # （FOR UPDATE 等待提交者后，授权版本可能已被并发确认更新。）
        transferred = 0
        for auth_id in auth_ids:
            result = db.execute(
                update(models.Authorization)
                .where(
                    models.Authorization.id == auth_id,
                    # 必须确有预留占额，且转换后不超额。
                    models.Authorization.reserved_count >= 1,
                    models.Authorization.used_count
                    < models.Authorization.max_count,
                )
                .values(
                    reserved_count=models.Authorization.reserved_count - 1,
                    used_count=models.Authorization.used_count + 1,
                )
            )
            transferred += result.rowcount or 0

        if transferred != len(auth_ids):
            # 影响行数不足：并发的取消/确认已先一步转走预留占额。
            # 回滚本事务新建的申请，读取预留行的最新已提交状态后按
            # 幂等（重复确认）/互斥（已取消/已过期）语义响应。
            db.rollback()
            db.expunge_all()
            latest = db.get(models.Reservation, reservation_id)
            if latest is None:
                raise ResourceNotFound("预留", reservation_id)
            if latest.status == "confirmed":
                return get_reservation(db, reservation_id)
            if latest.status == "cancelled":
                raise ReservationCancelled(reservation_id)
            if latest.status == "expired":
                raise ReservationExpired(reservation_id)
            raise RuntimeError(
                f"预留确认占额转换失败：期望 {len(auth_ids)} 项，"
                f"实际 {transferred} 项"
            )

        # 占额转换全部完成后才落正式申请明细与预留状态（同一事务原子提交）。
        distribution = models.Distribution(
            region=reservation.region,
            channel=reservation.channel,
            occur_at=reservation.occur_at,
            status="approved",
        )
        db.add(distribution)
        db.flush()
        for it in items:
            db.add(
                models.DistributionItem(
                    distribution_id=distribution.id,
                    material_id=it.material_id,
                    authorization_id=it.authorization_id,
                    item_index=it.item_index,
                )
            )

        reservation.status = "confirmed"
        reservation.distribution_id = distribution.id
        reservation.confirmed_at = datetime.now(timezone.utc)
        db.commit()
    except (
        ReservationCancelled,
        ReservationExpired,
    ):
        raise
    except Exception:
        db.rollback()
        raise

    return get_reservation(db, reservation_id)


def cancel_reservation(
    db: Session, reservation_id: int
) -> models.Reservation:
    """取消预留（仅可一次）：逐项释放 reserved_count。

    - 与确认互斥：已确认取消 → 409 reservation_already_confirmed；
    - 已过期取消：幂等返回该 expired 预留（额度已释放，不再释放）；
    - 重复取消：409 reservation_already_cancelled（不再释放额度）。
    """
    head = db.get(models.Reservation, reservation_id)
    if head is None:
        raise ResourceNotFound("预留", reservation_id)
    region, channel = head.region, head.channel

    # 独立事务先持久化到期释放（与确认路径相同的第一阶段）。
    sweep_bucket_in_txn(db, region, channel)

    try:
        _acquire_bucket_lock(db, region, channel)

        reservation = _load_reservation(db, reservation_id, for_update=True)

        # 与确认相同的临界时刻到期保护：拿到行锁后若恰已到期，
        # 先按授权 id 升序加锁，再原子置 expired 并释放，取消按过期
        # 幂等返回。
        if (
            reservation.status == "pending"
            and reservation.expires_at <= datetime.now(timezone.utc)
        ):
            exp_auth_ids = sorted(
                {it.authorization_id for it in reservation.items}
            )
            db.execute(
                select(models.Authorization.id)
                .where(models.Authorization.id.in_(exp_auth_ids))
                .order_by(models.Authorization.id.asc())
                .with_for_update()
            )
            exp_released = 0
            for auth_id in exp_auth_ids:
                exp_released += (
                    db.execute(
                        update(models.Authorization)
                        .where(
                            models.Authorization.id == auth_id,
                            models.Authorization.reserved_count >= 1,
                        )
                        .values(
                            reserved_count=(
                                models.Authorization.reserved_count - 1
                            )
                        )
                    ).rowcount
                    or 0
                )
            if exp_released == len(exp_auth_ids):
                reservation.status = "expired"
                db.commit()
            else:
                # 并发确认/取消已先一步转走占额：回滚后读取最新状态。
                db.rollback()
                db.expunge_all()
                latest = db.get(models.Reservation, reservation_id)
                if latest is not None and latest.status == "confirmed":
                    raise ReservationAlreadyConfirmed(reservation_id)
                if latest is not None and latest.status == "cancelled":
                    raise ReservationAlreadyCancelled(reservation_id)
                if latest is not None and latest.status == "expired":
                    return get_reservation(db, reservation_id)
                raise RuntimeError(
                    f"预留取消逾期释放异常：期望 {len(exp_auth_ids)} 项，"
                    f"实际 {exp_released} 项"
                )
            return get_reservation(db, reservation_id)

        if reservation.status == "cancelled":
            db.rollback()
            raise ReservationAlreadyCancelled(reservation_id)
        if reservation.status == "confirmed":
            db.rollback()
            raise ReservationAlreadyConfirmed(reservation_id)
        if reservation.status == "expired":
            # 过期已由 sweep 释放额度；取消操作幂等返回，不重复释放。
            db.rollback()
            return get_reservation(db, reservation_id)

        # pending：锁定授权（id 升序）后逐项条件释放。
        items = sorted(reservation.items, key=lambda it: it.id)
        auth_ids = sorted({it.authorization_id for it in items})
        db.execute(
            select(models.Authorization.id)
            .where(models.Authorization.id.in_(auth_ids))
            .order_by(models.Authorization.id.asc())
            .with_for_update()
        )
        released = 0
        for auth_id in auth_ids:
            result = db.execute(
                update(models.Authorization)
                .where(
                    models.Authorization.id == auth_id,
                    models.Authorization.reserved_count >= 1,
                )
                .values(
                    reserved_count=models.Authorization.reserved_count - 1
                )
            )
            released += result.rowcount or 0

        if released != len(auth_ids):
            # FOR UPDATE 等待并发提交者后，授权版本可能已被并发确认/取消
            # 更新（reserved_count 已为 0）：回滚后读取预留行的最新已提交
            # 状态，按互斥/幂等语义响应，绝不把 used_count 当作预留重复
            # 释放，也不把并发取消后的状态误判为系统错误。
            db.rollback()
            db.expunge_all()
            latest = db.get(models.Reservation, reservation_id)
            if latest is None:
                raise ResourceNotFound("预留", reservation_id)
            if latest.status == "confirmed":
                raise ReservationAlreadyConfirmed(reservation_id)
            if latest.status == "cancelled":
                raise ReservationAlreadyCancelled(reservation_id)
            if latest.status == "expired":
                return get_reservation(db, reservation_id)
            raise RuntimeError(
                f"预留取消释放失败：期望 {len(auth_ids)} 项，实际 {released} 项"
            )

        reservation.status = "cancelled"
        reservation.cancelled_at = datetime.now(timezone.utc)
        db.commit()
    except (
        ReservationAlreadyCancelled,
        ReservationAlreadyConfirmed,
    ):
        raise
    except Exception:
        db.rollback()
        raise

    return get_reservation(db, reservation_id)

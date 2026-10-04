"""核心业务逻辑：登记 / 核验占用 / 查询 / 撤销 / 授权退役改挂。

并发安全
========
所有写事务遵守统一的全局加锁顺序，杜绝跨事务死锁：

    **通道咨询锁（region+channel）→ 申请行（id 升序）→ 授权行（id 升序）**

发行核验（单事务：咨询锁 → 候选授权行锁 → 判定 → 条件更新扣次 → 落库）：

1. 先获取按 ``region+channel`` 分桶的事务级咨询锁，使同一申请通道内的
   全部写事务（提交/撤销/改挂）串行判定；同一事务内重复获取同把咨询锁
   可重入，事务提交/回滚时自动释放。
2. 再对候选授权按 id 升序执行 ``SELECT ... FOR UPDATE``，涉及相同授权行
   的并发事务由数据库行锁天然串行化。重叠授权的选择规则
   （到期早优先、其次编号小）在 Python 中排序完成。
3. 扣次采用条件更新 ``used_count < max_count`` 并断言影响行数，
   作为超额的最后一道防线。
4. 核验不通过则回滚，不产生任何占用。

撤销（咨询锁 → 申请行锁复查状态 → 明细行锁 → 授权行锁 → 释放）：
先锁申请行复查状态（并发双重撤销恰为一次 ``200``、一次 ``409``），
再 ``FOR UPDATE`` 锁定明细行以固定「明细 → 授权」归属（与改挂并发时
必有一方等待另一方提交，撤销只会释放明细当前归属的授权一次），
最后锁授权行释放。

改挂（见 :func:`migrate_authorization`）：咨询锁 → 两端授权行锁 →
申请行锁 → 明细行锁 → 校验后一次性搬移次数与归属 → 原授权停用。
"""
from collections import Counter
from datetime import datetime, timezone
from zlib import crc32

from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app import models
from app.errors import (
    AlreadyRevoked,
    DistributionRejected,
    DuplicateMaterialItems,
    MigrationRejected,
    MaterialCodeTaken,
    ResourceNotFound,
    UnknownMaterial,
)
from app.schemas import (
    AuthorizationCreate,
    AuthorizationMigrationRequest,
    DistributionCreate,
    MaterialCreate,
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


# ------------------------------------------------------- 发行核验与占用
def _bucket_lock_id(region: str, channel: str) -> int:
    """region+channel → 单个有符号 32 位整数（pg_advisory_xact_lock 参数）。"""
    raw = crc32(f"{region}\x1f{channel}".encode("utf-8"))
    return raw - 0x100000000 if raw >= 0x80000000 else raw


def create_distribution(
    db: Session, data: DistributionCreate
) -> models.Distribution:
    # 1) 重复素材：同一申请中不得携带重复素材。
    counts = Counter(data.material_ids)
    duplicated = sorted(mid for mid, n in counts.items() if n > 1)
    if duplicated:
        raise DuplicateMaterialItems(duplicated)

    material_ids = list(data.material_ids)

    # 2) 未知编号（一次性查全，避免逐行往返）。
    found_ids = set(
        db.scalars(
            select(models.Material.id).where(
                models.Material.id.in_(material_ids)
            )
        )
    )
    unknown = [mid for mid in material_ids if mid not in found_ids]
    if unknown:
        raise UnknownMaterial(sorted(unknown))

    try:
        occur = data.occur_at
        # 3) 先取通道咨询锁：同 region+channel 的提交/撤销/改挂由此
        #    串行判定，消除「涉及不同授权集合的事务各自读到有余量、
        #    先后扣减导致超额」的竞态。统一在行锁之前获取，是所有写
        #    事务的第一加锁点，杜绝跨事务死锁；咨询锁在提交/回滚时
        #    自动释放（同事务重入同把锁无碍）。
        db.execute(
            text("SELECT pg_advisory_xact_lock(:lk)"),
            {"lk": _bucket_lock_id(data.region, data.channel)},
        )

        # 4) 再按授权 id 升序一次性锁定全部候选行（与撤销/改挂事务相同
        #    的全局行锁顺序）。重叠授权的选择规则（到期早优先、其次
        #    编号小）在 Python 中排序完成。
        #    FOR UPDATE 同时锁定停用/无余量候选，保证并发判定稳定。
        locked_rows = list(
            db.scalars(
                select(models.Authorization)
                .where(
                    models.Authorization.region == data.region,
                    models.Authorization.channel == data.channel,
                    # 左闭右开：[starts_at, ends_at)
                    models.Authorization.starts_at <= occur,
                    models.Authorization.ends_at > occur,
                    models.Authorization.material_id.in_(material_ids),
                )
                .order_by(models.Authorization.id.asc())
                .with_for_update()
            )
        )

        # 5) 逐项判定，任一不通过则收集原因并整体回滚。
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
                        "message": "匹配的授权均已停用（停用仅影响新申请）",
                    }
                )
                continue

            # 候选已按 (ends_at, id) 升序，取第一个有余量的。
            with_quota = next(
                (a for a in active if a.used_count < a.max_count), None
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
                            f"{detail.used_count}/{detail.max_count}"
                        ),
                    }
                )
                continue
            chosen[mid] = with_quota

        if reasons:
            # 显式回滚，释放锁；不扣减任何次数。
            db.rollback()
            raise DistributionRejected(reasons)

        # 6) 全部通过：条件更新占用次数（防止超额的最后防线）。
        #    素材已去重，同一授权在一个申请中至多被选中一次。
        for auth in chosen.values():
            result = db.execute(
                update(models.Authorization)
                .where(
                    models.Authorization.id == auth.id,
                    models.Authorization.used_count
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
def get_distribution(db: Session, distribution_id: int) -> models.Distribution:
    distribution = db.get(
        models.Distribution,
        distribution_id,
        options=[
            selectinload(models.Distribution.items).selectinload(
                models.DistributionItem.material
            ),
            selectinload(models.Distribution.items).selectinload(
                models.DistributionItem.authorization
            ),
        ],
    )
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

        # 第一加锁点：该申请所在通道的咨询锁。与进行中的提交/改挂事务
        # 互斥，避免「释放」与「占用/迁占」相互覆盖，同时杜绝跨事务
        # 死锁（所有写事务一律先取咨询锁，再取行锁）。
        db.execute(
            text("SELECT pg_advisory_xact_lock(:lk)"),
            {"lk": _bucket_lock_id(region, channel)},
        )

        # 再锁定申请行，串行化对同一申请的并发撤销，复查状态，
        # 保证后续判定基于最新已提交数据（并发双重撤销恰为一次
        # 200、一次 409）。
        distribution = db.scalar(
            select(models.Distribution)
            .where(models.Distribution.id == distribution_id)
            .with_for_update()
        )
        if distribution.status == "revoked":
            raise AlreadyRevoked(distribution_id)

        # 锁定本申请的全部占用明细行：与改挂事务的明细归属更新互斥，
        # 固定「明细 → 授权」的当前归属，保证并发下撤销只会释放明细
        # 当前归属的授权一次（不会读到旧归属而释放错授权）。
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

        # 按授权 id 升序加锁，与核验/改挂事务保持同一全局行锁顺序。
        if auth_ids:
            db.execute(
                select(models.Authorization.id)
                .where(models.Authorization.id.in_(auth_ids))
                .order_by(models.Authorization.id.asc())
                .with_for_update()
            )

        # 同一授权在本申请中至多出现一次（素材已去重），逐项释放。
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
    - 替代授权余量足够承接全部迁入占用。

    成功后：原授权停用（inactive）、占用次数按迁移数迁出；
    替代授权按迁移数增加占用；改挂明细的 ``authorization_id``
    指向替代授权。返回迁移数与两端最新状态（含剩余次数）。

    并发：单事务内「咨询锁 → 两端授权行锁 → 申请行锁 → 明细行锁」，
    与提交/撤销遵循同一全局加锁顺序；次数搬移全部使用条件更新并断言
    影响行数，绝不超额或重复释放。
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

    # 不加锁读取，仅用于存在性/维度判断与确定咨询锁桶；
    # 维度（material_id/region/channel）不可变，无需锁保护。
    source = db.get(models.Authorization, source_id)
    if source is None:
        raise ResourceNotFound("授权", source_id)
    target = db.get(models.Authorization, target_id)
    if target is None:
        raise ResourceNotFound("授权", target_id)

    try:
        # 1) 第一加锁点：通道咨询锁（两端 region/channel 必须一致，
        #    不一致直接拒绝，不触碰任何锁）。
        if (
            source.region != target.region
            or source.channel != target.channel
        ):
            raise _reject(
                "dimension_mismatch",
                "原授权与替代授权的地区、渠道不一致，不可改挂",
            )
        if source.material_id != target.material_id:
            raise _reject(
                "dimension_mismatch",
                "原授权与替代授权的素材不一致，不可改挂",
            )

        db.execute(
            text("SELECT pg_advisory_xact_lock(:lk)"),
            {"lk": _bucket_lock_id(source.region, source.channel)},
        )

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

        # 3) 替代授权必须启用（停用仅影响新申请，但退役改挂属于新占用，
        #    同样不允许落到停用授权上）。
        if target.status != "active":
            raise _reject(
                "replacement_inactive",
                f"替代授权（id={target_id}）未处于启用状态",
            )

        # 4) 锁定原授权承载的全部未撤销申请的申请行（id 升序），
        #    与并发撤销互斥，拿到这批申请的稳定快照。
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

        # 5) 锁定这些申请下归属原授权的明细行（FOR UPDATE）。
        #    与撤销事务的明细锁互斥：并发撤销要么在改挂前提交
        #    （该明细不在此集合中），要么等待改挂提交后再释放替代授权，
        #    绝不会重复释放或释放错授权。
        items: list[models.DistributionItem] = []
        occur_times: list[datetime] = []
        if dist_ids:
            items = list(
                db.scalars(
                    select(models.DistributionItem)
                    .where(
                        models.DistributionItem.distribution_id.in_(
                            dist_ids
                        ),
                        models.DistributionItem.authorization_id
                        == source_id,
                    )
                    .order_by(models.DistributionItem.id.asc())
                    .with_for_update()
                )
            )
            # 明细数量与申请数一致：素材在申请内去重，一个授权对一个
            # 申请至多贡献一条明细。
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

        # 7) 余量必须足够一次性承接全部迁入占用。
        if target.max_count - target.used_count < n:
            raise _reject(
                "insufficient_quota",
                f"替代授权（id={target_id}）余量不足：待迁入 {n} 条，"
                f"剩余 {target.max_count - target.used_count} 条",
            )

        # 8) 一次性搬移：
        #    - 明细归属改为替代授权（仅改仍归属原授权的行，双重保险）；
        #    - 原授权 used_count 条件减 n；替代授权条件加 n（防超额
        #      最后防线）；原授权停用。
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

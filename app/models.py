"""ORM 模型。

授权时段为带时区的 **左闭右开** 区间 ``[starts_at, ends_at)``，
核验时使用 ``starts_at <= occur_at < ends_at``。
"""
from datetime import datetime, timezone

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    DateTime,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Material(Base):
    __tablename__ = "materials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    authorizations: Mapped[list["Authorization"]] = relationship(
        back_populates="material"
    )


class Authorization(Base):
    __tablename__ = "authorizations"
    __table_args__ = (
        CheckConstraint("starts_at < ends_at", name="ck_auth_valid_period"),
        CheckConstraint(
            "used_count >= 0 AND used_count <= max_count",
            name="ck_auth_quota_invariant",
        ),
        # 未到期预留同样占额：正式占用 + 预留占用不得超过总额度。
        CheckConstraint(
            "reserved_count >= 0 "
            "AND used_count + reserved_count <= max_count",
            name="ck_auth_reserved_invariant",
        ),
        Index(
            "ix_auth_match",
            "material_id",
            "region",
            "channel",
            "starts_at",
            "ends_at",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    material_id: Mapped[int] = mapped_column(
        ForeignKey("materials.id", ondelete="RESTRICT"), nullable=False
    )
    region: Mapped[str] = mapped_column(String(64), nullable=False)
    channel: Mapped[str] = mapped_column(String(64), nullable=False)
    starts_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    ends_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    max_count: Mapped[int] = mapped_column(Integer, nullable=False)
    used_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # 未到期、待确认预留的逐项占额（确认时原子转入 used_count，
    # 取消/过期时释放）。
    reserved_count: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    # active：可用于新申请/新预留；inactive：已停用，仅影响新申请与新预留。
    status: Mapped[str] = mapped_column(
        String(16), default="active", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    material: Mapped["Material"] = relationship(back_populates="authorizations")

    @property
    def remaining(self) -> int:
        """正式余量（语义与旧版完全一致：总额度 - 已正式占用）。"""
        return self.max_count - self.used_count

    @property
    def available(self) -> int:
        """可预留余量：扣除正式占用与未到期预留占用后尚可使用的次数。"""
        return self.max_count - self.used_count - self.reserved_count


class Distribution(Base):
    """一次发行申请：整体通过（approved）或整体不通过（不落库）。"""

    __tablename__ = "distributions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('approved', 'revoked')", name="ck_dist_status"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    region: Mapped[str] = mapped_column(String(64), nullable=False)
    channel: Mapped[str] = mapped_column(String(64), nullable=False)
    occur_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(16), default="approved", nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    items: Mapped[list["DistributionItem"]] = relationship(
        back_populates="distribution",
        cascade="all, delete-orphan",
        order_by="DistributionItem.id",
    )


class DistributionItem(Base):
    """申请中每个素材所占用的授权（占用时快照授权维度信息）。"""

    __tablename__ = "distribution_items"
    __table_args__ = (
        Index("ix_dist_item_auth", "authorization_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    distribution_id: Mapped[int] = mapped_column(
        ForeignKey("distributions.id", ondelete="CASCADE"), nullable=False
    )
    material_id: Mapped[int] = mapped_column(
        ForeignKey("materials.id", ondelete="RESTRICT"), nullable=False
    )
    authorization_id: Mapped[int] = mapped_column(
        ForeignKey("authorizations.id", ondelete="RESTRICT"), nullable=False
    )
    item_index: Mapped[int] = mapped_column(Integer, nullable=False)

    distribution: Mapped["Distribution"] = relationship(back_populates="items")
    material: Mapped["Material"] = relationship()
    authorization: Mapped["Authorization"] = relationship()


class Reservation(Base):
    """发行额度预留：整体通过（pending）后逐项占额，确认后转一笔正式申请。

    状态机::

        pending ──确认──▶ confirmed（同一行 distribution_id 幂等）
            ├──取消──▶ cancelled（仅可一次）
            └──到期──▶ expired（expires_at <= 当前时刻即逾期）

    取消与确认互斥；已取消/已过期后的确认一律拒绝；
    重复取消不再释放额度；到期时刻确认视为逾期。
    """

    __tablename__ = "reservations"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'confirmed', 'cancelled', 'expired')",
            name="ck_resv_status",
        ),
        # 仅 confirmed 预留挂正式申请；其余状态 distribution_id 必须为空。
        CheckConstraint(
            "(status = 'confirmed') = (distribution_id IS NOT NULL)",
            name="ck_resv_confirmed_has_distribution",
        ),
        CheckConstraint(
            "cancelled_at IS NULL OR status = 'cancelled'",
            name="ck_resv_cancelled_at",
        ),
        Index("ix_resv_bucket_status_expires", "region", "channel", "status"),
        Index("ix_resv_expires", "expires_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    region: Mapped[str] = mapped_column(String(64), nullable=False)
    channel: Mapped[str] = mapped_column(String(64), nullable=False)
    occur_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(16), default="pending", nullable=False
    )
    # 确认成功后挂接的那一笔已通过申请；重复确认据此返回同一申请。
    distribution_id: Mapped[int | None] = mapped_column(
        ForeignKey("distributions.id", ondelete="RESTRICT"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancelled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    items: Mapped[list["ReservationItem"]] = relationship(
        back_populates="reservation",
        cascade="all, delete-orphan",
        order_by="ReservationItem.id",
    )
    distribution: Mapped["Distribution | None"] = relationship()


class ReservationItem(Base):
    """预留中每个素材逐项选中的授权（预留成功即占额，直至确认/取消/过期）。"""

    __tablename__ = "reservation_items"
    __table_args__ = (
        Index("ix_resv_item_auth", "authorization_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    reservation_id: Mapped[int] = mapped_column(
        ForeignKey("reservations.id", ondelete="CASCADE"), nullable=False
    )
    material_id: Mapped[int] = mapped_column(
        ForeignKey("materials.id", ondelete="RESTRICT"), nullable=False
    )
    authorization_id: Mapped[int] = mapped_column(
        ForeignKey("authorizations.id", ondelete="RESTRICT"), nullable=False
    )
    item_index: Mapped[int] = mapped_column(Integer, nullable=False)

    reservation: Mapped["Reservation"] = relationship(back_populates="items")
    material: Mapped["Material"] = relationship()
    authorization: Mapped["Authorization"] = relationship()

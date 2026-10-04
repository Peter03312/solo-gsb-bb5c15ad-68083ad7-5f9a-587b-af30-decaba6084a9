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
    # active：可用于新申请；inactive：已停用，仅影响新申请。
    status: Mapped[str] = mapped_column(
        String(16), default="active", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    material: Mapped["Material"] = relationship(back_populates="authorizations")

    @property
    def remaining(self) -> int:
        return self.max_count - self.used_count


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

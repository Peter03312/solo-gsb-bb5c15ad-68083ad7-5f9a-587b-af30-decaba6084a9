"""请求 / 响应模型。

所有时间字段均强制要求带时区信息（如 ``2026-10-01T00:00:00+08:00``），
服务端统一按 UTC 存储、按原始语义比较，彻底避免夏令时 / 时区歧义。
"""
from datetime import datetime
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


def _ensure_aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(f"{field_name} 必须携带时区信息，例如 +08:00")
    return value


class MaterialCreate(BaseModel):
    code: str = Field(min_length=1, max_length=64, description="素材业务编号")
    name: str = Field(min_length=1, max_length=255, description="素材名称")

    @field_validator("code", "name")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("不能为空串")
        return v


class MaterialOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    name: str
    created_at: datetime


class AuthorizationCreate(BaseModel):
    material_id: int = Field(gt=0)
    region: str = Field(min_length=1, max_length=64)
    channel: str = Field(min_length=1, max_length=64)
    # 左闭右开：[starts_at, ends_at)
    starts_at: datetime
    ends_at: datetime
    max_count: int = Field(gt=0, description="可发行次数（正整数）")

    @field_validator("region", "channel")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("不能为空串")
        return v

    @field_validator("starts_at")
    @classmethod
    def _starts_aware(cls, v: datetime) -> datetime:
        return _ensure_aware(v, "starts_at")

    @field_validator("ends_at")
    @classmethod
    def _ends_aware(cls, v: datetime) -> datetime:
        return _ensure_aware(v, "ends_at")

    @model_validator(mode="after")
    def _validate_period(self) -> "AuthorizationCreate":
        if self.starts_at >= self.ends_at:
            raise ValueError(
                "非法时段：左闭右开要求 starts_at 严格早于 ends_at"
            )
        return self


class AuthorizationUpdate(BaseModel):
    status: Literal["active", "inactive"] = Field(
        description="active=启用，inactive=停用（仅影响新申请）"
    )


class AuthorizationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    material_id: int
    region: str
    channel: str
    starts_at: datetime
    ends_at: datetime
    max_count: int
    # used_count/remaining 语义与旧版完全一致：仅统计已通过（未撤销）申请。
    used_count: int
    remaining: int
    # 未到期待确认预留对该授权的逐项占额。
    reserved_count: int
    # 可预留余量 = max_count - used_count - reserved_count，
    # 即新申请与新预留此刻还能使用的次数。
    available: int
    status: str
    created_at: datetime


class DistributionCreate(BaseModel):
    material_ids: list[int] = Field(
        min_length=1, description="本次申请发行的素材编号（数据库自增 id）"
    )
    region: str = Field(min_length=1, max_length=64)
    channel: str = Field(min_length=1, max_length=64)
    occur_at: datetime = Field(description="发行时刻（带时区）")

    @field_validator("material_ids")
    @classmethod
    def _positive_ids(cls, v: list[int]) -> list[int]:
        if any(mid <= 0 for mid in v):
            raise ValueError("素材编号必须为正整数")
        return v

    @field_validator("region", "channel")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("不能为空串")
        return v

    @field_validator("occur_at")
    @classmethod
    def _occur_aware(cls, v: datetime) -> datetime:
        return _ensure_aware(v, "occur_at")


class DistributionItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    item_index: int
    material_id: int
    authorization: AuthorizationOut


# ------------------------------------------------------- 授权退役改挂
class AuthorizationMigrationRequest(BaseModel):
    """原授权退役：将其承载的全部未撤销申请一次性改挂到替代授权。"""

    source_authorization_id: int = Field(
        gt=0, description="原授权编号（退役方）"
    )
    replacement_authorization_id: int = Field(
        gt=0, description="替代授权编号（承接方，须与原授权不同）"
    )


class AuthorizationMigrationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    migrated_count: int = Field(description="实际改挂的未撤销申请明细条数")
    source_authorization: AuthorizationOut = Field(
        description="原授权（成功后已停用，次数已迁出）"
    )
    replacement_authorization: AuthorizationOut = Field(
        description="替代授权（已承接全部迁入占用）"
    )


class DistributionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    region: str
    channel: str
    occur_at: datetime
    status: Literal["approved", "revoked"]
    created_at: datetime
    revoked_at: datetime | None = None
    items: list[DistributionItemOut]


# --------------------------------------------------------------- 额度预留
class ReservationCreate(BaseModel):
    material_ids: list[int] = Field(
        min_length=1, description="本次预留的素材编号（数据库自增 id，不可重复）"
    )
    region: str = Field(min_length=1, max_length=64)
    channel: str = Field(min_length=1, max_length=64)
    occur_at: datetime = Field(description="带时区发行时刻")
    ttl_minutes: int = Field(
        description=(
            "预留有效期（分钟），须在服务端配置的 "
            "RESERVATION_MIN/MAX_TTL_MINUTES 区间内（默认 1～30）；"
            "到期时刻 = 创建时刻 + ttl_minutes，到期时刻确认视为逾期"
        )
    )

    @field_validator("material_ids")
    @classmethod
    def _positive_ids(cls, v: list[int]) -> list[int]:
        if any(mid <= 0 for mid in v):
            raise ValueError("素材编号必须为正整数")
        return v

    @field_validator("region", "channel")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("不能为空串")
        return v

    @field_validator("occur_at")
    @classmethod
    def _occur_aware(cls, v: datetime) -> datetime:
        return _ensure_aware(v, "occur_at")


class ReservationItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    item_index: int
    material_id: int
    authorization: AuthorizationOut


class ReservationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    region: str
    channel: str
    occur_at: datetime
    expires_at: datetime
    status: Literal["pending", "confirmed", "cancelled", "expired"]
    created_at: datetime
    confirmed_at: datetime | None = None
    cancelled_at: datetime | None = None
    # 确认成功后挂接的那一笔已通过申请；重复确认返回同一申请。
    distribution_id: int | None = None
    distribution: DistributionOut | None = None
    items: list[ReservationItemOut]


class ReservationListOut(BaseModel):
    """预留查询结果：可按状态过滤（缺省返回全部）。"""

    items: list[ReservationOut]
    count: int

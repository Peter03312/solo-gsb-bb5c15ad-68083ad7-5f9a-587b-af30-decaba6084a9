"""领域异常：统一转换为带错误码的 JSON 响应。"""


class APIError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: list[dict] | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details


class ResourceNotFound(APIError):
    """路径编号不存在。"""

    def __init__(self, resource: str, identifier: object):
        super().__init__(
            status_code=404,
            code="not_found",
            message=f"{resource}不存在：{identifier}",
        )


class MaterialCodeTaken(APIError):
    def __init__(self, code: str):
        super().__init__(
            status_code=409,
            code="duplicate_material",
            message=f"素材编号已存在：{code}",
        )


class UnknownMaterial(APIError):
    """请求体内引用了未知素材编号。"""

    def __init__(self, unknown_ids: list[int]):
        super().__init__(
            status_code=422,
            code="unknown_material",
            message=f"未知素材编号：{', '.join(map(str, unknown_ids))}",
            details=[{"material_id": mid} for mid in unknown_ids],
        )


class DuplicateMaterialItems(APIError):
    def __init__(self, duplicated_ids: list[int]):
        super().__init__(
            status_code=422,
            code="duplicate_material_items",
            message=f"申请中素材编号重复：{', '.join(map(str, duplicated_ids))}",
            details=[{"material_id": mid} for mid in duplicated_ids],
        )


class DistributionRejected(APIError):
    """授权核验未通过：整体拒绝、不占用任何次数。"""

    def __init__(self, reasons: list[dict]):
        super().__init__(
            status_code=422,
            code="distribution_rejected",
            message="授权核验未通过，未占用任何发行次数",
            details=reasons,
        )


class AlreadyRevoked(APIError):
    def __init__(self, distribution_id: int):
        super().__init__(
            status_code=409,
            code="already_revoked",
            message=f"发行申请已撤销，不可重复撤销：{distribution_id}",
        )


class MigrationRejected(APIError):
    """授权退役改挂核验未通过：整体拒绝，归属与次数均不变。"""

    def __init__(self, reasons: list[dict]):
        super().__init__(
            status_code=422,
            code="migration_rejected",
            message="授权改挂核验未通过，未变更任何申请归属与次数",
            details=reasons,
        )


class ReservationRejected(APIError):
    """预留核验未通过：整体拒绝、不占任何额度（正式/预留均不占）。"""

    def __init__(self, reasons: list[dict]):
        super().__init__(
            status_code=422,
            code="reservation_rejected",
            message="预留核验未通过，未占用任何发行额度",
            details=reasons,
        )


class ReservationExpired(APIError):
    """预留已过期：确认被拒绝，额度已释放（或本次原子释放）。"""

    def __init__(self, reservation_id: int):
        super().__init__(
            status_code=409,
            code="reservation_expired",
            message=(
                f"预留已过期，确认视为逾期，额度已释放：{reservation_id}"
            ),
        )


class ReservationCancelled(APIError):
    """对已取消预留执行确认：拒绝；取消与确认互斥。"""

    def __init__(self, reservation_id: int):
        super().__init__(
            status_code=409,
            code="reservation_cancelled",
            message=(
                "预留已取消，取消与确认互斥，不可再确认："
                f"{reservation_id}"
            ),
        )


class ReservationAlreadyConfirmed(APIError):
    """对已确认预留重复确认不是错误（返回同一申请）；

    但对已确认预留执行取消属于非法状态转换。
    """

    def __init__(self, reservation_id: int):
        super().__init__(
            status_code=409,
            code="reservation_already_confirmed",
            message=f"预留已确认，不可取消：{reservation_id}",
        )


class ReservationAlreadyCancelled(APIError):
    """重复取消：不再释放额度，返回 409。"""

    def __init__(self, reservation_id: int):
        super().__init__(
            status_code=409,
            code="reservation_already_cancelled",
            message=f"预留已取消，不可重复取消：{reservation_id}",
        )

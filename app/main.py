"""FastAPI 应用入口与 HTTP 路由。"""
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app import schemas, services
from app.database import get_db, init_db
from app.errors import APIError


@asynccontextmanager
async def lifespan(app: FastAPI):
    # docker compose up 后自动建表，无需手工迁移。
    init_db()
    yield


app = FastAPI(
    title="素材发行授权核验 API",
    version="1.1.0",
    description=(
        "登记素材与授权（地区/渠道/带时区左闭右开时段/可发行次数），"
        "对发行申请做整体核验、原子占用，支持查询、一次性撤销，"
        "以及授权退役时将未撤销申请一次性改挂到替代授权。"
    ),
    lifespan=lifespan,
)


@app.exception_handler(APIError)
async def api_error_handler(request: Request, exc: APIError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "code": exc.code,
                "message": exc.message,
                "details": exc.details or [],
            }
        },
    )


@app.get("/health", tags=["system"])
def health() -> dict:
    return {"status": "ok"}


# ------------------------------------------------------------------ 素材
@app.post(
    "/api/v1/materials",
    response_model=schemas.MaterialOut,
    status_code=201,
    tags=["materials"],
)
def create_material(
    body: schemas.MaterialCreate, db: Session = Depends(get_db)
):
    return services.create_material(db, body)


@app.get(
    "/api/v1/materials/{material_id}",
    response_model=schemas.MaterialOut,
    tags=["materials"],
)
def get_material(material_id: int, db: Session = Depends(get_db)):
    return services.get_material(db, material_id)


# ------------------------------------------------------------------ 授权
@app.post(
    "/api/v1/authorizations",
    response_model=schemas.AuthorizationOut,
    status_code=201,
    tags=["authorizations"],
)
def create_authorization(
    body: schemas.AuthorizationCreate, db: Session = Depends(get_db)
):
    return services.create_authorization(db, body)


@app.get(
    "/api/v1/authorizations/{authorization_id}",
    response_model=schemas.AuthorizationOut,
    tags=["authorizations"],
)
def get_authorization(authorization_id: int, db: Session = Depends(get_db)):
    return services.get_authorization(db, authorization_id)


@app.patch(
    "/api/v1/authorizations/{authorization_id}",
    response_model=schemas.AuthorizationOut,
    tags=["authorizations"],
)
def update_authorization(
    authorization_id: int,
    body: schemas.AuthorizationUpdate,
    db: Session = Depends(get_db),
):
    return services.set_authorization_status(
        db, authorization_id, body.status
    )


@app.post(
    "/api/v1/authorizations/migrate",
    response_model=schemas.AuthorizationMigrationOut,
    tags=["authorizations"],
    summary="授权退役改挂：原授权承载的全部未撤销申请一次性改挂到替代授权",
)
def migrate_authorization(
    body: schemas.AuthorizationMigrationRequest,
    db: Session = Depends(get_db),
):
    return services.migrate_authorization(db, body)


# ------------------------------------------------------------------ 发行
@app.post(
    "/api/v1/distributions",
    response_model=schemas.DistributionOut,
    status_code=201,
    tags=["distributions"],
    summary="提交发行申请：整体核验并原子占用次数",
)
def create_distribution(
    body: schemas.DistributionCreate, db: Session = Depends(get_db)
):
    return services.create_distribution(db, body)


@app.get(
    "/api/v1/distributions/{distribution_id}",
    response_model=schemas.DistributionOut,
    tags=["distributions"],
)
def get_distribution(distribution_id: int, db: Session = Depends(get_db)):
    return services.get_distribution(db, distribution_id)


@app.post(
    "/api/v1/distributions/{distribution_id}/revoke",
    response_model=schemas.DistributionOut,
    tags=["distributions"],
    summary="撤销发行申请（仅可撤销一次，释放占用次数）",
)
def revoke_distribution(
    distribution_id: int, db: Session = Depends(get_db)
):
    return services.revoke_distribution(db, distribution_id)

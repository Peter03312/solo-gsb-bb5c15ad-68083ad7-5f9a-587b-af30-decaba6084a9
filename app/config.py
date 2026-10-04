"""应用配置。

所有配置均通过环境变量读取，docker-compose.yml 中已给出默认值，
可用项目根目录下的 .env 文件覆盖。
"""
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv(
        "DATABASE_URL",
        "postgresql+psycopg://licensing:licensing@db:5432/licensing",
    )
    # 启动时等待数据库就绪的最长秒数（期间每秒重试）。
    db_wait_timeout: int = int(os.getenv("DB_WAIT_TIMEOUT", "30"))
    db_pool_size: int = int(os.getenv("DB_POOL_SIZE", "10"))
    db_max_overflow: int = int(os.getenv("DB_MAX_OVERFLOW", "20"))


settings = Settings()

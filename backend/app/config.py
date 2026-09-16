from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SAMPLE_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://postgres@localhost:5432/sample_db"
    # 交接码有效期（秒）：创建 10 分钟后到期
    handoff_ttl_seconds: int = 600
    # 交接码长度（去掉易混字符后的字母数字）
    code_length: int = 8
    # 仅验收/测试环境开启：注册重置与到期模拟钩子，生产必须保持 false
    enable_test_reset: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()

"""환경설정."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parents[1]

@dataclass(frozen=True)
class Settings:
    project_root: Path = PROJECT_ROOT

    output_dir: Path = Path(os.getenv("REPORT_OUTPUT_DIR", str(PROJECT_ROOT / "outputs")))

    vector_index_path: Path = Path(os.getenv("REPORT_VECTOR_INDEX_PATH", str(PROJECT_ROOT / "knowledge" / "vector_index.json"),))


    db_host: str = os.getenv("SIM_DB_HOST", "aws-0-ap-northeast-1.pooler.supabase.com")
    db_port: int = int(os.getenv("SIM_DB_PORT", "5432"))
    db_name: str = os.getenv("SIM_DB_NAME", "postgres")
    db_user: str = os.getenv("SIM_DB_USER", "")
    db_password: str = os.getenv("SIM_DB_PASSWORD", "")
    db_sslmode: str = os.getenv("SIM_DB_SSLMODE", "require")
    # 연결 타임아웃(초). 미지정 시 psycopg2는 무한 대기하여 앱이 기동되지 않는다.
    db_connect_timeout: int = int(os.getenv("SIM_DB_CONNECT_TIMEOUT", "5"))

    api_host: str = os.getenv("SIM_API_HOST", "0.0.0.0")
    api_port: int = int(os.getenv("SIM_API_PORT", "8000"))

    @property
    def dsn(self) -> str:
        return (
            f"host={self.db_host} port={self.db_port} dbname={self.db_name} "
            f"user={self.db_user} password={self.db_password} sslmode={self.db_sslmode} "
            f"connect_timeout={self.db_connect_timeout}"
        )


settings = Settings()

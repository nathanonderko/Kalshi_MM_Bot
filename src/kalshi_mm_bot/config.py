import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[2]

DEMO_WS_URL = "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2"
PROD_WS_URL = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"

DEMO_REST_BASE_URL = "https://external-api.demo.kalshi.co/trade-api/v2"
PROD_REST_BASE_URL = "https://external-api.kalshi.com/trade-api/v2"


@dataclass(frozen=True, slots=True)
class EnvironmentConfig:
    name: str
    rest_base_url: str
    ws_url: str


@dataclass(frozen=True, slots=True)
class Settings:
    api_key_id: str
    private_key_path: Path

    demo_ws_url: str = DEMO_WS_URL
    prod_ws_url: str = PROD_WS_URL

    demo_rest_base_url: str = DEMO_REST_BASE_URL
    prod_rest_base_url: str = PROD_REST_BASE_URL

    def environment(self, *, prod: bool) -> EnvironmentConfig:
        if prod:
            return EnvironmentConfig(
                name="production",
                rest_base_url=self.prod_rest_base_url,
                ws_url=self.prod_ws_url,
            )

        return EnvironmentConfig(
            name="demo",
            rest_base_url=self.demo_rest_base_url,
            ws_url=self.demo_ws_url,
        )


def load_settings() -> Settings:
    load_dotenv(PROJECT_ROOT / ".env")

    return Settings(
        api_key_id=os.environ["KALSHI_API_KEY_ID"],
        private_key_path=_project_path(os.environ["KALSHI_PRIVATE_KEY_PATH"]),
    )


def _project_path(raw_path: str) -> Path:
    path = Path(raw_path)
    return path if path.is_absolute() else PROJECT_ROOT / path

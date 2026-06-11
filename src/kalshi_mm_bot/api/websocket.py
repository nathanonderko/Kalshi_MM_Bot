import json
from typing import Any

from websockets.asyncio.client import ClientConnection, connect

from kalshi_mm_bot.api.auth import KalshiAuth


class KalshiWebSocketClient:
    def __init__(self, ws_url: str, auth: KalshiAuth) -> None:
        self.ws_url = ws_url
        self.ws_path = "/trade-api/ws/v2"
        self.auth = auth
        self._connection: ClientConnection | None = None

    async def connect(self) -> None:
        if self._connection is not None:
            raise RuntimeError("websocket is already connected")

        headers = self.auth.signed_headers("GET", self.ws_path)

        self._connection = await connect(
            self.ws_url,
            additional_headers=headers,
        )

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.close()
            self._connection = None

    async def send_json(self, payload: dict[str, Any]) -> None:
        connection = self._require_connection()
        await connection.send(json.dumps(payload, separators=(",", ":")))

    async def recv_json(self) -> dict[str, Any]:
        connection = self._require_connection()
        raw = await connection.recv()

        if not isinstance(raw, str):
            raise TypeError("expected websocket text message, got bytes")

        msg = json.loads(raw)

        if not isinstance(msg, dict):
            raise TypeError("expected JSON object from websocket")

        return msg

    def _require_connection(self) -> ClientConnection:
        if self._connection is None:
            raise RuntimeError("websocket is not connected")
        return self._connection

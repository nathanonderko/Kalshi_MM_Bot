import base64
import time
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

_SIGNATURE_HASH = hashes.SHA256()
_SIGNATURE_PADDING = padding.PSS(
    mgf=padding.MGF1(_SIGNATURE_HASH),
    salt_length=padding.PSS.DIGEST_LENGTH,
)


class KalshiAuth:
    def __init__(self, api_key_id: str, private_key_path: Path) -> None:
        self.api_key_id = api_key_id
        self.private_key_path = private_key_path
        self._private_key: rsa.RSAPrivateKey | None = None

    def signed_headers(self, method: str, path: str) -> dict[str, str]:
        timestamp_ms = str(int(time.time() * 1000))
        signature = self._sign(timestamp_ms, method, path)

        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
            "KALSHI-ACCESS-SIGNATURE": signature,
        }

    def _sign(self, timestamp_ms: str, method: str, path: str) -> str:
        message = f"{timestamp_ms}{method.upper()}{path}".encode("utf-8")

        signature = self._load_private_key().sign(
            message,
            _SIGNATURE_PADDING,
            _SIGNATURE_HASH,
        )

        return base64.b64encode(signature).decode("utf-8")

    def _load_private_key(self) -> rsa.RSAPrivateKey:
        if self._private_key is not None:
            return self._private_key

        private_key_bytes = self.private_key_path.read_bytes()

        private_key = serialization.load_pem_private_key(
            private_key_bytes,
            password=None,
        )

        if not isinstance(private_key, rsa.RSAPrivateKey):
            raise TypeError("Kalshi private key must be an RSA private key")

        self._private_key = private_key
        return self._private_key

from __future__ import annotations

import base64
import logging
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from jwt.exceptions import InvalidTokenError as JWTError

logger = logging.getLogger(__name__)


def _b64url_uint(value: int) -> str:
    """Base64url-encode an unsigned integer (no padding)."""
    length = (value.bit_length() + 7) // 8
    raw = value.to_bytes(length, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


class JWKSKeyStore:
    """Stores RSA key pairs and supports JWT signing/verification with key rotation."""

    def __init__(self) -> None:
        # Ordered list of (kid, private_pem, public_pem)
        self._keys: list[tuple[str, bytes, bytes]] = []

    def add_key(self, kid: str, private_pem: bytes, public_pem: bytes) -> None:
        """Add a key pair identified by kid."""
        self._keys.append((kid, private_pem, public_pem))

    def get_jwks(self) -> dict[str, Any]:
        """Return all public keys in JWK Set format."""
        jwk_list: list[dict[str, str]] = []
        for kid, _priv, pub_pem in self._keys:
            public_key: RSAPublicKey = serialization.load_pem_public_key(pub_pem)  # type: ignore[assignment]
            numbers = public_key.public_numbers()
            jwk_list.append(
                {
                    "kty": "RSA",
                    "use": "sig",
                    "alg": "RS256",
                    "kid": kid,
                    "n": _b64url_uint(numbers.n),
                    "e": _b64url_uint(numbers.e),
                }
            )
        return {"keys": jwk_list}

    def sign_token(self, claims: dict[str, Any], kid: str | None = None) -> str:
        """Sign a JWT with the specified kid or the most recently added key."""
        if not self._keys:
            raise ValueError("No keys registered in JWKSKeyStore")

        if kid is not None:
            match = next((entry for entry in self._keys if entry[0] == kid), None)
            if match is None:
                raise KeyError(f"kid {kid!r} not found in key store")
            selected = match
        else:
            selected = self._keys[-1]

        selected_kid, private_pem, _ = selected
        headers = {"kid": selected_kid}
        return jwt.encode(claims, private_pem.decode("utf-8"), algorithm="RS256", headers=headers)

    def verify_token(self, token: str) -> dict[str, Any]:
        """Verify a JWT by trying each registered public key. Returns claims."""
        if not self._keys:
            raise ValueError("No keys registered in JWKSKeyStore")

        last_exc: JWTError | None = None
        for _kid, _priv, pub_pem in self._keys:
            try:
                claims: dict[str, Any] = jwt.decode(
                    token,
                    pub_pem.decode("utf-8"),
                    algorithms=["RS256"],
                )
                return claims
            except JWTError as exc:
                logging.getLogger(__name__).exception("verify_token caught exception")
                last_exc = exc

        raise last_exc or JWTError("Token verification failed")


# Global singleton
key_store = JWKSKeyStore()


# FastAPI router
JWKSRouter = APIRouter()


@JWKSRouter.get("/api/.well-known/jwks.json", response_class=JSONResponse)
async def get_jwks() -> JSONResponse:
    """Return the JWKS document with all active public keys."""
    return JSONResponse(content=key_store.get_jwks())

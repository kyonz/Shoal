"""Workload identity tokens (architecture §7.1).

Shoal is an OpenID Connect issuer for its own workloads. It signs short-lived
JWTs that name a piece of work (``sub``) for one audience, and publishes the
discovery document and JWKS at ``/.well-known`` so a cloud provider (AWS STS,
GCP workload identity federation, Azure federated credentials) can verify them
and exchange them for cloud credentials. No cloud secret is stored in Shoal.

The ES256 signing key comes from ``SHOAL_IDENTITY_SIGNING_KEY`` (PEM) or is
derived from ``SHOAL_SECRET_KEY``, so every replica signs with the same key
without extra configuration. Rotating either rotates the key.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from shoal.core import clock

if TYPE_CHECKING:
    from shoal.config import Settings

ALGORITHM = "ES256"
# Order of the P-256 group.
_P256_N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551


def _derive_key(secret: str) -> ec.EllipticCurvePrivateKey:
    digest = hmac.new(secret.encode(), b"shoal-workload-identity-es256", hashlib.sha512).digest()
    scalar = int.from_bytes(digest, "big") % (_P256_N - 1) + 1
    return ec.derive_private_key(scalar, ec.SECP256R1())


class WorkloadIssuer:
    def __init__(self, settings: Settings):
        self.issuer = (settings.oidc_issuer or settings.public_url).rstrip("/")
        if settings.identity_signing_key is not None:
            key = serialization.load_pem_private_key(settings.identity_signing_key.get_secret_value().encode(), None)
            if not isinstance(key, ec.EllipticCurvePrivateKey) or key.curve.name != "secp256r1":
                raise ValueError("SHOAL_IDENTITY_SIGNING_KEY must be an EC P-256 private key")
            self._key = key
        else:
            self._key = _derive_key(settings.secret_key.get_secret_value())
        public_der = self._key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        self.kid = base64.urlsafe_b64encode(hashlib.sha256(public_der).digest()[:12]).decode().rstrip("=")

    def jwks(self) -> dict[str, Any]:
        jwk = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(self._key.public_key()))
        return {"keys": [{**jwk, "kid": self.kid, "use": "sig", "alg": ALGORITHM}]}

    def discovery(self) -> dict[str, Any]:
        return {
            "issuer": self.issuer,
            "jwks_uri": f"{self.issuer}/.well-known/jwks.json",
            "response_types_supported": ["id_token"],
            "subject_types_supported": ["public"],
            "id_token_signing_alg_values_supported": [ALGORITHM],
            "claims_supported": ["sub", "aud", "exp", "iat", "iss", "jti", "nbf"],
        }

    def issue(self, subject: str, audience: str, claims: dict[str, Any] | None = None,
              ttl: timedelta = timedelta(minutes=5)) -> str:
        now = clock.now()
        payload = {
            **(claims or {}),
            "iss": self.issuer,
            "sub": subject,
            "aud": audience,
            "iat": int(now.timestamp()),
            "nbf": int(now.timestamp()) - 5,
            "exp": int((now + ttl).timestamp()),
            "jti": uuid.uuid4().hex,
        }
        return jwt.encode(payload, self._key, algorithm=ALGORITHM, headers={"kid": self.kid})

    def verify(self, token: str, audience: str) -> dict[str, Any]:
        return jwt.decode(token, self._key.public_key(), algorithms=[ALGORITHM], audience=audience, issuer=self.issuer)

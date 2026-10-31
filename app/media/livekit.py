from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def create_livekit_token(api_key: str, api_secret: str, identity: str, room: str,
                         ttl_seconds: int = 3600) -> str:
    """Create a LiveKit access token (HS256 JWT) without the livekit SDK."""
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "iss": api_key,
        "sub": identity,
        "nbf": now - 5,
        "exp": now + ttl_seconds,
        "video": {"room": room, "roomJoin": True, "canPublish": True, "canSubscribe": True},
    }
    signing_input = ".".join(
        _b64url(json.dumps(part, separators=(",", ":")).encode("utf-8"))
        for part in (header, payload)
    )
    signature = hmac.new(api_secret.encode("utf-8"), signing_input.encode("ascii"),
                         hashlib.sha256).digest()
    return f"{signing_input}.{_b64url(signature)}"


def decode_payload(token: str) -> dict:
    part = token.split(".")[1]
    padded = part + "=" * (-len(part) % 4)
    return json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))

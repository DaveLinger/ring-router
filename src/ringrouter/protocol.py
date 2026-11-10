"""Pebble Index webhook protocol v1: signing, verification, multipart parsing.

Wire format (confirmed from th3walkingdud3/peeble-ring-router; re-check in phase 0
against real ring traffic):

  POST multipart/form-data
  fields:  transcription, recordedAt (unix ms), audio (m4a, ignored), client, test
  headers: X-Index-Webhook-Version: 1
           X-Index-Timestamp:       unix seconds
           X-Index-Delivery:        recording id, reused on every retry
           X-Index-Trigger:         single-click-hold | double-click-hold | test-event
           X-Index-Test:            "true" for test events
           X-Index-Signature:       hex HMAC-SHA256 over
                                    "v1\\n{ts}\\n{delivery}\\n{trigger}\\n{1|0}\\n" + raw body
"""

from __future__ import annotations

import email.parser
import email.policy
import hashlib
import hmac
import re
import time
import uuid
from dataclasses import dataclass

WEBHOOK_VERSION = "1"
RECORDING_TRIGGERS = {"single-click-hold", "double-click-hold"}
DELIVERY_RE = re.compile(r"[A-Za-z0-9._:-]{1,128}")
SIG_RE = re.compile(r"[0-9a-f]{64}")


def signature_input(timestamp: str, delivery_id: str, trigger: str, is_test: bool, body: bytes) -> bytes:
    head = f"v{WEBHOOK_VERSION}\n{timestamp}\n{delivery_id}\n{trigger}\n{'1' if is_test else '0'}\n"
    return head.encode("utf-8") + body


def sign(secret: str, timestamp: str, delivery_id: str, trigger: str, is_test: bool, body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"),
                    signature_input(timestamp, delivery_id, trigger, is_test, body),
                    hashlib.sha256).hexdigest()


def parse_multipart(content_type: str, body: bytes) -> dict[str, str]:
    msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
        b"Content-Type: " + content_type.encode("latin-1") + b"\r\n\r\n" + body)
    if not msg.is_multipart():
        raise ValueError("not multipart")
    fields: dict[str, str] = {}
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if name and name != "audio":  # audio is accepted but ignored
            fields[name] = (part.get_payload(decode=True) or b"").decode("utf-8", "replace")
    return fields


@dataclass
class Verified:
    delivery_id: str
    trigger: str
    is_test: bool
    timestamp: int


class RejectError(Exception):
    def __init__(self, status: int, reason: str):
        super().__init__(reason)
        self.status = status
        self.reason = reason


def verify(headers, body: bytes, secret: str, window_s: int = 300, now: float | None = None) -> Verified:
    """Check headers + signature. Raises RejectError(status, reason)."""
    get = headers.get
    version = get("X-Index-Webhook-Version", "")
    ts = get("X-Index-Timestamp", "")
    delivery_id = get("X-Index-Delivery", "")
    trigger = get("X-Index-Trigger", "")
    sig = get("X-Index-Signature", "")
    is_test = get("X-Index-Test", "").lower() == "true"
    if version != WEBHOOK_VERSION:
        raise RejectError(400, f"unsupported webhook version {version!r}")
    if not ts.isdigit() or not DELIVERY_RE.fullmatch(delivery_id) or not trigger:
        raise RejectError(400, "malformed headers")
    if not SIG_RE.fullmatch(sig) or not hmac.compare_digest(
            sig, sign(secret, ts, delivery_id, trigger, is_test, body)):
        raise RejectError(401, "bad signature")
    now = time.time() if now is None else now
    if abs(now - int(ts)) > window_s:
        raise RejectError(401, "timestamp expired")
    return Verified(delivery_id=delivery_id, trigger=trigger, is_test=is_test, timestamp=int(ts))


# ---------------------------------------------------------------- emulation --

def build_request(transcript: str | None, secret: str, *, trigger: str = "single-click-hold",
                  is_test: bool = False, delivery_id: str | None = None, ts: int | None = None,
                  recorded_at_ms: int | None = None, include_audio: bool = False,
                  bad_signature: bool = False) -> tuple[dict[str, str], bytes]:
    """Build (headers, body) exactly as the Pebble app would send them."""
    boundary = "----ringsim" + uuid.uuid4().hex
    ts = int(time.time()) if ts is None else ts
    delivery_id = delivery_id or str(uuid.uuid4())
    recorded_at_ms = recorded_at_ms or ts * 1000
    parts: list[bytes] = []

    def field(name: str, value: str) -> None:
        parts.append((f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n"
                      f"{value}\r\n").encode("utf-8"))

    if transcript is not None:
        field("transcription", transcript)
    field("recordedAt", str(recorded_at_ms))
    field("client", "ring")
    if is_test:
        field("test", "true")
    if include_audio:
        parts.append((f"--{boundary}\r\nContent-Disposition: form-data; name=\"audio\"; "
                      f"filename=\"rec.m4a\"\r\nContent-Type: audio/mp4\r\n\r\n").encode() + b"\x00" * 64 + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join(parts)
    sig = sign(secret, str(ts), delivery_id, trigger, is_test, body)
    if bad_signature:
        sig = "0" * 64
    headers = {
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "X-Index-Webhook-Version": WEBHOOK_VERSION,
        "X-Index-Timestamp": str(ts),
        "X-Index-Delivery": delivery_id,
        "X-Index-Trigger": trigger,
        "X-Index-Test": "true" if is_test else "false",
        "X-Index-Signature": sig,
    }
    return headers, body

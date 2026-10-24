"""Versioned signal-bus message contract (schema_version 1).

Wire format: ZeroMQ multipart ``[topic, json_utf8]``; topic defaults to ``vs.v1``.

Sequencing convention:
  * ``publisher_id`` is a random id chosen when a publisher process starts. A change of
    publisher_id means the monitor restarted; receivers must treat it as a discontinuity.
  * ``seq`` starts at 1 and increments by exactly 1 for EVERY message of ANY kind (STATE,
    HEARTBEAT, CONTROL) from that publisher. Receivers can therefore detect gaps,
    duplicates and reordering on any message.
  * EVERY message carries a complete current-state snapshot, so a heartbeat alone is enough
    to recover from a missed STATE update (PUB/SUB does not guarantee delivery).

Timestamps:
  * ``ts`` is the UTC wall-clock emission time (used by clients for freshness checks).
  * ``as_of`` is the data time the assessment refers to (simulated time during replay).
"""

from __future__ import annotations

import json
import math
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, field_validator

from vetosentinel.models import RiskState
from vetosentinel.timeutil import parse_iso_utc

SCHEMA_VERSION = 1
MAX_MESSAGE_BYTES = 64 * 1024


class Kind(StrEnum):
    STATE = "STATE"
    HEARTBEAT = "HEARTBEAT"
    CONTROL = "CONTROL"


class TopHeadline(BaseModel):
    model_config = ConfigDict(extra="ignore")
    headline_id: str = Field(max_length=64)
    headline: str = Field(max_length=500)
    source: str = Field(max_length=200)


class Components(BaseModel):
    model_config = ConfigDict(extra="ignore")
    panic_z: float | None
    drift_z: float | None
    volume_z: float | None

    @field_validator("panic_z", "drift_z", "volume_z")
    @classmethod
    def _finite(cls, v: float | None) -> float | None:
        if v is not None and not math.isfinite(v):
            raise ValueError("non-finite component")
        return v


class BusMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    schema_version: Literal[1]
    publisher_id: str = Field(min_length=1, max_length=64)
    seq: StrictInt = Field(ge=1)
    kind: Kind
    ts: datetime
    as_of: datetime | None
    state: RiskState
    risk_score: float | None
    components: Components
    reason: str = Field(max_length=2000)
    degraded: StrictBool
    degraded_reasons: list[str] = Field(max_length=20)
    top_headlines: list[TopHeadline] = Field(max_length=20)
    monitor_ready: StrictBool
    heartbeat_interval_ms: StrictInt = Field(gt=0)
    heartbeat_timeout_ms: StrictInt = Field(gt=0)
    data_origin: str = Field(max_length=200)
    implementations: dict[str, str]
    fallback_in_use: StrictBool
    control: dict[str, Any] | None = None

    @field_validator("ts", "as_of", mode="before")
    @classmethod
    def _parse_ts(cls, v: Any) -> Any:
        if v is None or isinstance(v, datetime):
            return v
        if not isinstance(v, str):
            raise ValueError("timestamp must be an ISO-8601 string")
        return parse_iso_utc(v)  # rejects naive timestamps

    @field_validator("risk_score")
    @classmethod
    def _finite(cls, v: float | None) -> float | None:
        if v is not None and not math.isfinite(v):
            raise ValueError("risk_score must be finite")
        return v


def decode(payload: bytes) -> BusMessage:
    """Validate a raw payload. Raises ValueError on ANY problem (callers must fail safe)."""
    if len(payload) > MAX_MESSAGE_BYTES:
        raise ValueError(f"message too large ({len(payload)} bytes)")
    try:
        obj = json.loads(payload.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ValueError(f"undecodable payload: {e}") from e
    if not isinstance(obj, dict):
        raise ValueError("payload is not a JSON object")
    try:
        return BusMessage.model_validate(obj)
    except Exception as e:  # pydantic.ValidationError
        raise ValueError(f"schema validation failed: {e}") from e


def _reject_constant(name: str) -> float:
    raise ValueError(f"non-finite JSON constant {name} not allowed")


def encode(body: dict[str, Any]) -> bytes:
    data = json.dumps(body, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(data) > MAX_MESSAGE_BYTES:
        raise ValueError("encoded message exceeds MAX_MESSAGE_BYTES")
    return data

"""Peewee models.  All timestamps are integer Unix epochs in UTC."""

from __future__ import annotations

import json
from typing import Any

from peewee import (
    BooleanField,
    CharField,
    FloatField,
    ForeignKeyField,
    IntegerField,
    Model,
    TextField,
)

from app.db import db
from app.timeutil import now_ts

# Check.status values
STATUS_OK = "ok"
STATUS_FAIL = "fail"
STATUS_UNKNOWN = "unknown"


class BaseModel(Model):
    class Meta:
        database = db


class Admin(BaseModel):
    username = CharField(unique=True, max_length=64)
    password_hash = CharField(max_length=200)
    # Bumped on password change; sessions carrying an older value are rejected.
    password_changed_at = IntegerField(default=now_ts)
    created_at = IntegerField(default=now_ts)

    class Meta:
        table_name = "admins"


class Subscription(BaseModel):
    name = CharField(max_length=200, default="")
    url = TextField()
    interval_minutes = IntegerField(default=360)
    auto_sync = BooleanField(default=True)
    make_public = BooleanField(default=True)
    last_sync = IntegerField(null=True)
    last_error = TextField(null=True)
    last_count = IntegerField(default=0)
    created_at = IntegerField(default=now_ts)

    class Meta:
        table_name = "subscriptions"


class Server(BaseModel):
    name = CharField(max_length=200)
    raw_link = TextField()
    protocol = CharField(max_length=16)
    transport = CharField(max_length=16, default="tcp")
    security = CharField(max_length=16, default="none")
    address = CharField(max_length=255)
    port = IntegerField()
    parsed_outbound = TextField()  # Xray outbound JSON (without tag)
    identity = CharField(max_length=64, index=True)  # protocol+address+port+credential hash
    country = CharField(max_length=2, default="")
    tags = CharField(max_length=500, default="")  # comma separated
    is_active = BooleanField(default=True, index=True)
    is_public = BooleanField(default=True)
    sort_order = IntegerField(default=0, index=True)
    socks_port = IntegerField(null=True, unique=True)  # stable local SOCKS inbound port
    subscription = ForeignKeyField(Subscription, null=True, backref="servers", on_delete="SET NULL")
    xray_error = TextField(null=True)  # set when Xray rejects this outbound
    created_at = IntegerField(default=now_ts)
    updated_at = IntegerField(default=now_ts)

    class Meta:
        table_name = "servers"

    @property
    def outbound(self) -> dict[str, Any]:
        return json.loads(self.parsed_outbound)

    @property
    def tag_list(self) -> list[str]:
        return [t.strip() for t in self.tags.split(",") if t.strip()]


class Check(BaseModel):
    """Raw check result (kept for ``raw_retention_hours``)."""

    server = ForeignKeyField(Server, backref="checks", on_delete="CASCADE")
    checked_at = IntegerField(index=True)
    status = CharField(max_length=8)  # ok / fail / unknown
    latency_ms = IntegerField(null=True)
    error_type = CharField(max_length=32, null=True)
    error_detail = CharField(max_length=300, null=True)

    class Meta:
        table_name = "checks"
        indexes = ((("server", "checked_at"), False),)


class HourlyStat(BaseModel):
    """Aggregated checks per server per UTC hour (kept ``hourly_retention_days``)."""

    server = ForeignKeyField(Server, backref="hourly", on_delete="CASCADE")
    hour = IntegerField()  # epoch of the hour start
    total = IntegerField(default=0)
    success = IntegerField(default=0)
    fail = IntegerField(default=0)
    unknown = IntegerField(default=0)
    # Sums allow exact averages / standard deviation across many hours.
    latency_count = IntegerField(default=0)
    latency_sum = FloatField(default=0)
    latency_sq_sum = FloatField(default=0)
    avg_latency = FloatField(null=True)
    min_latency = IntegerField(null=True)
    max_latency = IntegerField(null=True)
    p95_latency = IntegerField(null=True)

    class Meta:
        table_name = "hourly_stats"
        indexes = ((("server", "hour"), True),)


class Setting(BaseModel):
    key = CharField(primary_key=True, max_length=64)
    value = TextField()

    class Meta:
        table_name = "settings"


class Incident(BaseModel):
    """One outage: opened after N failed checks, closed after M successes."""

    server = ForeignKeyField(Server, backref="incidents", on_delete="CASCADE")
    started_at = IntegerField(index=True)
    ended_at = IntegerField(null=True, index=True)
    duration_seconds = IntegerField(null=True)
    error_type = CharField(max_length=32, null=True)

    class Meta:
        table_name = "incidents"
        indexes = ((("server", "started_at"), False),)


class TelegramOutbox(BaseModel):
    """Queued Telegram message, one row per recipient chat."""

    event_type = CharField(max_length=32)
    chat_id = CharField(max_length=32)
    text = TextField()
    created_at = IntegerField(index=True)  # real time of the event
    sent_at = IntegerField(null=True, index=True)
    attempts = IntegerField(default=0)
    next_attempt_at = IntegerField(default=0, index=True)
    last_error = TextField(null=True)
    status = CharField(max_length=10, default="pending", index=True)  # pending / sent / failed
    via = CharField(max_length=200, null=True)
    exclude_server_id = IntegerField(null=True)  # never send through this server

    class Meta:
        table_name = "telegram_outbox"


ALL_MODELS: list[type[BaseModel]] = [
    Admin,
    Subscription,
    Server,
    Check,
    HourlyStat,
    Setting,
    Incident,
    TelegramOutbox,
]

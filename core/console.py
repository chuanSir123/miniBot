"""Bounded, thread-safe, immediately flushed runtime events."""

import datetime
import json
import re
import threading

_lock = threading.Lock()


def clean(value):
    if isinstance(value, dict):
        return {
            str(k): (
                "[redacted]"
                if any(
                    x in str(k).lower()
                    for x in (
                        "api_key",
                        "access_token",
                        "authorization",
                        "password",
                        "secret",
                    )
                )
                else clean(v)
            )
            for k, v in value.items()
            if not str(k).startswith("_")
        }
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value[:20]]
    if isinstance(value, bytes):
        return f"<bytes:{len(value)}>"
    if isinstance(value, str):
        value = re.sub(r"as_sk_[A-Za-z0-9_-]+|sk-[A-Za-z0-9_-]{8,}|Bearer\s+\S+", "[redacted]", value)
        value = re.sub(r"data:image/[^;]+;base64,[A-Za-z0-9+/=]+", "[image]", value)
        return value[:1200] + ("…" if len(value) > 1200 else "")
    return value


def log(component, event, **fields):
    line = json.dumps(clean(fields), ensure_ascii=False, default=str)
    with _lock:
        print(
            f"{datetime.datetime.now():%H:%M:%S} [{component}] {event} {line}",
            flush=True,
        )

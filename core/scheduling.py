"""Persistent schedules in the machine's local timezone; missed runs coalesce."""
import datetime as dt
import math
import re


def next_trigger(rule, previous, now):
    if not rule:
        return None
    match = re.fullmatch(r"interval:(\d+)", rule)
    if match:
        seconds = int(match[1])
        if seconds < 60:
            raise ValueError("重复间隔至少60秒")
        return previous + max(1, math.floor((now - previous) / seconds) + 1) * seconds
    match = re.fullmatch(r"(daily|weekdays|weekly:[0-6]) (\d{2}):(\d{2})", rule)
    if not match:
        raise ValueError("重复规则支持 daily HH:MM、weekdays HH:MM、weekly:0 HH:MM（周一为0）、interval:秒")
    kind, hour, minute = match.groups()
    if int(hour) > 23 or int(minute) > 59:
        raise ValueError("无效重复时刻")
    base = dt.datetime.fromtimestamp(now)
    for offset in range(8):
        candidate = (base + dt.timedelta(days=offset)).replace(hour=int(hour), minute=int(minute), second=0, microsecond=0)
        if kind == "weekdays" and candidate.weekday() >= 5:
            continue
        if kind.startswith("weekly:") and candidate.weekday() != int(kind[-1]):
            continue
        if candidate.timestamp() > now:
            return candidate.timestamp()
    raise ValueError("无法计算下次执行时间")

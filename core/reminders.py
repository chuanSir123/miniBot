"""Deterministic common reminder commands; complex dates fall back to the model."""

import datetime
import re
import time


def parse_reminder(text, now=None):
    now = time.time() if now is None else now
    text = text.strip()
    match = re.fullmatch(
        r"(?:请)?(?:在)?(\d+(?:\.\d+)?)\s*(秒|分钟|分|小时|天)(?:后|之后)[，, ]*(?:提醒我|提醒)[：:，, ]*(.+)",
        text,
    )
    if match:
        amount, unit, message = match.groups()
        seconds = (
            float(amount)
            * {"秒": 1, "分": 60, "分钟": 60, "小时": 3600, "天": 86400}[unit]
        )
        if 0 < seconds <= 366 * 86400:
            return now + seconds, message
        raise ValueError("提醒时间须在未来一年以内")
    match = re.fullmatch(
        r"(今天|明天)?\s*(\d{1,2})(?::|：|点)(\d{1,2})?(?:分)?[，, ]*(?:提醒我|提醒)[：:，, ]*(.+)",
        text,
    )
    if match:
        day, hour, minute, message = match.groups()
        hour = int(hour)
        minute = int(minute or 0)
        if not 0 <= hour < 24 or not 0 <= minute < 60:
            raise ValueError("无效时刻")
        current = datetime.datetime.fromtimestamp(now)
        due = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if day == "明天":
            due += datetime.timedelta(days=1)
        elif due.timestamp() <= now:
            if day == "今天":
                raise ValueError("今天的该时间已经过去")
            due += datetime.timedelta(days=1)
        return due.timestamp(), message
    return None

"""UTC 时间解析与格式化工具。

契约要求：时间均带时区并以 UTC 记录，区间左闭右开。
"""

from datetime import datetime, timezone


def parse_iso(value: str) -> datetime:
    """解析 ISO 8601 时间字符串，返回带 UTC 时区的 datetime。"""
    if not isinstance(value, str) or not value:
        raise ValueError(f"时间格式非法：{value!r}")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        raise ValueError(f"时间缺少时区信息：{value!r}")
    return dt.astimezone(timezone.utc)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    """格式化为以 Z 结尾的 UTC 字符串。"""
    if dt.tzinfo is None:
        raise ValueError("datetime 必须带时区")
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def add_minutes(dt: datetime, minutes: float) -> datetime:
    from datetime import timedelta

    return dt + timedelta(minutes=int(minutes))

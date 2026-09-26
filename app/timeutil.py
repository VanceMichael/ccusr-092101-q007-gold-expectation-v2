"""时间工具：统一使用带偏移量的 ISO 8601 字符串。"""

from datetime import datetime, timezone


def parse_iso8601(value: object, field: str = "time") -> datetime:
    """解析带时区偏移量的 ISO 8601 字符串；拒绝缺失偏移量的输入。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 必须是带时区偏移量的 ISO 8601 字符串")
    text = value.strip()
    normalized = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        raise ValueError(f"{field} 不是合法的 ISO 8601 时间：{value}")
    if parsed.tzinfo is None:
        raise ValueError(f"{field} 必须携带时区偏移量：{value}")
    return parsed


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

from __future__ import annotations


def format_tokens(value: int) -> str:
    """把 token 数格式化为 K/M 单位，如 64000 → 64K、1000000 → 1M。"""
    if value >= 1_000_000:
        return f"{value / 1_000_000:g}M"
    if value >= 1000:
        return f"{value / 1000:g}K"
    return str(value)

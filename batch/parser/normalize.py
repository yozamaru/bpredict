"""空値を0と区別し、日時はタイムゾーンを明示して変換する。"""

import math
import re
import unicodedata
from datetime import UTC, datetime

from .errors import ParseError, ValidationError

MISSING = {"", "-", "DNP"}


def text(value: object, *, optional: bool = False) -> str | None:
    if value is None:
        if optional:
            return None
        raise ParseError("文字列がない")
    if not isinstance(value, str):
        raise ParseError("文字列でない")
    result = value.strip()
    if not result:
        if optional:
            return None
        raise ParseError("文字列が空")
    return result


def source_id(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ParseError("公式IDの型が不正")
    result = str(value).strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", result):
        raise ParseError("公式IDの形式が不正")
    return result


def integer(value: object, *, minimum: int = 0, maximum: int = 250) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ParseError("真偽値はカウント値でない")
    if isinstance(value, str):
        normalized = unicodedata.normalize("NFKC", value).strip()
        if normalized.upper() in MISSING:
            return None
        # カンマの誤配置（1,2など）を黙って整数にしない。
        if not re.fullmatch(r"[+-]?(?:\d+|\d{1,3}(?:,\d{3})+)", normalized):
            raise ParseError("整数の形式が不正")
        result = int(normalized.replace(",", ""))
    elif isinstance(value, int):
        result = value
    elif isinstance(value, float) and math.isfinite(value) and value.is_integer():
        result = int(value)
    else:
        raise ParseError("整数の型が不正")
    if not minimum <= result <= maximum:
        raise ValidationError("整数が値域外")
    return result


def required_integer(value: object, *, minimum: int = 0, maximum: int = 250) -> int:
    result = integer(value, minimum=minimum, maximum=maximum)
    if result is None:
        raise ParseError("必須の整数が空")
    return result


def flag(value: object) -> int | None:
    if isinstance(value, bool):
        return int(value)
    return integer(value, maximum=1)


def minutes(value: object) -> float | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ParseError("出場時間の形式が不正")
    normalized = unicodedata.normalize("NFKC", value).strip()
    if normalized.upper() in MISSING:
        return None
    match = re.fullmatch(r"(\d{1,3}):([0-5]\d)", normalized)
    if not match:
        raise ParseError("出場時間はMM:SSでない")
    seconds = int(match[1]) * 60 + int(match[2])
    if seconds > 3600:
        raise ValidationError("選手の出場時間が60分を超える")
    return seconds / 60


def timestamp(value: object) -> datetime:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ParseError("日時の型が不正")
    string = str(value).strip()
    net = re.fullmatch(r"/Date\((\d+)(?:[+-]\d{4})?\)/", string)
    if net:
        # .NET形式の値はUTCのミリ秒。後ろのオフセットを二重適用しない。
        seconds = int(net[1]) / 1000
    elif re.fullmatch(r"\d{9,10}", string):
        seconds = float(string)
    else:
        raise ParseError("未対応の日時形式")
    try:
        return datetime.fromtimestamp(seconds, UTC)
    except (OverflowError, OSError, ValueError):
        raise ValidationError("日時が値域外") from None


def iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")

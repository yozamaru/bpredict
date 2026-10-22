"""空値を0と区別し、日時はタイムゾーンを明示して変換する。"""

import math
import re
import unicodedata
from datetime import UTC, datetime

from .errors import ParseError, ValidationError

MISSING = {"", "-", "DNP"}


def text(value: object, *, optional: bool = False) -> str | None:
    """文字列を取り出す。**空文字は「無い」として扱う。**

    サイトは値が無いとき `null` ではなく空文字を返すことがある（詳細設計 4.4 の
    正規化3）。`optional=True` のときだけ None を返し、そうでなければ落とす —
    **必須の項目が静かに空で通ると、欠損が0として下流へ流れる。**
    """
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
    """公式サイトのIDを文字列へ正規化する。

    **型がシーズンで違う。** `StadiumCD` は 2016-17 が整数 `3`、2025-26 が文字列
    `"169"` である（詳細設計 1.2 / 4.4 の正規化5）。正規化しないと同じ会場が
    `3` と `"3"` の2行に分裂し、`venues` が壊れる。

    **真偽値を先に弾く。** Python では `isinstance(True, int)` が真であり、
    弾かないと `True` が `"True"` というIDになる。
    """
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ParseError("公式IDの型が不正")
    result = str(value).strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", result):
        raise ParseError("公式IDの形式が不正")
    return result


def integer(value: object, *, minimum: int = 0, maximum: int = 250) -> int | None:
    """カウント値を整数へ正規化する。**空値と 0 を区別する。**

    `""` / `"-"` / `"DNP"` は None を返す（出場なし・記録なし）。**0 にしない** —
    「記録なし」が「0回」になると、平均の分母に入って集計が狂う（詳細設計 4.4 の
    正規化3）。全角数字と桁区切りのカンマも受ける。

    **値域を外れたら `ValidationError` を投げる**（既定 0〜250）。スクレイピングは
    入力が信用できないパイプラインであり、異常値が Elo に流れ込むとレーティング
    全体が壊れる（基本設計 2.1）。形式の誤りは `ParseError` で、両者を混ぜない —
    前者はデータの欠陥、後者は構造変更の疑いである。

    **カンマの誤配置を黙って通さない。** `1,2` は `12` ではなく形式の誤りとして扱う。
    """
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
    """`integer` と同じだが、空を許さない。

    恒等式の検証に使う項目（得点・試投数）はここを通す。**None のまま下流へ渡すと、
    `pts = 2fgm×2 + 3fgm×3 + ftm` の照合が「比べられないので通った」ことになる。**
    """
    result = integer(value, minimum=minimum, maximum=maximum)
    if result is None:
        raise ParseError("必須の整数が空")
    return result


def flag(value: object) -> int | None:
    """0 / 1 の旗を正規化する。真偽値も受ける。

    DDL の `CHECK (... IN (0,1))` に合わせて 0〜1 に絞る（詳細設計 1.3）。
    """
    if isinstance(value, bool):
        return int(value)
    return integer(value, maximum=1)


def minutes(value: object) -> float | None:
    """出場時間（`"MM:SS"`）を分の実数へ変換する。

    **サイトは分を小数で返さない。** `"31:12"` のような文字列であり、素朴に
    `float()` へ通すと落ちる（詳細設計 4.4 の対応表）。`"DNP"` は None を返す。

    **60分を超えたら `ValidationError`。** 上限の出典は
    `player_game_stats.minutes` の CHECK（0〜60。詳細設計 1.3）であり、ここは
    DDL に入らない値を先に止めているだけである。**基準をこちらで作っていない。**
    """
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
    """試合開始時刻を UTC の `datetime` へ変換する。

    **.NET 形式のオフセットを二重に適用しない。** サイトは
    `/Date(1474538700000+0900)/` を返すが、**ミリ秒の側がすでに UTC である**。
    後ろの `+0900` を足すと9時間ずれ、`game_date`（JST の暦日）が日によって
    前後する（CLAUDE.md 時刻の扱い）。10桁の秒も受ける。
    """
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
    """`datetime` を DB に入れる書式へ変換する。

    **DB の全時刻列は UTC の ISO 8601 である**（CLAUDE.md 時刻の扱い）。
    `+00:00` を `Z` に置き換え、秒までで切る。

    **書式のゆれが関門を壊す。** 時刻は TEXT 列であり、比較は辞書順になる —
    `POST /internal/predictions` の tipoff ガード
    （`api/src/routes/internal/predictions.ts`）と `finalize` の
    `WHERE g.tipoff_at <= ?`（同 `finalize.ts`）がこの列を直接比べている。
    **秒の精度を変えると、どちらも静かに判定が変わる。**
    """
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")

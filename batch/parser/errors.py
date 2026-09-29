"""本文・URL・選手情報を例外メッセージに含めない。"""

from dataclasses import dataclass


class ParseError(ValueError):
    """必須キー、型、構造が想定と異なる。"""


class ValidationError(ValueError):
    """値域・恒等式が成立しない。"""


class OutOfScopeError(ValueError):
    """取り込みの対象外の試合。**データの欠陥ではない。**

    `ValidationError` と区別するのは、(a) `不正` ではなく `非リーグ戦` に数える
    ため、(b) 連続失敗の打ち切り counter に入れないためである。再実行すれば直る
    ものと、永久に対象外のものを混ぜない。対象外の試合が3つ並んだだけで
    シーズンが止まってもいけない。

    判定の入口は2つあるが（チーム / 大会区分）、**扱いは1つ**である。
    """


class UnknownClubError(OutOfScopeError):
    """`club_source_ids` で解決できない `TeamID` の試合。

    要件 5.3 の絞り込み3段目。選抜チーム・海外クラブ・下位リーグが該当する。
    """


class OutOfScopeCompetitionError(OutOfScopeError):
    """日程の大会区分と、試合詳細の `Event` が食い違う試合。

    `event=2`（そのシーズンの日程）にはリーグ戦でない試合が混ざっている
    （要件 5.3）。2023-24 の `502494` はオールスターの「アジアライジング
    スターゲーム」で、`event=2` に出ていながら試合詳細は別の区分を返していた。

    **日程側の区分を正としない。** 試合詳細の `Event` は公式が試合ごとに返して
    いる値であり、日程の一覧より細かい。食い違ったら取り込まない。
    """


class DataUnavailable(ParseError):
    """認識した試合ページに終了済み記録がまだない。"""


class ParseErrorStreak(ParseError):
    """パース失敗が連続3件。取得区間を中止する。"""


@dataclass
class ParseFailureTracker:
    consecutive: int = 0

    def success(self) -> None:
        self.consecutive = 0

    def failure(self) -> None:
        self.consecutive += 1
        if self.consecutive >= 3:
            raise ParseErrorStreak("パース失敗が連続3件")

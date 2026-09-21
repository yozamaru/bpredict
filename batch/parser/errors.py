"""本文・URL・選手情報を例外メッセージに含めない。"""

from dataclasses import dataclass


class ParseError(ValueError):
    """必須キー、型、構造が想定と異なる。"""


class ValidationError(ValueError):
    """値域・恒等式が成立しない。"""


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

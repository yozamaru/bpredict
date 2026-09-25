"""静的JSON の組み立てと書き出し（詳細設計 3.7）。

**主要導線は Workers も D1 も経由させない**（要件 4.2）。ここが書き出したファイルを
Pages が静的アセットとして配信する。無料枠が枯れた日でも当日の予測が表示され続ける。

**スキーマは公開APIの応答と同一である。** 専用のスキーマを作らない。キー構造は
`contracts/public-shapes.json` に固定し、両側のテストが読む（詳細設計 3.7）。
"""

from batch.static_json.builder import (
    GameDetailInput,
    GameListInput,
    MetaInput,
    build_game_detail,
    build_game_list,
    build_meta,
)
from batch.static_json.writer import WriteResult, write_static_json

__all__ = [
    "GameDetailInput",
    "GameListInput",
    "MetaInput",
    "WriteResult",
    "build_game_detail",
    "build_game_list",
    "build_meta",
    "write_static_json",
]

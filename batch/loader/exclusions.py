"""学習データに取り込まない試合の一覧（要件 5.3）。

**不戦敗が該当する。** 公式は試合数に計上するが実際にはプレーしておらず、得点差を
そのまま Elo に通すと実力差の証拠がないままレーティングが動く。

**一覧はリポジトリに残す。** 後で画面に「これらの試合は学習データに含めていない」と
注釈するための唯一の出典になる。`batch/snapshot/` には置かない — あちらは
**学習入力の唯一の源**であり（絶対ルール3）、入力でないファイルを混ぜると意味が濁る。
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from batch.parser.schedule_parser import ExcludedGame

DEFAULT_PATH = Path("batch/exclusions/excluded_games.json")


def load(path: Path = DEFAULT_PATH) -> list[dict[str, object]]:
    """既存の一覧を読む。無ければ空。"""
    if not path.exists():
        return []
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, list):
        # ファイルの形式が壊れている。**黙って空として扱わない** — 一度除外した
        # 事実が静かに消える（noqa: 引数の型ではなくファイル形式の誤りである）
        raise ValueError("除外一覧の形式が不正")  # noqa: TRY004
    return [row for row in loaded if isinstance(row, dict)]


def merge(
    existing: list[dict[str, object]], found: list[ExcludedGame], *, season_id: str,
) -> list[dict[str, object]]:
    """試合IDで重ねる。**何度流しても同じ結果になる**（冪等）。

    同じ試合を再び見つけたら新しい観察で置き換える。サイトの表示が直った場合に
    古い記録が残り続けないようにするためで、消えた場合は**残す** — 一度
    除外した事実を、次の実行が黙って消してはならない。
    """
    by_id = {str(row.get("gameId")): dict(row) for row in existing}
    for game in found:
        row = asdict(game)
        by_id[game.game_id] = {
            "gameId": row.pop("game_id"),
            "seasonId": season_id,
            "gameDate": row.pop("game_date"),
            "home": row.pop("home_name"),
            "away": row.pop("away_name"),
            "homeScore": row.pop("home_score"),
            "awayScore": row.pop("away_score"),
            "reason": row.pop("reason"),
        }
    return [by_id[key] for key in sorted(by_id)]


def save(rows: list[dict[str, object]], path: Path = DEFAULT_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(rows, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
        encoding="utf-8",
    )

"""学習データに取り込まない試合の一覧（要件 5.3 / `batch/loader/exclusions.py`）。

**一度除外した事実を、次の実行が黙って消してはならない。** 後で画面に
「これらの試合は学習データに含めていない」と注釈するための唯一の出典になる。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from batch.loader import exclusions
from batch.parser.schedule_parser import ExcludedGame


def a_game(game_id: str = "500609", **over: object) -> ExcludedGame:
    fields: dict[str, object] = {
        "game_id": game_id,
        "game_date": "2023-04-12",
        "home_name": "架空ホーム",
        "away_name": "架空アウェイ",
        "home_score": "20",
        "away_score": "0",
        "reason": "状態欄が空で得点がある（不戦敗の形）",
    }
    fields.update(over)
    return ExcludedGame(**fields)  # type: ignore[arg-type]


def test_merge_records_what_was_observed() -> None:
    rows = exclusions.merge([], [a_game()], season_id="2022-23-B1")
    assert rows == [{
        "gameId": "500609",
        "seasonId": "2022-23-B1",
        "gameDate": "2023-04-12",
        "home": "架空ホーム",
        "away": "架空アウェイ",
        "homeScore": "20",
        "awayScore": "0",
        "reason": "状態欄が空で得点がある（不戦敗の形）",
    }]


def test_merge_is_idempotent() -> None:
    """**何度流しても同じ結果になる。** backfill は再開のたびに日程を読み直す。"""
    once = exclusions.merge([], [a_game()], season_id="2022-23-B1")
    twice = exclusions.merge(once, [a_game()], season_id="2022-23-B1")
    assert once == twice


def test_merge_keeps_rows_that_are_no_longer_found() -> None:
    """**一度除外した事実を消さない。**

    サイトの表示が変わって検出されなくなっても、除外した記録は残す。
    消えると「なぜこの試合が無いのか」を後から説明できない。
    """
    existing = exclusions.merge([], [a_game("111")], season_id="2021-22-B1")
    rows = exclusions.merge(existing, [a_game("222")], season_id="2022-23-B1")
    assert [row["gameId"] for row in rows] == ["111", "222"]


def test_merge_replaces_a_row_when_the_observation_changes() -> None:
    existing = exclusions.merge([], [a_game(home_score="20")], season_id="2022-23-B1")
    rows = exclusions.merge(existing, [a_game(home_score="99")], season_id="2022-23-B1")
    assert len(rows) == 1
    assert rows[0]["homeScore"] == "99"


def test_save_and_load_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "excluded_games.json"
    rows = exclusions.merge([], [a_game()], season_id="2022-23-B1")
    exclusions.save(rows, path)
    assert exclusions.load(path) == rows
    # 日本語をエスケープしない（差分が読めなくなる）
    assert "架空ホーム" in path.read_text(encoding="utf-8")


def test_load_without_the_file_is_empty(tmp_path: Path) -> None:
    assert exclusions.load(tmp_path / "missing.json") == []


def test_broken_file_is_not_silently_treated_as_empty(tmp_path: Path) -> None:
    """**黙って空として扱わない。** 一度除外した事実が静かに消える。"""
    path = tmp_path / "excluded_games.json"
    path.write_text(json.dumps({"not": "a list"}), encoding="utf-8")
    with pytest.raises(ValueError, match="形式"):
        exclusions.load(path)

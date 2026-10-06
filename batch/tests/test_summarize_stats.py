"""実績の集計（詳細設計 4.14 / 6.4.2。要件 A-19）。

**出すのは集計値だけである**（要件 3.1.1）。試合ごとの記録が1行として入らない。
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from batch.jobs import summarize_stats as job
from batch.tests.test_static_json import contract, key_paths

COUNTS = {
    "fg2m": 4, "fg2a": 8, "fg3m": 2, "fg3a": 5, "ftm": 3, "fta": 4,
    "oreb": 1, "dreb": 3, "ast": 5, "tov": 2, "stl": 1, "blk": 0,
    "pf": 2, "fd": 3,
}


def games(rows: list[dict[str, object]]) -> pd.DataFrame:
    return pd.DataFrame(rows)


def _game(game_id: str, season_id: str, *, status: str = "FINISHED",
          home: int = 80, away: int = 70) -> dict[str, object]:
    return {
        "id": game_id, "season_id": season_id, "status": status,
        "home_score": home, "away_score": away,
    }


def _player_row(game_id: str, player_id: str, club_id: str,
                **over: object) -> dict[str, object]:
    row: dict[str, object] = {
        "game_id": game_id, "player_id": player_id, "club_id": club_id,
        "started": 1, "minutes": 30.0, "pts": 4 * 2 + 2 * 3 + 3,
        **COUNTS,
    }
    row.update(over)
    return row


def _team_game(game_id: str, club_id: str, season_id: str, *,
               is_home: int = 1, result: int | None = 1) -> dict[str, object]:
    return {
        "game_id": game_id, "club_id": club_id, "season_id": season_id,
        "is_home": is_home, "result": result,
    }


def _team_stat(game_id: str, club_id: str, **over: object) -> dict[str, object]:
    row: dict[str, object] = {"game_id": game_id, "club_id": club_id, **COUNTS}
    row.update(over)
    return row


# --- 選手 ---


def test_rows_are_aggregates_not_single_games() -> None:
    """**1試合の記録が1行として入らない**（要件 3.1.1）。

    `scope` は 'SEASON' / 'CAREER' の2つだけで、`scope_key` に試合IDが入らない。
    """
    finished = job._finished(games([_game("g1", "s1"), _game("g2", "s1")]))
    stats = pd.DataFrame([
        _player_row("g1", "p1", "c1"), _player_row("g2", "p1", "c1"),
    ])
    rows = job.summarize_players(stats, finished)
    assert {r.scope for r in rows} == {"SEASON", "CAREER"}
    assert not any(r.scope_key in {"g1", "g2"} for r in rows)
    # 2試合が1行の季と1行の通算に畳まれる
    assert len(rows) == 2
    assert all(r.games == 2 for r in rows)


def test_career_rows_have_no_club() -> None:
    """CAREER の行は `scope_key` と `club_id` が空文字。**NULL にしない**（1.9）。"""
    finished = job._finished(games([_game("g1", "s1")]))
    rows = job.summarize_players(pd.DataFrame([_player_row("g1", "p1", "c1")]), finished)
    career = [r for r in rows if r.scope == "CAREER"]
    assert len(career) == 1
    assert career[0].scope_key == ""
    assert career[0].club_id == ""


def test_season_rows_have_a_club() -> None:
    """SEASON の行はクラブを持つ。季中の移籍が2行で出る前提が崩れる。"""
    finished = job._finished(games([_game("g1", "s1"), _game("g2", "s1")]))
    stats = pd.DataFrame([
        _player_row("g1", "p1", "c1"), _player_row("g2", "p1", "c2"),
    ])
    rows = job.summarize_players(stats, finished)
    seasons = sorted(r.club_id for r in rows if r.scope == "SEASON")
    assert seasons == ["c1", "c2"]
    # 通算は1行で、2クラブ分の合計になる
    career = [r for r in rows if r.scope == "CAREER"]
    assert len(career) == 1
    assert career[0].games == 2


def test_season_rows_sum_to_the_career_row() -> None:
    """季の行の合計が通算の行と一致すること。

    **クラブ別に持つため、季の行は1季に2行以上ありうる**（1.9）。
    """
    finished = job._finished(games([
        _game("g1", "s1"), _game("g2", "s1"), _game("g3", "s2"),
    ]))
    stats = pd.DataFrame([
        _player_row("g1", "p1", "c1"), _player_row("g2", "p1", "c2"),
        _player_row("g3", "p1", "c2"),
    ])
    rows = job.summarize_players(stats, finished)
    career = next(r for r in rows if r.scope == "CAREER")
    seasons = [r for r in rows if r.scope == "SEASON"]
    assert sum(r.games for r in seasons) == career.games
    assert sum(r.pts for r in seasons) == career.pts
    for key in COUNTS:
        assert sum(r.counts[key] for r in seasons) == career.counts[key], key


def test_only_finished_games_are_counted() -> None:
    """`SCHEDULED` / `POSTPONED` / `CANCELLED` が母数に入らないこと（1.9.1）。"""
    rows = games([
        _game("g1", "s1"),
        _game("g2", "s1", status="SCHEDULED"),
        _game("g3", "s1", status="POSTPONED"),
        _game("g4", "s1", status="CANCELLED"),
    ])
    finished = job._finished(rows)
    assert set(finished["id"]) == {"g1"}
    stats = pd.DataFrame([_player_row(g, "p1", "c1") for g in ("g1", "g2", "g3", "g4")])
    career = next(r for r in job.summarize_players(stats, finished) if r.scope == "CAREER")
    assert career.games == 1


def test_club_comes_from_the_appearance_not_the_roster() -> None:
    """クラブは `player_game_stats.club_id`（実績）で決まること（2.1 の規約6）。

    移籍した選手の過去の記録が新クラブへ移らない。
    """
    finished = job._finished(games([_game("g1", "s1"), _game("g2", "s2")]))
    stats = pd.DataFrame([
        _player_row("g1", "p1", "old-club"), _player_row("g2", "p1", "new-club"),
    ])
    rows = {(r.scope_key, r.club_id): r for r in job.summarize_players(stats, finished)
            if r.scope == "SEASON"}
    assert rows[("s1", "old-club")].games == 1
    assert rows[("s2", "new-club")].games == 1


def test_plus_minus_is_not_aggregated() -> None:
    """`plus_minus` を集計しないこと（旧年度でキーが欠落する。1.9）。"""
    assert "plus_minus" not in job.COUNTS
    finished = job._finished(games([_game("g1", "s1")]))
    stats = pd.DataFrame([_player_row("g1", "p1", "c1", plus_minus=12)])
    row = job.summarize_players(stats, finished)[0]
    assert "plus_minus" not in row.counts


def test_games_started_never_exceeds_games() -> None:
    finished = job._finished(games([_game("g1", "s1"), _game("g2", "s1")]))
    stats = pd.DataFrame([
        _player_row("g1", "p1", "c1", started=1),
        _player_row("g2", "p1", "c1", started=0),
    ])
    for row in job.summarize_players(stats, finished):
        assert row.games_started <= row.games
        assert row.games_started == 1


# --- クラブ ---


def test_team_has_two_denominators() -> None:
    """`games`（勝敗・得点）と `stat_games`（ボックススコア）が別に数えられること。

    **スタッツが欠ける試合を作って確かめる** — 欠けないデータでは2つが一致し、
    検査が空振りする。
    """
    finished = job._finished(games([_game("g1", "s1"), _game("g2", "s1")]))
    team_games = pd.DataFrame([
        _team_game("g1", "c1", "s1"), _team_game("g2", "c1", "s1", result=0),
    ])
    # **g2 のスタッツだけ無い**（取り込みが games と stats の間で失敗した状態）
    team_stats = pd.DataFrame([_team_stat("g1", "c1")])
    rows = job.summarize_teams(team_games, team_stats, finished)
    season = next(r for r in rows if r.scope == "SEASON")
    assert season.games == 2
    assert season.stat_games == 1
    assert season.wins == 1


def test_team_points_come_from_games_not_stats() -> None:
    """得点は `games` から取る（`team_game_stats.pts` を使うと母数に縛られる）。"""
    finished = job._finished(games([_game("g1", "s1", home=90, away=80)]))
    team_games = pd.DataFrame([
        _team_game("g1", "home", "s1", is_home=1, result=1),
        _team_game("g1", "away", "s1", is_home=0, result=0),
    ])
    # スタッツは1行も無い
    rows = job.summarize_teams(team_games, pd.DataFrame(columns=["game_id", "club_id", *COUNTS]),
                               finished)
    by_club = {(r.club_id, r.scope): r for r in rows}
    assert by_club[("home", "CAREER")].points_for == 90
    assert by_club[("home", "CAREER")].points_against == 80
    assert by_club[("away", "CAREER")].points_for == 80
    assert by_club[("away", "CAREER")].points_against == 90
    assert by_club[("home", "CAREER")].stat_games == 0


def test_team_wins_never_exceed_games() -> None:
    finished = job._finished(games([_game("g1", "s1"), _game("g2", "s1")]))
    team_games = pd.DataFrame([
        _team_game("g1", "c1", "s1", result=1), _team_game("g2", "c1", "s1", result=0),
    ])
    for row in job.summarize_teams(team_games, pd.DataFrame([_team_stat("g1", "c1")]), finished):
        assert row.wins <= row.games


def test_unplayed_team_games_are_not_counted() -> None:
    """`result` が NULL（未実施）の行が母数に入らないこと。"""
    finished = job._finished(games([_game("g1", "s1")]))
    team_games = pd.DataFrame([
        _team_game("g1", "c1", "s1", result=1), _team_game("g1", "c2", "s1", result=None),
    ])
    rows = job.summarize_teams(team_games, pd.DataFrame([_team_stat("g1", "c1")]), finished)
    assert {r.club_id for r in rows} == {"c1"}


# --- 静的生成する選手の一覧 ---


def _seasons_csv(tmp_path: Path) -> Path:
    path = tmp_path / "seasons.csv"
    path.write_text(
        "id,label,league,start_date,end_date\n"
        "s1,2024-25,B1,2024-09-01,2025-06-30\n"
        "s2,2025-26,B1,2025-09-01,2026-06-30\n"
        "s3,2026-27,PREMIER,2026-09-01,2027-06-30\n"
        "s0,2023-24,B1,2023-09-01,2024-06-30\n",
        encoding="utf-8",
    )
    return path


def test_recent_season_ids_uses_start_date_not_id(tmp_path: Path) -> None:
    """**`start_date` の降順で取る。`id` の降順で並べない**（文字列比較は崩れる）。"""
    assert job.recent_season_ids(_seasons_csv(tmp_path)) == ["s3", "s2", "s1"]


def test_players_csv_lists_only_recent_seasons(tmp_path: Path) -> None:
    """直近3シーズンに1試合でも出場した選手だけを持つこと（要件 8.2）。"""
    finished = job._finished(games([_game("g0", "s0"), _game("g1", "s1")]))
    stats = pd.DataFrame([
        _player_row("g0", "old", "c1"), _player_row("g1", "now", "c1"),
    ])
    players = pd.DataFrame([{"id": "old", "name": "古い"}, {"id": "now", "name": "今の"}])
    rows = job.recent_players(
        stats, finished, players, job.recent_season_ids(_seasons_csv(tmp_path)))
    assert rows == [("now", "今の")]


def test_player_index_is_sorted_and_stable(tmp_path: Path) -> None:
    """並びは `player_id` の昇順。**実行ごとに差分が動かない。**"""
    out = tmp_path / "players.csv"
    job.write_player_index(out, [("20", "二十"), ("10", "十")])
    first = out.read_text(encoding="utf-8")
    job.write_player_index(out, [("20", "二十"), ("10", "十")])
    assert out.read_text(encoding="utf-8") == first
    assert first.splitlines()[0] == "player_id,name"

    # 集計側が昇順で渡す
    finished = job._finished(games([_game("g1", "s1"), _game("g2", "s1")]))
    stats = pd.DataFrame([_player_row("g1", "b", "c1"), _player_row("g2", "a", "c1")])
    players = pd.DataFrame([{"id": "a", "name": "あ"}, {"id": "b", "name": "い"}])
    rows = job.recent_players(stats, finished, players, ["s1"])
    assert [pid for pid, _ in rows] == ["a", "b"]


def test_player_index_rejects_a_comma_in_a_name(tmp_path: Path) -> None:
    """氏名に区切り文字があれば落とす。**黙って壊れた CSV を書かない。**"""
    with pytest.raises(job.SummarizeError):
        job.write_player_index(tmp_path / "players.csv", [("1", "姓,名")])


# --- 送信 ---


def test_payload_keys_match_the_contract() -> None:
    """送る形を**契約ファイルで固定する**（詳細設計 3.7 / 4.12）。

    **API 側も同じ契約を読む。** 片方だけを直すと両側が落ちる — それがこの
    仕組みの目的である（形の凍結ではない）。
    """
    stat = job.PlayerStat(
        player_id="p1", scope="CAREER", scope_key="", club_id="",
        games=2, games_started=1, minutes=60.0, counts=dict(COUNTS), pts=17,
    )
    assert key_paths({"playerStats": job._player_payload([stat])}) == contract(
        "internalStatSummaryPlayers")

    team = job.TeamStat(
        club_id="c1", scope="SEASON", scope_key="s1", games=2, wins=1,
        points_for=160, points_against=150, stat_games=1, counts=dict(COUNTS),
    )
    assert key_paths({"teamStats": job._team_payload([team])}) == contract(
        "internalStatSummaryTeams")


def test_requests_respect_the_row_limit() -> None:
    """1リクエストの行数が上限（160）を超えないこと（3.4）。"""
    assert job.PLAYER_PER_REQUEST == 160
    assert job.TEAM_PER_REQUEST == 160

    sent: list[dict[str, object]] = []

    class Spy:
        def post(self, path: str, body: dict[str, object]) -> None:
            assert path == "stat-summary"
            sent.append(body)

    rows = [
        job.PlayerStat(
            player_id=f"p{i}", scope="CAREER", scope_key="", club_id="",
            games=1, games_started=0, minutes=10.0, counts=dict(COUNTS), pts=17,
        )
        for i in range(400)
    ]
    job.send(Spy(), job.Outcome(players=rows))  # type: ignore[arg-type]
    assert len(sent) == 3
    for body in sent:
        stats = body["playerStats"]
        assert isinstance(stats, list)
        assert len(stats) <= job.PLAYER_PER_REQUEST


def test_missing_columns_are_reported_not_filled() -> None:
    """必要な列が無ければ落とす。**黙って既定値を入れない**（規約5）。"""
    with pytest.raises(job.SummarizeError):
        job._finished(pd.DataFrame([{"id": "g1"}]))
    with pytest.raises(job.SummarizeError):
        job._ints(pd.DataFrame([{"fg2m": 1}]), ("fg2m", "fg2a"))

"""架空8クラブ × 2シーズンの決定論的なテストデータを作る。

実在の選手名・クラブ名を使わない（基本設計 8.1）。乱数は固定シードで、
同じ入力から常に同じ結果を返す。恒等式と CHECK 制約をすべて満たす。
"""

from __future__ import annotations

import random
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from batch.ratings.elo import DEFAULT_PARAMS, rating_change

JST = ZoneInfo("Asia/Tokyo")


def _utc(value: datetime) -> str:
    """DB の時刻列は UTC の ISO 8601（`...Z`）で持つ（CLAUDE.md 時刻の扱い）。"""
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")

CLUBS = [(f"t{index:02d}", f"demo-club-{index}", f"架空クラブ{index}") for index in range(1, 9)]
SEASONS = [
    ("2024-25-B1", "2024-25", "B1", "2024-10-05", "2025-05-11"),
    ("2025-26-B1", "2025-26", "B1", "2025-10-04", "2026-05-10"),
]
# 1週末に4カード、土日で同じ相手と2連戦。14週で1シーズン112試合。
WEEKENDS = 14
TIPOFF_HOURS_JST = (14, 15, 17, 19)
PLAYERS_PER_CLUB = 10
ELO_START = 1500.0


@dataclass(frozen=True)
class Game:
    id: str
    season_id: str
    game_date: str
    tipoff_at: datetime
    home: str
    away: str
    venue_id: str
    series_game_no: int
    home_score: int
    away_score: int
    attendance: int


def _pairings(round_index: int) -> list[tuple[str, str]]:
    """総当たりの巡回。8クラブを4カードに割る（円卓法）。"""
    ids = [club[0] for club in CLUBS]
    fixed, rotating = ids[0], ids[1:]
    offset = round_index % len(rotating)
    order = rotating[offset:] + rotating[:offset]
    table = [fixed, *order]
    pairs = []
    for index in range(len(table) // 2):
        home, away = table[index], table[-1 - index]
        # 巡回の偶奇でホームとアウェイを入れ替え、ホーム試合数を揃える
        pairs.append((home, away) if round_index % 2 == 0 else (away, home))
    return pairs


def _schedule() -> list[Game]:
    strength = {club[0]: 1500 + (4 - index) * 25 for index, club in enumerate(CLUBS)}
    rng = random.Random(20260922)
    games: list[Game] = []
    for season_id, _, _, start_date, _ in SEASONS:
        season_start = datetime.fromisoformat(start_date)
        for weekend in range(WEEKENDS):
            pairs = _pairings(weekend)
            for day_offset in (0, 1):  # 土日の2連戦
                day = season_start + timedelta(days=weekend * 7 + day_offset)
                for slot, (home, away) in enumerate(pairs):
                    hour = TIPOFF_HOURS_JST[slot % len(TIPOFF_HOURS_JST)]
                    tipoff = datetime(day.year, day.month, day.day, hour, 5, tzinfo=JST)
                    margin = (strength[home] - strength[away]) / 25 + 3 + rng.gauss(0, 9)
                    total = rng.randint(150, 175)
                    home_score = max(40, round((total + margin) / 2))
                    away_score = max(40, total - home_score)
                    if home_score == away_score:  # 引き分けは存在しない
                        home_score += 1
                    games.append(
                        Game(
                            id=f"{season_id[:7]}-{len(games):04d}",
                            season_id=season_id,
                            game_date=day.date().isoformat(),
                            tipoff_at=tipoff,
                            home=home,
                            away=away,
                            venue_id=f"v{home[1:]}",
                            series_game_no=day_offset + 1,
                            home_score=home_score,
                            away_score=away_score,
                            attendance=rng.randint(2000, 4800),
                        )
                    )
    return games


def _box_score(total_points: int, rng: random.Random) -> dict[str, int]:
    """恒等式を満たすチーム・選手のカウントを作る。"""
    ftm = rng.randint(6, 20)
    fg3m = rng.randint(4, 14)
    remaining = total_points - ftm - fg3m * 3
    if remaining < 0 or remaining % 2 == 1:
        # 2点シュートで埋められる形に寄せる（引数の得点は必ず再現する）
        ftm += remaining % 2 if remaining >= 0 else 0
        remaining = total_points - ftm - fg3m * 3
        while remaining < 0:
            fg3m -= 1
            remaining = total_points - ftm - fg3m * 3
        if remaining % 2 == 1:
            ftm += 1
            remaining -= 1
    fg2m = remaining // 2
    fg2a = fg2m + rng.randint(8, 22)
    fg3a = fg3m + rng.randint(6, 18)
    fta = ftm + rng.randint(2, 8)
    oreb = rng.randint(6, 14)
    dreb = rng.randint(20, 32)
    tov = rng.randint(8, 18)
    return {
        "pts": total_points,
        "fg2m": fg2m, "fg2a": fg2a,
        "fg3m": fg3m, "fg3a": fg3a,
        "ftm": ftm, "fta": fta,
        "oreb": oreb, "dreb": dreb,
        "ast": rng.randint(14, 28),
        "tov": tov,
        "stl": rng.randint(4, 12),
        "blk": rng.randint(1, 7),
        "pf": rng.randint(12, 22),
        "fd": rng.randint(12, 22),
    }


def _split_counts(team: dict[str, int], shares: list[float]) -> list[dict[str, int]]:
    """チーム合計を選手へ配分し、合計が一致することを保つ。"""
    rows: list[dict[str, int]] = [{} for _ in shares]
    for key, total in team.items():
        if key == "pts":
            continue
        assigned = [int(total * share) for share in shares]
        for index in range(total - sum(assigned)):
            assigned[index % len(assigned)] += 1
        for row, value in zip(rows, assigned, strict=True):
            row[key] = value
    for row in rows:
        # 成功数が試投数を超えないよう、配分後に整える
        for made, attempted in (("fg2m", "fg2a"), ("fg3m", "fg3a"), ("ftm", "fta")):
            row[attempted] = max(row[attempted], row[made])
        row["pts"] = row["fg2m"] * 2 + row["fg3m"] * 3 + row["ftm"]
    return rows


def seed_test_database(connection: sqlite3.Connection) -> None:
    """開いた接続（マイグレーション適用済み）へテストデータを投入する。"""
    rng = random.Random(4242)
    cursor = connection.cursor()

    cursor.executemany(
        "INSERT INTO seasons (id, label, league, start_date, end_date) VALUES (?, ?, ?, ?, ?)",
        SEASONS,
    )
    cursor.executemany("INSERT INTO clubs (id, slug, name) VALUES (?, ?, ?)", CLUBS)
    cursor.executemany(
        "INSERT INTO club_source_ids (source_id, club_id, valid_from) VALUES (?, ?, ?)",
        [(club_id, club_id, "2016-09-01") for club_id, _, _ in CLUBS],
    )
    cursor.executemany(
        "INSERT INTO venues (id, name, prefecture, lat, lng) VALUES (?, ?, ?, ?, ?)",
        [(f"v{club_id[1:]}", f"架空アリーナ{club_id[1:]}", "東京都", 35.6 + index * 0.1,
          139.7 + index * 0.1) for index, (club_id, _, _) in enumerate(CLUBS)],
    )
    cursor.executemany(
        "INSERT INTO venue_revisions (venue_id, valid_from, name, capacity) VALUES (?, ?, ?, ?)",
        [(f"v{club_id[1:]}", "2016-09-01", f"架空アリーナ{club_id[1:]}", 5000 + index * 250)
         for index, (club_id, _, _) in enumerate(CLUBS)],
    )
    cursor.executemany(
        "INSERT INTO venue_source_keys (source_code, venue_id) VALUES (?, ?)",
        [(f"v{club_id[1:]}", f"v{club_id[1:]}") for club_id, _, _ in CLUBS],
    )
    cursor.executemany(
        "INSERT INTO club_seasons (club_id, season_id, name, short_name, league, primary_venue_id)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [(club_id, season_id, name, f"架空{club_id[1:]}", "B1", f"v{club_id[1:]}")
         for club_id, _, name in CLUBS for season_id, *_ in SEASONS],
    )

    players = []
    for club_id, _, _ in CLUBS:
        for number in range(PLAYERS_PER_CLUB):
            players.append((f"{club_id}p{number:02d}", f"架空 {club_id[1:]}{number:02d}",
                            180 + number, club_id, number))
    cursor.executemany(
        "INSERT INTO players (id, name, height_cm) VALUES (?, ?, ?)",
        [(pid, name, height) for pid, name, height, _, _ in players],
    )
    positions = ("PG", "SG", "SF", "PF", "C")
    cursor.executemany(
        "INSERT INTO player_seasons (player_id, season_id, club_id, number, position, roster_type)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [(pid, season_id, club_id, str(number), positions[number % len(positions)], "JP")
         for pid, _, _, club_id, number in players for season_id, *_ in SEASONS],
    )

    games = _schedule()
    elo = {club_id: ELO_START for club_id, _, _ in CLUBS}
    games_played = {club_id: 0 for club_id, _, _ in CLUBS}
    rating_index: dict[tuple[str, str], tuple[object, ...]] = {}

    for game in games:
        finished = game.tipoff_at + timedelta(hours=2)
        cursor.execute(
            "INSERT INTO games (id, season_id, league, competition, game_date, tipoff_at,"
            " finished_at, finished_at_is_estimated, home_club_id, away_club_id, venue_id,"
            " is_primary_venue, series_game_no, status, home_score, away_score, attendance,"
            " spectator_restricted, source_url, fetched_at)"
            " VALUES (?, ?, 'B1', 'REGULAR', ?, ?, ?, 0, ?, ?, ?, 1, ?, 'FINISHED', ?, ?, ?, 0,"
            " NULL, ?)",
            (game.id, game.season_id, game.game_date, _utc(game.tipoff_at),
             _utc(finished), game.home, game.away, game.venue_id,
             game.series_game_no, game.home_score, game.away_score, game.attendance,
             _utc(finished)),
        )

        for club_id, opponent, is_home, score, opponent_score in (
            (game.home, game.away, 1, game.home_score, game.away_score),
            (game.away, game.home, 0, game.away_score, game.home_score),
        ):
            cursor.execute(
                "INSERT INTO team_games (game_id, club_id, opponent_id, season_id, game_date,"
                " finished_at, is_home, competition, result, margin)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 'REGULAR', ?, ?)",
                (game.id, club_id, opponent, game.season_id, game.game_date,
                 _utc(finished), is_home,
                 1 if score > opponent_score else 0, score - opponent_score),
            )
            team = _box_score(score, rng)
            possessions = team["fg2a"] + team["fg3a"] - team["oreb"] + team["tov"] \
                + 0.44 * team["fta"]
            cursor.execute(
                "INSERT INTO team_game_stats (game_id, club_id, game_date, is_home, pts, fg2m,"
                " fg2a, fg3m, fg3a, ftm, fta, oreb, dreb, ast, tov, stl, blk, pf, fd,"
                " possessions, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
                " ?, ?, ?, ?, ?, ?)",
                (game.id, club_id, game.game_date, is_home, team["pts"], team["fg2m"],
                 team["fg2a"], team["fg3m"], team["fg3a"], team["ftm"], team["fta"],
                 team["oreb"], team["dreb"], team["ast"], team["tov"], team["stl"],
                 team["blk"], team["pf"], team["fd"], round(possessions, 1),
                 _utc(finished)),
            )

            # 上位8名が出場し、残りは欠場（出場者の分布に幅を持たせる）
            shares = [0.20, 0.17, 0.15, 0.13, 0.12, 0.09, 0.08, 0.06]
            rows = _split_counts(team, shares)
            minutes = [34.0, 31.0, 29.0, 26.0, 24.0, 20.0, 18.0, 18.0]
            for index, (row, played) in enumerate(zip(rows, minutes, strict=True)):
                player_id = f"{club_id}p{index:02d}"
                cursor.execute(
                    "INSERT INTO player_game_stats (game_id, player_id, club_id, game_date,"
                    " started, minutes, fg2m, fg2a, fg3m, fg3a, ftm, fta, oreb, dreb, ast, tov,"
                    " stl, blk, pf, fd, plus_minus, pts, fetched_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (game.id, player_id, club_id, game.game_date, 1 if index < 5 else 0, played,
                     row["fg2m"], row["fg2a"], row["fg3m"], row["fg3a"], row["ftm"], row["fta"],
                     row["oreb"], row["dreb"], row["ast"], row["tov"], row["stl"], row["blk"],
                     min(row["pf"], 5), row["fd"], score - opponent_score, row["pts"],
                     _utc(finished)),
                )
            for index in range(PLAYERS_PER_CLUB):
                cursor.execute(
                    "INSERT INTO game_entries (game_id, player_id, status, source, confidence,"
                    " fetched_at) VALUES (?, ?, ?, 'OFFICIAL', NULL, ?)",
                    (game.id, f"{club_id}p{index:02d}", "ENTRY" if index < 8 else "OUT",
                     _utc(finished)),
                )

        # Elo の更新。**式は本番実装（`batch/ratings/elo.py`）を呼ぶ。**
        # シードに式を写すと、探索でパラメータを変えたときに片方だけ古くなる。
        change = rating_change(
            elo[game.home], elo[game.away], game.home_score, game.away_score,
            home_advantage=DEFAULT_PARAMS.home_advantage, params=DEFAULT_PARAMS,
        )
        elo[game.home] += change
        elo[game.away] -= change
        games_played[game.home] += 1
        games_played[game.away] += 1

        # **`team_ratings` の1行はその試合日の「終了時点」の値**（詳細設計 1.4）。
        # 特徴量は `as_of_date < 対象試合日` の最新行を読むため、開始前の値を
        # 書くと前日の結果が永久に反映されない。同じ日に2試合ある場合は
        # 最後の試合の結果まで含めた値で上書きする。
        for club_id in (game.home, game.away):
            rating_index[(club_id, game.game_date)] = (
                club_id, game.game_date, game.season_id,
                elo[club_id], None, None, None, games_played[club_id],
            )

    cursor.executemany(
        "INSERT INTO team_ratings (club_id, as_of_date, season_id, elo, off_rating, def_rating,"
        " pace, games_played) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        list(rating_index.values()),
    )
    connection.commit()

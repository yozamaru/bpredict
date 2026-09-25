"""Elo の算出（詳細設計 2.5）と再計算ジョブ（4.2 ステップ7）の検証。

式そのものは手計算と突き合わせる。**シードの値と比べても意味がない** —
シードは同じ実装を呼んでいるため、実装が間違っていれば両方が同じだけ間違う。
"""
from __future__ import annotations

import itertools
import math

import pandas as pd
import pytest

from batch.jobs import recompute_ratings
from batch.loader.api import InternalApi
from batch.ratings import elo as elo_module
from batch.ratings.elo import (
    DEFAULT_PARAMS,
    EloParams,
    RatingError,
    expected_home,
    home_advantage_for,
    margin_multiplier,
    rating_change,
    recompute,
    season_start_elo,
)
from batch.ratings.params import (
    HOME_ADVANTAGE_INITIAL,
    K_INITIAL,
    LEAGUE_MEAN,
    PROMOTED_ELO_INITIAL,
    SEASON_REGRESSION_INITIAL,
)


def _seasons(*ids: str) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"id": season_id, "label": season_id[:7], "league": "B1",
             "start_date": f"{2016 + index}-09-01", "end_date": f"{2017 + index}-05-31"}
            for index, season_id in enumerate(ids)
        ]
    )


def _game(
    game_id: str,
    season_id: str,
    date: str,
    home: str,
    away: str,
    home_score: int,
    away_score: int,
    *,
    status: str = "FINISHED",
    restricted: object = None,
) -> dict[str, object]:
    return {
        "id": game_id, "season_id": season_id, "game_date": date,
        "tipoff_at": f"{date}T10:05:00Z", "status": status,
        "home_club_id": home, "away_club_id": away,
        "home_score": home_score, "away_score": away_score,
        "spectator_restricted": restricted,
    }


# --- 式 -------------------------------------------------------------------

def test_expected_home_matches_formula():
    """Elo 差60点は勝率 58.6%（詳細設計 2.5 の検算）。

    厳密には 58.55% で、文書の 58.6% は小数第1位への丸めである。
    """
    assert expected_home(1560.0, 1500.0, 0.0) == pytest.approx(0.58550, abs=5e-5)


def test_home_advantage_initial_matches_the_measured_home_winrate():
    """初期値 20 の根拠を数値で固定する。

    **旧版は「ホーム勝率60% → 70.5点」を固定していたが、前提が実測で外れた。**
    リーグ戦4シーズン（n=1,829）の実測は **52.7%** で、対応するのは 18.8点
    （`verification/RESULTS.md`）。旧グリッド `{40,55,70,85}` は実測値を1つも
    含んでいなかった。
    """
    assert expected_home(1500.0, 1500.0, 18.8) == pytest.approx(0.527, abs=5e-4)
    # 探索の初期値 20 は実測相当の近傍にある
    assert abs(HOME_ADVANTAGE_INITIAL - 18.8) < 2.0
    # 旧前提（60% → 70.4点）も式としては再現できる。**前提が変わっただけである**
    assert expected_home(1500.0, 1500.0, 70.4) == pytest.approx(0.60, abs=5e-4)


def test_search_grids_contain_their_initial_values() -> None:
    """探索グリッドが初期値を含むこと。

    **旧グリッドは実測値を含んでいなかった**（最小の40でも実測の2倍以上）。
    含まないグリッドで探索すると「最も弱い値」が常に選ばれるだけになる。
    """
    from batch.ratings.params import (
        HOME_ADVANTAGE_GRID,
        K_GRID,
        PROMOTED_ELO_GRID,
        PROMOTED_ELO_INITIAL,
        SEASON_REGRESSION_GRID,
        SEASON_REGRESSION_INITIAL,
    )

    assert K_INITIAL in K_GRID
    assert HOME_ADVANTAGE_INITIAL in HOME_ADVANTAGE_GRID
    assert SEASON_REGRESSION_INITIAL in SEASON_REGRESSION_GRID
    assert PROMOTED_ELO_INITIAL in PROMOTED_ELO_GRID
    # 実測相当（18.8点）を挟む値がグリッドにあること
    assert min(HOME_ADVANTAGE_GRID) <= 18.8 <= max(HOME_ADVANTAGE_GRID)


def test_rating_change_is_zero_sum_and_hand_computed():
    """増分が手計算と一致し、両チームの合計が動かないこと。"""
    elo_home, elo_away = 1550.0, 1500.0
    change = rating_change(
        elo_home, elo_away, 88, 81,
        home_advantage=HOME_ADVANTAGE_INITIAL, params=DEFAULT_PARAMS,
    )
    expected = 1 / (1 + 10 ** ((1500.0 - 1550.0 - HOME_ADVANTAGE_INITIAL) / 400))
    multiplier = math.log(8) * (2.2 / (0.001 * 50 + 2.2))
    assert change == pytest.approx(K_INITIAL * multiplier * (1.0 - expected))
    # 零和: ホームに +change、アウェイに -change を入れるので総和は不変
    assert (elo_home + change) + (elo_away - change) == pytest.approx(elo_home + elo_away)


def test_margin_multiplier_decays_with_elo_gap():
    """Elo 差が大きいほど倍率が小さくなる（自己相関の抑制）。"""
    close = margin_multiplier(1500.0, 1500.0, 10)
    apart = margin_multiplier(1700.0, 1300.0, 10)
    assert apart < close


def test_elo_uses_home_advantage_when_restriction_unknown():
    """`spectator_restricted` が NULL のときは通常のホームアドバンテージを使う。

    NULL は「制限されていた証拠がない」であって「制限されていた」ではない
    （詳細設計 1.3 / 2.5）。0 にすると判定不能な試合すべてで Elo が歪む。
    """
    assert home_advantage_for(None, DEFAULT_PARAMS) == HOME_ADVANTAGE_INITIAL
    assert home_advantage_for(float("nan"), DEFAULT_PARAMS) == HOME_ADVANTAGE_INITIAL
    assert home_advantage_for(0, DEFAULT_PARAMS) == HOME_ADVANTAGE_INITIAL
    assert home_advantage_for(1, DEFAULT_PARAMS) == 0.0


def test_spectator_restricted_removes_home_advantage_in_recompute():
    """観客制限下の試合では Elo の更新にホームアドバンテージが乗らないこと。"""
    base = _game("g1", "2020-21-B1", "2020-10-03", "A", "B", 80, 79)
    normal = recompute(pd.DataFrame([base]), _seasons("2020-21-B1"))
    restricted = recompute(
        pd.DataFrame([{**base, "spectator_restricted": 1}]), _seasons("2020-21-B1")
    )
    a_normal = float(normal[normal["club_id"] == "A"].iloc[0]["elo"])
    a_restricted = float(restricted[restricted["club_id"] == "A"].iloc[0]["elo"])
    # 同じ勝ち方でも、HA がない方が「番狂わせ度」が高く増分が大きい
    assert a_restricted > a_normal


# --- シーズン境界 ---------------------------------------------------------

def test_elo_season_regression_applied_once():
    """シーズン間回帰が1回だけ掛かること（洗い替え2回で二重適用しない）。"""
    games = pd.DataFrame([
        _game("g1", "2016-17-B1", "2016-10-01", "A", "B", 100, 80),
        _game("g2", "2017-18-B1", "2017-10-01", "A", "B", 90, 88),
    ])
    seasons = _seasons("2016-17-B1", "2017-18-B1")
    first = recompute(games, seasons)
    again = recompute(games, seasons)
    assert first.equals(again)

    end_2016 = float(first[(first["club_id"] == "A") & (first["as_of_date"] == "2016-10-01")]
                     .iloc[0]["elo"])
    start_2017 = LEAGUE_MEAN + (end_2016 - LEAGUE_MEAN) * SEASON_REGRESSION_INITIAL
    change = rating_change(
        start_2017,
        LEAGUE_MEAN + ((3000.0 - end_2016) - LEAGUE_MEAN) * SEASON_REGRESSION_INITIAL,
        90, 88, home_advantage=HOME_ADVANTAGE_INITIAL, params=DEFAULT_PARAMS,
    )
    got = float(first[(first["club_id"] == "A") & (first["as_of_date"] == "2017-10-01")]
                .iloc[0]["elo"])
    assert got == pytest.approx(start_2017 + change)


def test_first_season_starts_at_league_mean():
    """**データ上の最初のシーズン**は全クラブがリーグ平均から始まること。

    ここで昇格扱い（1400）にすると、Elo は零和なのでリーグ平均が 1400 に固定され、
    以後シーズン間回帰が毎年 1500 方向へ引っ張る系統誤差になる。「昇格」は前季に
    トップリーグの実績がないことを指すのであって、手元にデータがないことではない。
    """
    games = pd.DataFrame([_game("g1", "2016-17-B1", "2016-10-01", "A", "B", 80, 79)])
    rows = recompute(games, _seasons("2016-17-B1"))
    assert float(rows["elo"].sum()) == pytest.approx(2 * LEAGUE_MEAN)


def test_returning_club_treated_as_promoted():
    """前季にトップリーグの試合がないクラブは、古い Elo を持ち越さない。

    空白が1シーズンでも8シーズンでも同じ扱いにする（要件 6.6）。
    `SEASON_REGRESSION` を空白数だけ適用すると偏差が `R^N` に縮んで実質
    リーグ平均になり、二部に長くいたクラブを平均と評価することになる。
    """
    games = pd.DataFrame([
        # C は初季に大勝して高い Elo を得るが、次季は不在、3季目に復帰する
        _game("g1", "2016-17-B1", "2016-10-01", "C", "A", 120, 70),
        _game("g2", "2017-18-B1", "2017-10-01", "A", "B", 80, 79),
        _game("g3", "2018-19-B1", "2018-10-01", "C", "A", 80, 79),
    ])
    rows = recompute(games, _seasons("2016-17-B1", "2017-18-B1", "2018-19-B1"))
    high = float(rows[(rows["club_id"] == "C") & (rows["as_of_date"] == "2016-10-01")]
                 .iloc[0]["elo"])
    assert high > LEAGUE_MEAN

    def elo_of(club_id: str, date: str) -> float:
        row = rows[(rows["club_id"] == club_id) & (rows["as_of_date"] == date)]
        return float(row.iloc[0]["elo"])

    # C の 2018-19 開始値は昇格クラブの初期値。過去の高い Elo でも、その回帰値でもない
    start_c = PROMOTED_ELO_INITIAL
    # A は前季に試合があるので回帰値から始まる
    start_a = season_start_elo(elo_of("A", "2017-10-01"), DEFAULT_PARAMS)
    change = rating_change(
        start_c, start_a, 80, 79,
        home_advantage=HOME_ADVANTAGE_INITIAL, params=DEFAULT_PARAMS,
    )
    assert elo_of("C", "2018-10-01") == pytest.approx(start_c + change)
    # 持ち越していたら、初季の高い Elo を1回回帰させた水準から始まっていたはず
    carried_over = LEAGUE_MEAN + (high - LEAGUE_MEAN) * SEASON_REGRESSION_INITIAL
    assert start_c < carried_over


def test_season_start_elo_for_promoted_is_below_league_mean():
    """昇格クラブに 1500（リーグ平均）を与えない（要件 6.6）。"""
    assert season_start_elo(None, DEFAULT_PARAMS) == PROMOTED_ELO_INITIAL
    assert PROMOTED_ELO_INITIAL < LEAGUE_MEAN


# --- 行の意味と絞り込み ---------------------------------------------------

def test_rating_row_is_end_of_day_value():
    """1行はその試合日の**終了時点**の値（詳細設計 1.4）。

    開始前の値を書くと、特徴量が `as_of_date < 対象試合日` で読むため
    前日の結果が永久に反映されない。
    """
    games = pd.DataFrame([_game("g1", "2016-17-B1", "2016-10-01", "A", "B", 80, 79)])
    rows = recompute(games, _seasons("2016-17-B1"))
    assert float(rows[rows["club_id"] == "A"].iloc[0]["elo"]) > LEAGUE_MEAN
    assert int(rows[rows["club_id"] == "A"].iloc[0]["games_played"]) == 1


def test_same_day_multiple_games_keep_last_value():
    """同じ日に複数試合があるクラブは、最後の試合まで含めた値になること。"""
    games = pd.DataFrame([
        _game("g1", "2016-17-B1", "2016-10-01", "A", "B", 100, 80),
        {**_game("g2", "2016-17-B1", "2016-10-01", "A", "C", 70, 100),
         "tipoff_at": "2016-10-01T13:05:00Z"},
    ])
    rows = recompute(games, _seasons("2016-17-B1"))
    row = rows[(rows["club_id"] == "A") & (rows["as_of_date"] == "2016-10-01")]
    assert len(row) == 1
    assert int(row.iloc[0]["games_played"]) == 2


@pytest.mark.parametrize("status", ["SCHEDULED", "POSTPONED", "CANCELLED"])
def test_unplayed_games_do_not_move_elo(status):
    """結果が確定していない試合を集計に含めない。"""
    games = pd.DataFrame([
        _game("g1", "2016-17-B1", "2016-10-01", "A", "B", 80, 79),
        _game("g2", "2016-17-B1", "2016-10-08", "A", "B", 0, 0, status=status),
    ])
    rows = recompute(games, _seasons("2016-17-B1"))
    assert set(rows["as_of_date"]) == {"2016-10-01"}


def test_games_without_score_are_skipped_even_if_finished():
    """`FINISHED` でもスコアが NULL なら含めない（0 と欠損を混同しない）。"""
    games = pd.DataFrame([
        {**_game("g1", "2016-17-B1", "2016-10-01", "A", "B", 0, 0),
         "home_score": None, "away_score": None},
    ])
    assert recompute(games, _seasons("2016-17-B1")).empty


def test_games_played_resets_each_season():
    """`games_played` は当季の消化数（序盤判定に使う。詳細設計 2.5）。"""
    games = pd.DataFrame([
        _game("g1", "2016-17-B1", "2016-10-01", "A", "B", 80, 79),
        _game("g2", "2016-17-B1", "2016-10-08", "A", "B", 80, 79),
        _game("g3", "2017-18-B1", "2017-10-01", "A", "B", 80, 79),
    ])
    rows = recompute(games, _seasons("2016-17-B1", "2017-18-B1"))
    last = rows[(rows["club_id"] == "A") & (rows["as_of_date"] == "2017-10-01")].iloc[0]
    assert int(last["games_played"]) == 1


def test_derived_ratings_are_left_null():
    """`off_rating` / `def_rating` / `pace` は埋めない（集計窓が文書で未定義）。"""
    games = pd.DataFrame([_game("g1", "2016-17-B1", "2016-10-01", "A", "B", 80, 79)])
    rows = recompute(games, _seasons("2016-17-B1"))
    for column in ("off_rating", "def_rating", "pace"):
        assert rows[column].isna().all()


def test_missing_season_raises():
    """試合があるのに `seasons` にないシーズンは黙って捨てない。"""
    games = pd.DataFrame([_game("g1", "9999-00-B1", "2016-10-01", "A", "B", 80, 79)])
    with pytest.raises(RatingError):
        recompute(games, _seasons("2016-17-B1"))


def test_recompute_is_deterministic_under_row_order():
    """入力の行順を変えても結果が変わらないこと（冪等な洗い替えの前提）。"""
    rows = [
        _game("g1", "2016-17-B1", "2016-10-01", "A", "B", 100, 80),
        _game("g2", "2016-17-B1", "2016-10-08", "B", "A", 90, 70),
        _game("g3", "2016-17-B1", "2016-10-15", "A", "B", 75, 74),
    ]
    seasons = _seasons("2016-17-B1")
    forward = recompute(pd.DataFrame(rows), seasons)
    backward = recompute(pd.DataFrame(list(reversed(rows))), seasons)
    assert forward.equals(backward)


def test_params_are_honoured():
    """パラメータは探索対象である（既定値を実装に埋め込んでいないこと）。"""
    games = pd.DataFrame([_game("g1", "2016-17-B1", "2016-10-01", "A", "B", 80, 79)])
    seasons = _seasons("2016-17-B1")
    default = recompute(games, seasons)
    strong = recompute(games, seasons, params=EloParams(k=32.0))
    assert float(strong[strong["club_id"] == "A"].iloc[0]["elo"]) > \
        float(default[default["club_id"] == "A"].iloc[0]["elo"])


# --- 実データに近い規模 ---------------------------------------------------

def test_recompute_over_seeded_database(seeded_db):
    """架空8クラブ × 2シーズンのシードで、全クラブ・全試合日の行が出ること。"""
    from batch.features.dataset import export_sqlite

    dataset = export_sqlite(seeded_db)
    rows = recompute(dataset.table("games"), dataset.table("seasons"))
    games = dataset.table("games")
    finished = games[games["status"] == "FINISHED"]
    # 1試合につき2クラブ。同日同クラブは1行にまとまる
    assert len(rows) == len({
        (club, date)
        for _, game in finished.iterrows()
        for club in (game["home_club_id"], game["away_club_id"])
        for date in [game["game_date"]]
    })
    assert rows["elo"].notna().all()

    # 零和: シーズン内で Elo の合計は動かない。**各試合日の断面**で合計を取り、
    # その日までに1試合もしていないクラブは開始値で数える。
    seasons = dataset.table("seasons").sort_values("start_date")
    first_season = str(seasons.iloc[0]["id"])
    first = rows[rows["season_id"] == first_season]
    clubs = sorted(set(first["club_id"]))
    for date in sorted(set(first["as_of_date"])):
        snapshot = first[first["as_of_date"] <= date]
        latest = snapshot.sort_values("as_of_date").groupby("club_id").tail(1)
        played = dict(zip(latest["club_id"], latest["elo"], strict=True))
        total = sum(float(played.get(club, LEAGUE_MEAN)) for club in clubs)
        assert total == pytest.approx(len(clubs) * LEAGUE_MEAN, abs=1e-6), date


# --- ジョブ ---------------------------------------------------------------

class _Recorder:
    """`InternalApi` の transport を差し替えて送信内容を記録する。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url, method, body, headers):
        import json

        from batch.loader.api import Response

        payload = json.loads(body) if body else {}
        self.calls.append((url, payload))
        return Response(200, json.dumps({"data": {"inserted": 0}}))


def _api(recorder: _Recorder) -> InternalApi:
    return InternalApi("https://example.invalid", "t", transport=recorder)


def test_chunks_never_split_a_single_date():
    """日付の境界で切ること。同じ `as_of_date` が2リクエストに跨らない。

    跨ると、後のリクエストの期間 DELETE が前のリクエストの行を消す。
    """
    frame = pd.DataFrame([
        {"as_of_date": f"2016-10-{day:02d}", "club_id": f"c{i}"}
        for day in (1, 2, 3) for i in range(4)
    ])
    parts = list(recompute_ratings.chunks(frame, limit=5))
    assert len(parts) == 3
    seen: set[str] = set()
    for part in parts:
        dates = set(part["as_of_date"])
        assert not (dates & seen), "同じ日付が複数のリクエストに現れた"
        seen |= dates


def test_chunk_ranges_do_not_overlap():
    """各リクエストの `fromDate`〜`toDate` が互いに重ならないこと。

    重なると、後のリクエストの期間 DELETE が前のリクエストで入れた行を消す。
    """
    frame = pd.DataFrame([
        {"as_of_date": f"2016-10-{day:02d}", "club_id": f"c{i}"}
        for day in (1, 5, 9) for i in range(3)
    ])
    ranges = [
        (str(part["as_of_date"].min()), str(part["as_of_date"].max()))
        for part in recompute_ratings.chunks(frame, limit=4)
    ]
    assert len(ranges) > 1, "分割が起きていないと重なりの検査にならない"
    for (_, previous_end), (next_start, _) in itertools.pairwise(ranges):
        assert previous_end < next_start


def test_run_writes_snapshot_before_posting(tmp_path, seeded_db, monkeypatch):
    """スナップショットを D1 より先に書くこと（基本設計 2.2 の書き出し順序）。"""
    from batch.features.dataset import export_sqlite, load_snapshot, write_snapshot

    write_snapshot(export_sqlite(seeded_db), tmp_path)
    order: list[str] = []

    real_write = recompute_ratings.write_snapshot

    def spy_write(dataset, directory, *, tables=None):
        order.append("snapshot")
        return real_write(dataset, directory, tables=tables)

    monkeypatch.setattr(recompute_ratings, "write_snapshot", spy_write)

    recorder = _Recorder()

    class Ordered(_Recorder):
        def __call__(self, url, method, body, headers):
            order.append("d1")
            return recorder(url, method, body, headers)

    result = recompute_ratings.run(api=_api(Ordered()), snapshot_dir=tmp_path)
    assert result.status == "SUCCESS"
    assert order[0] == "snapshot"
    assert "d1" in order
    # MANIFEST が書き直されており、そのまま読み直せる
    assert len(load_snapshot(tmp_path).table("team_ratings")) == result.rows


def test_run_from_date_narrows_only_the_write_range(tmp_path, seeded_db):
    """`--from-date` が絞るのは書き込み範囲だけで、計算は常に全期間であること。"""
    from batch.features.dataset import export_sqlite, load_snapshot, write_snapshot

    write_snapshot(export_sqlite(seeded_db), tmp_path)
    full_recorder = _Recorder()
    full = recompute_ratings.run(api=_api(full_recorder), snapshot_dir=tmp_path)

    partial_recorder = _Recorder()
    partial = recompute_ratings.run(
        api=_api(partial_recorder), snapshot_dir=tmp_path, from_date="2025-01-01"
    )
    assert partial.rows == full.rows                      # 計算は全期間
    assert partial.from_date >= "2025-01-01"              # 書き込みは範囲内だけ
    posted = sum(len(payload["ratings"]) for _, payload in partial_recorder.calls)
    assert 0 < posted < full.rows
    # スナップショットには全期間が入る
    assert len(load_snapshot(tmp_path).table("team_ratings")) == full.rows


def test_run_respects_row_limit_per_request(tmp_path, seeded_db):
    """1リクエストの行数が `team_ratings` の上限を超えないこと（詳細設計 3.4）。"""
    from batch.features.dataset import export_sqlite, write_snapshot

    write_snapshot(export_sqlite(seeded_db), tmp_path)
    recorder = _Recorder()
    recompute_ratings.run(api=_api(recorder), snapshot_dir=tmp_path)
    assert recorder.calls
    for _, payload in recorder.calls:
        assert len(payload["ratings"]) <= recompute_ratings.ROWS_PER_REQUEST


def test_run_is_idempotent(tmp_path, seeded_db):
    """`test_recompute_ratings_full_rebuild` — 2回流して同じ結果になること。"""
    from batch.features.dataset import export_sqlite, write_snapshot

    write_snapshot(export_sqlite(seeded_db), tmp_path)
    first_recorder, second_recorder = _Recorder(), _Recorder()
    first = recompute_ratings.run(api=_api(first_recorder), snapshot_dir=tmp_path)
    digest_first = (tmp_path / "team_ratings.parquet").read_bytes()
    second = recompute_ratings.run(api=_api(second_recorder), snapshot_dir=tmp_path)
    assert (first.rows, first.from_date, first.to_date) == \
        (second.rows, second.from_date, second.to_date)
    assert digest_first == (tmp_path / "team_ratings.parquet").read_bytes()
    assert [p for _, p in first_recorder.calls] == [p for _, p in second_recorder.calls]


def test_module_does_not_compute_elo_in_features():
    """特徴量側で Elo を計算しないこと（詳細設計 1.4）。

    `batch/features/` は `team_ratings` を読むだけで、`batch.ratings` を import
    しない。全試合を走査する過程で対象試合を含めてしまう事故を防ぐ。
    """
    import pathlib

    root = pathlib.Path(elo_module.__file__).resolve().parents[2] / "features"
    for path in root.glob("*.py"):
        assert "batch.ratings" not in path.read_text(encoding="utf-8"), path.name

"""学習ジョブ（工程8。`batch/jobs/train.py`）。

**実データを読まない。** スナップショットも D1 も触らず、合成した学習行列と
一時ディレクトリだけで検証する。
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from batch.jobs import train
from batch.model.dataset import TrainingData

SEASONS = ("s1", "s2", "s3", "s4")


def fake_data(per_season: int = 60, seasons: tuple[str, ...] = SEASONS) -> TrainingData:
    """`elo_diff` が勝敗を本当に説明する合成データ。

    無相関にすると「ベースラインを上回る」条件の検証ができない。
    """
    rng = np.random.default_rng(11)
    season_ids = [s for s in seasons for _ in range(per_season)]
    n = len(season_ids)
    elo = rng.normal(0, 120, n)
    probability = 1.0 / (1.0 + np.exp(-elo / 180.0))
    home_win = (rng.random(n) < probability).astype(float)
    features = pd.DataFrame({
        "elo_diff": elo,
        "rest_days_diff": rng.integers(-2, 3, n).astype(float),
        "entry_is_official": np.zeros(n),   # 既知の定数列（`game_entries` が空）
    })
    return TrainingData(
        features=features,
        home_win=home_win,
        margin=elo / 10.0 + rng.normal(0, 8, n),
        total=rng.normal(160, 12, n),
        game_ids=[f"g{i}" for i in range(n)],
        season_ids=season_ids,
        game_dates=[f"2020-01-{i % 28 + 1:02d}" for i in range(n)],
        spectator_restricted=[None] * n,
    )


# --- Learner の差し替え ---

def test_elo_only_learner_uses_just_the_elo_column() -> None:
    """ベースライン2は**「Elo差単体」**である（要件 6.4）。

    `elo_home` / `elo_away` を混ぜると「Elo だけでどこまで行けるか」が測れない。
    """
    assert train.ELO_ONLY == ("elo_diff",)
    data = fake_data(per_season=30)
    learn = train.logistic_learner(train.ELO_ONLY)
    predict, best = learn(
        data.features, data.home_win, np.ones(len(data)), data.features, data.home_win,
    )
    assert best == 0  # early stopping がないため 0
    # **列の順番や余計な列に影響されない**
    shuffled = data.features[["entry_is_official", "rest_days_diff", "elo_diff"]]
    assert predict(data.features) == pytest.approx(predict(shuffled))


def test_elo_only_learner_ignores_the_other_columns() -> None:
    """他の列を壊しても予測が変わらないこと（本当に1列しか見ていない）。"""
    data = fake_data(per_season=30)
    learn = train.logistic_learner(train.ELO_ONLY)
    predict, _ = learn(
        data.features, data.home_win, np.ones(len(data)), data.features, data.home_win,
    )
    broken = data.features.copy()
    broken["rest_days_diff"] = 999.0
    assert predict(data.features) == pytest.approx(predict(broken))


def test_home_always_learner_always_favors_home() -> None:
    """`p > 0.5` が常に真であること。Brier で不利なのはこのベースラインの性質である。"""
    data = fake_data(per_season=10)
    learn = train.home_always_learner()
    predict, _ = learn(
        data.features, data.home_win, np.ones(len(data)), data.features, data.home_win,
    )
    probs = predict(data.features)
    assert probs.size == len(data)
    assert (probs > 0.5).all()


# --- 特徴量のキャッシュ ---

def write_manifest(snapshot: Path, body: str) -> None:
    snapshot.mkdir(parents=True, exist_ok=True)
    (snapshot / "MANIFEST.json").write_text(body, encoding="utf-8")


def test_missing_manifest_is_an_error() -> None:
    with pytest.raises(train.TrainError):
        train.manifest_digest(Path("/nonexistent-snapshot"))


def test_cache_round_trip_preserves_the_matrix(tmp_path: Path) -> None:
    data = fake_data(per_season=5)
    cache = tmp_path / "m.parquet"
    train.save_cache(cache, "digest-a", data)
    loaded = train.load_cached(cache, "digest-a")
    assert loaded is not None
    assert list(loaded.features.columns) == list(data.features.columns)
    assert loaded.home_win == pytest.approx(data.home_win)
    assert loaded.margin == pytest.approx(data.margin)
    assert loaded.total == pytest.approx(data.total)
    assert loaded.game_ids == data.game_ids
    assert loaded.season_ids == data.season_ids
    assert loaded.seasons == data.seasons


def test_cache_is_ignored_when_the_snapshot_changed(tmp_path: Path) -> None:
    """**鍵が合わなければ使わない。** 古い行列で評価する事故を防ぐ。"""
    cache = tmp_path / "m.parquet"
    train.save_cache(cache, "digest-a", fake_data(per_season=5))
    assert train.load_cached(cache, "digest-b") is None


def test_cache_is_ignored_when_the_feature_list_changed(tmp_path: Path, monkeypatch) -> None:
    """**特徴量を増やしたら作り直すこと。**

    スナップショットだけを鍵にすると、特徴量を1つ足しても MANIFEST は変わらないため
    鍵が一致し、**増やす前の特徴量で評価した結果が「増やした後の結果」として出る**。
    しかも落ちないため気づけない。工程8は「1つ足して測る」を繰り返す工程であり、
    ここが抜けていると**測定そのものが無意味になる**。
    """
    cache = tmp_path / "m.parquet"
    train.save_cache(cache, "digest-a", fake_data(per_season=5))
    assert train.load_cached(cache, "digest-a") is not None

    monkeypatch.setattr(train, "FEATURE_KEYS", (*train.FEATURE_KEYS, "new_feature"))
    assert train.load_cached(cache, "digest-a") is None


def test_cache_is_ignored_when_the_feature_order_changed(tmp_path: Path, monkeypatch) -> None:
    """**並びも鍵に含める。** `load_cached` は位置で特徴量を取り出す。"""
    cache = tmp_path / "m.parquet"
    train.save_cache(cache, "digest-a", fake_data(per_season=5))
    monkeypatch.setattr(train, "FEATURE_KEYS", tuple(reversed(train.FEATURE_KEYS)))
    assert train.load_cached(cache, "digest-a") is None


def test_cache_records_the_feature_keys(tmp_path: Path) -> None:
    """何で測ったかを後から読めるようにする（横の JSON に一覧を残す）。"""
    import json

    cache = tmp_path / "m.parquet"
    train.save_cache(cache, "digest-a", fake_data(per_season=5))
    meta = json.loads(cache.with_suffix(".json").read_text(encoding="utf-8"))
    assert meta["feature_keys"] == list(train.FEATURE_KEYS)
    assert meta["feature_sha256"] == train.feature_digest()


def test_cache_is_rebuilt_when_the_manifest_changes(tmp_path: Path, monkeypatch) -> None:
    """スナップショットが変わったら作り直すこと（鍵は MANIFEST のハッシュ）。"""
    snapshot = tmp_path / "snap"
    cache = tmp_path / "m.parquet"
    built = {"count": 0}

    def fake_build(_ds: object) -> TrainingData:
        built["count"] += 1
        return fake_data(per_season=5)

    monkeypatch.setattr(train, "load_snapshot", lambda _path: object())
    monkeypatch.setattr(train, "build_matrix", fake_build)

    write_manifest(snapshot, '{"files": {}, "a": 1}')
    train.training_data(snapshot=snapshot, cache=cache, log=lambda _m: None)
    train.training_data(snapshot=snapshot, cache=cache, log=lambda _m: None)
    assert built["count"] == 1, "同じ MANIFEST では作り直さない"

    write_manifest(snapshot, '{"files": {}, "a": 2}')
    train.training_data(snapshot=snapshot, cache=cache, log=lambda _m: None)
    assert built["count"] == 2, "MANIFEST が変われば作り直す"


def test_refresh_rebuilds_even_when_the_cache_matches(tmp_path: Path, monkeypatch) -> None:
    snapshot = tmp_path / "snap"
    cache = tmp_path / "m.parquet"
    built = {"count": 0}

    def fake_build(_ds: object) -> TrainingData:
        built["count"] += 1
        return fake_data(per_season=5)

    monkeypatch.setattr(train, "load_snapshot", lambda _path: object())
    monkeypatch.setattr(train, "build_matrix", fake_build)
    write_manifest(snapshot, '{"files": {}}')
    train.training_data(snapshot=snapshot, cache=cache, log=lambda _m: None)
    train.training_data(snapshot=snapshot, cache=cache, refresh=True, log=lambda _m: None)
    assert built["count"] == 2


# --- 評価 ---

@pytest.fixture(scope="module")
def report() -> train.Report:
    """LightGBM を1回だけ回す（テストごとに回すと遅い）。

    **1シーズン300件にする。** 4シーズンならテスト fold は2つで n=600 となり、
    採用判定の下限 500 を満たす。これを下回ると `passes_criteria` が
    「比較そのものを行わない」で早期に返り、他の条件の検証ができない
    （実装時にこれで2件落ちた）。
    """
    return train.evaluate_all(fake_data(per_season=300), log=lambda _m: None)


def test_all_four_models_are_evaluated_on_the_same_split(report: train.Report) -> None:
    """**同一の分割で比べる。** 違うウィンドウの数値を比較しても意味がない。"""
    for ev in (report.winner, report.home, report.elo, report.full):
        assert ev.n == report.n
    assert len(report.test_seasons) == len(report.winner.folds)


def test_home_always_is_the_worst_on_brier(report: train.Report) -> None:
    """1段目は確率として極端なので Brier では当然不利になる。"""
    assert report.home.brier > report.winner.brier
    assert report.home.brier > report.elo.brier


def test_the_report_carries_the_gate_inputs(report: train.Report) -> None:
    """採用判定に渡した材料が報告に残ること（後から検算できる）。"""
    assert report.ece_floor is None or report.ece_floor > 0
    assert "entry_is_official" in report.constant_columns
    assert max(report.null_rates.values()) == pytest.approx(0.0)


def test_known_constant_columns_do_not_block_adoption(report: train.Report) -> None:
    """`entry_is_official` は既知の定数列であり、注記に出るが採用は止めない。"""
    assert any("entry_is_official" in note for note in report.decision.notes)
    assert not any("想定外の定数列" in f for f in report.decision.failures)


def test_the_first_model_skips_the_comparison_with_a_current_model(
    report: train.Report,
) -> None:
    """**初回登録である。** 現行モデルがないため比較は課せない（詳細設計 4.6）。"""
    assert any("現行モデルがない" in note for note in report.decision.notes)


def test_report_as_dict_is_json_serializable(report: train.Report) -> None:
    body = json.dumps(report.as_dict(), ensure_ascii=False)
    revived = json.loads(body)
    assert len(revived["models"]) == 4
    assert revived["n"] == report.n
    assert "brier_difference_vs_elo" in revived


def test_render_names_every_baseline(report: train.Report) -> None:
    text = train.render(report)
    for name in ("ホーム必勝", "Elo差単体", "全特徴", "採用判定"):
        assert name in text


# --- 寄与度（gain）---

def test_gain_share_normalises_each_fold_to_one_hundred() -> None:
    """**fold ごとに合計100へ正規化してから平均する。**

    1 fold の gain の絶対値は学習データの量で変わるため直接は比べられない。
    割合にすれば「その fold でモデルが何に依存したか」として読める。
    """
    collected = [
        {"a": 30.0, "b": 10.0},      # 合計 40 → a 75% / b 25%
        {"a": 300.0, "b": 100.0},    # 合計 400 → 同じ割合
    ]
    share = train.gain_share(collected)
    assert share["a"] == pytest.approx(75.0)
    assert share["b"] == pytest.approx(25.0)


def test_gain_share_skips_folds_with_no_gain() -> None:
    """**木が1本も育たなかった fold を 0 として平均に入れない。**

    入れると分母だけが増え、全列の割合が理由なく薄まる。
    """
    share = train.gain_share([{"a": 1.0}, {"a": 0.0}])
    assert share["a"] == pytest.approx(100.0)


def test_gain_share_is_empty_without_any_usable_fold() -> None:
    assert train.gain_share([]) == {}
    assert train.gain_share([{"a": 0.0}]) == {}


def test_winner_learner_collects_one_gain_per_fold() -> None:
    """`Learner` の契約は変えず、通知で gain を外へ出すこと。"""
    data = fake_data(per_season=300)
    collected: list[dict[str, float]] = []
    result = train.walk_forward(data, train.winner_learner(collected), max_folds=2)
    assert len(collected) == len(result.folds)
    assert collected and set(collected[0]) == set(data.features.columns)


# --- 予想スコアの MAE ---

def test_team_score_mae_is_not_the_margin_mae() -> None:
    """**チーム得点の MAE と得点差の MAE を混同しない。**

    付録B の見立て「8〜10点」はチーム得点についてのものである。
    `home = (total + margin) / 2` なので、両者は一致しない。
    """
    from batch.model.evaluate import Evaluation, Fold

    def fold(pred, actual):
        return Fold(
            test_season="s", train_seasons=("a",), valid_season="b",
            n_train=1, n_valid=1, n_test=len(pred), best_iteration=1,
            probs=np.asarray(pred, dtype=np.float64),
            actual=np.asarray(actual, dtype=np.float64),
        )

    # 得点差は 2点ずれ、合計得点は 4点ずれている
    margin = Evaluation(folds=(fold([5.0, 5.0], [3.0, 7.0]),))
    total = Evaluation(folds=(fold([160.0, 160.0], [156.0, 164.0]),))
    assert margin.mae == pytest.approx(2.0)
    assert total.mae == pytest.approx(4.0)
    # ホーム = (合計 + 得点差)/2 の誤差は (4 ± 2)/2、アウェイは (4 ∓ 2)/2
    # → |3|, |1|, |1|, |3| の平均 = 2.0
    assert train.team_score_mae(margin, total) == pytest.approx(2.0)


def test_team_score_mae_is_nan_without_folds() -> None:
    from batch.model.evaluate import Evaluation

    assert math.isnan(train.team_score_mae(Evaluation(), Evaluation()))

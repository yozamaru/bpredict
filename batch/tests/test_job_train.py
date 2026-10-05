"""学習ジョブ（工程8。`batch/jobs/train.py`）。

**実データを読まない。** スナップショットも D1 も触らず、合成した学習行列と
一時ディレクトリだけで検証する。
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, cast

import numpy as np
import pandas as pd
import pytest

from batch.features.team_rate import TARGETS
from batch.jobs import train
from batch.model.criteria import Decision
from batch.model.dataset import TrainingData, time_decay_weights
from batch.model.evaluate import Fold
from batch.model.params import TIME_DECAY_LAMBDA_INITIAL
from batch.model.registry import ModelRecord

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
    for ev in (report.winner, report.home, report.elo, report.lightgbm):
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


# --- 本番モデルは全特徴ロジスティック回帰である（要件 6.1.1） ---

def test_the_winner_is_the_logistic_not_the_lightgbm(report: train.Report) -> None:
    """**`winner` の役が入れ替わったことを固定する。**

    旧版は `winner` が LightGBM で、採用判定も gain もそちらを見ていた。
    A-09 を満たすのは全特徴ロジスティック回帰だけである（要件 6.1.1）ため、
    **名前と役を一致させた**。ここが戻ると、A-09 未達のモデルが「本番」として
    評価され、しかも数字は出るため気づけない。
    """
    reference = train.logistic_learner()
    data = fake_data(per_season=300)
    # **本番と同じ重みで比べる。** `np.ones` で測ると時間減衰が落ちて別物になる
    weights = time_decay_weights(
        data.season_ids, data.seasons, lam=TIME_DECAY_LAMBDA_INITIAL)
    expected = train.walk_forward(data, reference, weights=weights, max_folds=5)
    assert report.winner.brier == pytest.approx(expected.brier)
    # LightGBM は別物である（同じなら役が入れ替わっていない）
    assert report.lightgbm.brier != pytest.approx(expected.brier)


def test_the_gain_comes_from_the_lightgbm_baseline(report: train.Report) -> None:
    """寄与度（gain）は木からしか出ない。**本番モデルの寄与は係数である**（2.7）。

    gain を出すのをやめない — 要件 6.2 の特徴量の採否はこれで測ってきており、
    次に特徴量を足すときの比較相手になる。
    """
    assert report.gain_share
    assert set(report.gain_share) <= set(fake_data(per_season=30).features.columns)


# --- A-09 と判定の順序（要件 6.1） ---

def difference(point: float, low: float, high: float) -> train.Difference:
    return train.Difference(point=point, ci_low=low, ci_high=high)


def test_a09_needs_both_significance_and_the_minimum_effect() -> None:
    """**「有意」だけでは足りず、「0.003 以上」だけでも足りない。**"""
    assert train.meets_a09(difference(0.004, 0.002, 0.006))
    # 有意だが実質差が足りない
    assert not train.meets_a09(difference(0.002, 0.001, 0.003))
    # 実質差はあるが信頼区間が0を跨ぐ
    assert not train.meets_a09(difference(0.004, -0.001, 0.009))


def fold(season: str, probs: np.ndarray, actual: np.ndarray) -> Fold:
    """`Fold` の件数の列はこの検証では使わない。**0 を置いて意味を消す。**"""
    return Fold(
        test_season=season, train_seasons=(), valid_season="",
        n_train=0, n_valid=0, n_test=len(probs),
        best_iteration=0, probs=probs, actual=actual,
    )


def evaluation(brier: float) -> train.Evaluation:
    """Brier だけを持つ `Evaluation` の代用。`adopt_route` は Brier しか見ない。"""
    n = 400
    actual = np.array([1.0, 0.0] * (n // 2))
    # (p - y)^2 の平均が brier になる p を置く
    probs = np.where(actual == 1.0, 1.0 - math.sqrt(brier), math.sqrt(brier))
    return train.Evaluation(folds=(fold("s", probs, actual),))


def test_route_with_no_a09_candidate_is_not_adopted() -> None:
    """**1段目。** A-09 を満たす経路が無ければ、どちらも採らない。

    旧版のコードは2段目だけを書いており、**A-09 を満たさない経路を規則が
    強制しうる**状態だった。
    """
    route, notes = adopt(0.2000, difference(0.001, -0.001, 0.003),
                        0.1990, difference(0.002, -0.000, 0.004))
    assert route == "NONE"
    assert any("候補が0件" in n for n in notes)


def test_single_a09_candidate_is_adopted_even_if_the_other_is_better() -> None:
    """**3段目。** 候補が1つなら、Brier がより良い経路があってもそれを採る。

    2026-10-04 に実際に起きた形である（A が A-09 を満たし、B は信頼区間の下限が
    −0.000045 でわずかに未達だった）。
    """
    route, notes = adopt(0.1987, difference(0.004, 0.002, 0.006),
                        0.1980, difference(0.002, -0.0001, 0.004))
    assert route == "A"
    assert any("候補が1つ" in n for n in notes)


def test_two_candidates_prefer_b_when_the_gap_is_small() -> None:
    """**2段目。** 差が 0.003 未満なら整合性を優先して B。"""
    route, _ = adopt(0.1987, difference(0.004, 0.002, 0.006),
                     0.1990, difference(0.0037, 0.0018, 0.0056))
    assert route == "B"


def test_two_candidates_prefer_a_when_b_is_clearly_worse() -> None:
    """**2段目。** B が 0.003 以上悪ければ A。"""
    route, _ = adopt(0.1950, difference(0.0077, 0.005, 0.010),
                     0.1990, difference(0.0037, 0.0018, 0.0056))
    assert route == "A"


def adopt(
    a_brier: float, a_diff: train.Difference,
    b_brier: float, b_diff: train.Difference,
) -> tuple[str, list[str]]:
    return train.adopt_route((evaluation(a_brier), a_diff),
                             (evaluation(b_brier), b_diff))


# --- 予想スコアは勝率から導く（要件 6.1.1） ---

def test_derived_score_mae_is_reported_alongside_the_old_route(
    report: train.Report,
) -> None:
    """**両方出す。** 片方だけだと、乗り換えで精度が落ちたか分からない。"""
    assert report.derived_score_mae is not None
    assert report.derived_score_mae > 0
    assert "チーム得点 MAE" in train.render(report)
    assert "旧経路" in train.render(report)


def test_derived_score_mae_rejects_mismatched_folds() -> None:
    """fold の並びが違えば落とす。**小さく出ても意味のない MAE を返さない。**"""
    def one(season: str) -> train.Evaluation:
        return train.Evaluation(
            folds=(fold(season, np.array([0.6]), np.array([1.0])),))

    with pytest.raises(train.TrainError):
        train.derived_team_score_mae(one("s1"), one("s2"), one("s1"), 12.9)


# --- 登録（詳細設計 4.5.1） ---

class FakeApi:
    """`InternalApi.post` の代わり。**D1 にも HTTP にも触らない。**"""

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, object]]] = []

    def post(self, path: str, payload: object) -> object:
        assert isinstance(payload, dict)
        self.sent.append((path, payload))
        return {"version": payload.get("version")}


def fake_report(*, adopt: bool) -> train.Report:
    """登録の分岐だけを見るための最小の `Report`。"""
    from batch.model.criteria import Decision

    data = fake_data(per_season=40, seasons=("s1", "s2", "s3"))
    ev = train.walk_forward(
        data, train.logistic_learner(), weights=np.ones(len(data)), max_folds=5)
    scores = train.walk_forward(
        data, lambda tx, ty, tw, vx, vy: (
            (lambda f: np.full(len(f), 0.0)), 80),
        target=data.margin, weights=np.ones(len(data)), max_folds=5)
    return train.Report(
        n=ev.n, seasons=data.seasons,
        test_seasons=[f.test_season for f in ev.folds],
        winner=ev, home=ev, elo=ev, lightgbm=ev,
        ece_floor=0.03, difference=difference(0.004, 0.002, 0.006),
        null_rates={"elo_diff": 0.0}, constant_columns=[],
        decision=Decision(adopt=adopt, failures=[] if adopt else ["Brier"], notes=[]),
        margin=scores, total=scores, margin_sigma=12.9,
    )


def fake_rate_report(*, adopt: bool = True) -> train.RateReport:
    """30本の判定だけを持つ `RateReport`（評価そのものは別のテストで見る）。"""
    names = ["PLAYER_AVAIL", "PLAYER_MIN"]
    for target in TARGETS:
        names += [f"TEAM_RATE/{target}", f"PLAYER_RATE/{target}"]
    assert len(names) == 30
    decisions = {
        name: Decision(adopt=True, failures=[], notes=[]) for name in names
    }
    if not adopt:
        decisions["TEAM_RATE/fg2a"] = Decision(
            adopt=False, failures=["想定外の定数列がある（own_fg2a_l10）"], notes=[])
    return train.RateReport(
        evaluations=cast("train.RateEvaluations", object()),
        decisions=decisions,
        rows={name: 1200 for name in names},
    )


def fake_rate_records(count: int = 30) -> list[ModelRecord]:
    """30本ぶんの `ModelRecord`（中身は登録の検査に使わない）。"""
    out: list[ModelRecord] = []
    for index in range(count):
        out.append(ModelRecord(
            version=f"team_rate-x{index}-v1.0.0", model_type="TEAM_RATE",
            target=f"x{index}", algo="lightgbm", trained_at="2026-10-05T00:00:00Z",
            train_rows=1200, train_range="s1..s3", eval_window="s3..s3",
            params={}, feature_list=["pace_own"], artifact_text="tree\n",
        ))
    return out


def test_register_sends_nothing_when_the_criteria_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**基準未達なら1本も送らない。** 現行モデルを継続する（詳細設計 4.5.1）。

    「登録だけしておく」経路を作らない — `model_versions` に有効でない行が溜まると、
    どれが本番かを `is_active` 以外で判断する余地が生まれる。
    """
    api = FakeApi()
    data = fake_data(per_season=40, seasons=("s1", "s2", "s3"))
    versions = train.register_models(
        data, fake_report(adopt=False), api=api,  # type: ignore[arg-type]
        rates=fake_rate_report(), matrices=cast("Any", (1, 2, 3, 4)),
        log=lambda _m: None)
    assert versions == []
    assert api.sent == []


def test_register_sends_nothing_without_the_rate_evaluation() -> None:
    """**30本の評価が無ければ登録しない**（詳細設計 4.5.1）。

    勝敗の3本だけを入れ替えると、登録済みの30本（前の世代）と組み合わさる。
    `prediction_model_bundle` が1予測につき33行を記録することに意味があるのは、
    33本が同じ世代であるときだけである。
    """
    api = FakeApi()
    data = fake_data(per_season=40, seasons=("s1", "s2", "s3"))
    lines: list[str] = []
    versions = train.register_models(
        data, fake_report(adopt=True), api=api,  # type: ignore[arg-type]
        log=lines.append)
    assert versions == []
    assert api.sent == []
    assert any("30本の評価がない" in line for line in lines)


def test_register_sends_nothing_when_one_rate_model_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**1本でも落ちたら33本とも登録しない**（詳細設計 4.5.1）。

    個人スタッツはチーム予測に整合化してから保存するため（2.4）、一部だけ
    世代を入れ替えると整合化の両側が別の世代のモデルから出る。
    """
    monkeypatch.setattr(
        "batch.model.final.train_final",
        lambda f, a, w, *, num_boost_round, params=None: (object(), "tree\n"),
    )
    api = FakeApi()
    data = fake_data(per_season=40, seasons=("s1", "s2", "s3"))
    lines: list[str] = []
    versions = train.register_models(
        data, fake_report(adopt=True), api=api,  # type: ignore[arg-type]
        rates=fake_rate_report(adopt=False), matrices=cast("Any", (1, 2, 3, 4)),
        log=lines.append)
    assert versions == []
    assert api.sent == []
    assert any("33本とも登録しない" in line for line in lines)


def test_register_activates_all_thirty_three(monkeypatch: pytest.MonkeyPatch) -> None:
    """判定を通ったら**33本とも** `activate=True` で送る（詳細設計 4.5.1）。"""
    monkeypatch.setattr(
        "batch.model.final.train_final",
        lambda f, a, w, *, num_boost_round, params=None: (object(), "tree\n"),
    )
    monkeypatch.setattr(
        train, "build_rate_records", lambda **_k: fake_rate_records())
    api = FakeApi()
    data = fake_data(per_season=40, seasons=("s1", "s2", "s3"))
    versions = train.register_models(
        data, fake_report(adopt=True), api=api,  # type: ignore[arg-type]
        rates=fake_rate_report(), matrices=cast("Any", (1, 2, 3, 4)),
        log=lambda _m: None)
    assert versions[:3] == ["winner-v1.0.0", "margin-v1.0.0", "total-v1.0.0"]
    assert len(versions) == 33
    assert [p for p, _ in api.sent] == ["models"] * 33
    assert all(body["activate"] is True for _p, body in api.sent)


def test_register_refuses_a_count_other_than_thirty_three(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**本数を検査する**（詳細設計 4.5.1）。

    14本のどれかが静かに抜けると、推論が「30本が揃っていない」として個人
    スタッツを出さなくなる（4.2）。**気づけるように落とす。**
    """
    monkeypatch.setattr(
        "batch.model.final.train_final",
        lambda f, a, w, *, num_boost_round, params=None: (object(), "tree\n"),
    )
    monkeypatch.setattr(
        train, "build_rate_records", lambda **_k: fake_rate_records(29))
    api = FakeApi()
    data = fake_data(per_season=40, seasons=("s1", "s2", "s3"))
    with pytest.raises(train.TrainError, match="33"):
        train.register_models(
            data, fake_report(adopt=True), api=api,  # type: ignore[arg-type]
            rates=fake_rate_report(), matrices=cast("Any", (1, 2, 3, 4)),
            log=lambda _m: None)


def test_an_unmet_gate_is_not_a_failure() -> None:
    """**基準未達は exit 0 である**（詳細設計 4.5 / 4.6）。

    v1.101 まで exit 1 を返しており、**月次 cron が通常の結果を毎月「失敗」として
    通知する**状態だった（実際に 2026-10-04 の実行が failure で終わった）。
    4.5 は「新モデルを有効化せず現行を継続する」と定めるだけで `PARTIAL` も
    exit 1 も求めておらず、要件 6.5 は**見逃す方向に倒すのは意図的な設計**だと
    書いている。**毎月オオカミ少年をやると、本当の失敗が読み飛ばされる。**
    """
    lines: list[str] = []
    assert train.exit_code(fake_report(adopt=False), log=lines.append) == 0
    assert any("現行モデルを継続する" in line for line in lines)


def test_a_blocked_comparison_is_a_failure() -> None:
    """**「比較できなかった」は設定の誤りであり通知に乗せる**（詳細設計 4.6）。

    列を変えて `--initial` を忘れた状態がこれである。基準未達と同じ扱いにすると、
    気づかないまま条件1〜2 が課されない状態が続く。
    """
    report = train.replace(
        fake_report(adopt=False),
        comparison_blocked="現行モデルの列が今の行列に無い（3列）",
    )
    assert train.exit_code(report, log=lambda _m: None) == 1


def test_register_needs_the_score_evaluations() -> None:
    """得点差・合計得点の評価がなければ登録できない（σ が出ない）。"""
    report = fake_report(adopt=True)
    without = train.replace(report, margin=None, total=None)
    with pytest.raises(train.TrainError):
        train.evaluations_of(without)

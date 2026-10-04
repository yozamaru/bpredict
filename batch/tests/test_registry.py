"""モデルの保存・読み出し（基本設計 2.3 / 詳細設計 1.6・3.4・4.5）。

| テスト | どの規約か |
|---|---|
| `test_round_trip_keeps_the_predictions` | 保存して戻しても予測が一致する |
| `test_load_rejects_a_different_feature_order` | **列の順序がずれたまま通さない** |
| `test_oversized_artifact_is_rejected` | 1.5MB 上限（A-18） |
| `test_payload_matches_the_zod_schema` | 本文のキーが api 側の Zod と対応する |
| `test_register_does_not_activate_by_default` | 既定で有効化しない |
| `test_fetch_artifact_checks_the_digest` | ハッシュを照合する |
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from batch.model.baselines import Logistic
from batch.model.registry import (
    ARTIFACT_MAX_BYTES,
    ModelRecord,
    RegistryError,
    active_models,
    artifact_sha256,
    check_artifact_size,
    dump_logistic,
    fetch_artifact,
    load_logistic,
    load_logistic_means,
    payload_of,
    register,
)

FEATURES = ["elo_diff", "winrate_season_diff", "sos_diff"]


def model() -> Logistic:
    return Logistic(intercept=0.12, coefficients=np.array([0.003, 1.5, -0.002]))


#: 学習データの重みなし列平均（詳細設計 2.7.1）。根拠の寄与と `base_value` が要する。
MEANS = np.array([12.0, 0.01, -3.0])


def record(**over: Any) -> ModelRecord:
    base: dict[str, Any] = {
        "version": "winner-v1.0.0",
        "model_type": "WINNER",
        "algo": "logistic",
        "trained_at": "2026-10-04T00:00:00Z",
        "train_rows": 6270,
        "train_range": "2016-17..2026-27",
        "eval_window": "2024-25..2026-27",
        "params": {"l2": 1e-4, "max_iterations": 100, "tolerance": 1e-8},
        "feature_list": FEATURES,
        "artifact_text": dump_logistic(model(), FEATURES, MEANS),
        "win_prob_source": "WINNER",
    }
    base.update(over)
    return ModelRecord(**base)


class FakeApi:
    """`InternalApi` の代わり。**送った内容を覚えるだけ。**"""

    def __init__(self, responses: dict[str, object] | None = None) -> None:
        self.posted: list[tuple[str, dict[str, Any]]] = []
        self.responses = responses or {}

    def post(self, path: str, payload: Any) -> None:
        self.posted.append((path, dict(payload)))

    def get(self, path: str, query: Any = None) -> object:
        return self.responses[path]


# --- 保存と読み出し ---

def test_round_trip_keeps_the_predictions() -> None:
    """保存して戻したモデルが、元と同じ確率を返すこと。"""
    original = model()
    restored = load_logistic(dump_logistic(original, FEATURES, MEANS), FEATURES)
    x = np.array([[120.0, 0.08, 15.0], [-60.0, -0.05, -8.0]])
    assert np.allclose(original.predict(x), restored.predict(x))


def test_the_artifact_is_small() -> None:
    """**係数と切片だけなので小さい。** 1.5MB 上限は LightGBM 側の制約である。"""
    assert len(dump_logistic(model(), FEATURES, MEANS).encode("utf-8")) < 1024


def test_load_rejects_a_different_feature_order() -> None:
    """**列の順序がずれたまま通さない。**

    通ってしまうのが最も悪い壊れ方である — 推論は成功し、値だけが別物になる。
    """
    text = dump_logistic(model(), FEATURES, MEANS)
    with pytest.raises(RegistryError):
        load_logistic(text, [FEATURES[1], FEATURES[0], FEATURES[2]])


def test_load_rejects_an_added_feature() -> None:
    text = dump_logistic(model(), FEATURES, MEANS)
    with pytest.raises(RegistryError):
        load_logistic(text, [*FEATURES, "新しい列"])


def test_load_rejects_another_format() -> None:
    """LightGBM のテキストモデルを読まされても落ちること。"""
    with pytest.raises(RegistryError):
        load_logistic("tree\nversion=v4\n", FEATURES)


def test_load_rejects_a_future_artifact_version() -> None:
    """**知らない版を黙って読まない。**"""
    payload = json.loads(dump_logistic(model(), FEATURES, MEANS))
    payload["version"] = 99
    with pytest.raises(RegistryError):
        load_logistic(json.dumps(payload), FEATURES)


def test_dump_rejects_a_mismatched_feature_list() -> None:
    with pytest.raises(RegistryError):
        dump_logistic(model(), FEATURES[:2], MEANS[:2])


# --- 学習データの平均（詳細設計 2.7.1） ---


def test_the_means_round_trip() -> None:
    """**根拠の寄与と `base_value` がこれを要する。** 推論時には作り直せない。"""
    text = dump_logistic(model(), FEATURES, MEANS)
    assert np.allclose(load_logistic_means(text, FEATURES), MEANS)


def test_dump_rejects_a_mismatched_mean_count() -> None:
    with pytest.raises(RegistryError):
        dump_logistic(model(), FEATURES, MEANS[:2])


def test_dump_rejects_a_non_finite_mean() -> None:
    with pytest.raises(RegistryError):
        dump_logistic(model(), FEATURES, np.array([1.0, float("nan"), 3.0]))


def test_an_artifact_without_means_is_not_read() -> None:
    """**版1 を読まない**（2.7.1）。

    「means が無ければ根拠を作らない」という分岐を置くと、**根拠が静かに
    出なくなる**状態が生まれる。版を上げ、登録し直す側に倒す。
    """
    payload = json.loads(dump_logistic(model(), FEATURES, MEANS))
    del payload["means"]
    payload["version"] = 1
    text = json.dumps(payload)
    with pytest.raises(RegistryError):
        load_logistic(text, FEATURES)
    with pytest.raises(RegistryError):
        load_logistic_means(text, FEATURES)


def test_load_means_rejects_a_truncated_list() -> None:
    payload = json.loads(dump_logistic(model(), FEATURES, MEANS))
    payload["means"] = payload["means"][:2]
    with pytest.raises(RegistryError):
        load_logistic_means(json.dumps(payload), FEATURES)


# --- サイズ上限（A-18） ---

def test_oversized_artifact_is_rejected() -> None:
    with pytest.raises(RegistryError):
        check_artifact_size("x" * (ARTIFACT_MAX_BYTES + 1))


def test_size_is_measured_in_bytes_not_characters() -> None:
    """**バイト数で測る。** 文字数で測ると多バイト文字で上限を超えて通る。"""
    text = "あ" * (ARTIFACT_MAX_BYTES // 3 + 1)      # UTF-8 で3バイト/文字
    assert len(text) < ARTIFACT_MAX_BYTES
    with pytest.raises(RegistryError):
        check_artifact_size(text)


def test_payload_refuses_an_oversized_artifact() -> None:
    with pytest.raises(RegistryError):
        payload_of(record(artifact_text="x" * (ARTIFACT_MAX_BYTES + 1)), activate=False)


# --- 送る本文 ---

def test_payload_matches_the_zod_schema() -> None:
    """**本文のキーが api 側の Zod と対応すること。**

    片方だけ直すと本番で 400 を受けて初めて分かる（詳細設計 3.7 と同じ問題）。
    Zod は `.strict()` なので、**スキーマに無いキーを送ると拒否される**。
    """
    schema = (Path(__file__).resolve().parents[2]
              / "api" / "src" / "schemas" / "models.ts").read_text(encoding="utf-8")
    body = payload_of(record(), activate=True)
    for key in body:
        assert f"{key}:" in schema, f"Zod に無いキーを送っている: {key}"


def test_payload_carries_the_digest() -> None:
    body = payload_of(record(), activate=False)
    assert body["artifactSha256"] == artifact_sha256(str(body["artifactText"]))


def test_payload_omits_absent_optionals() -> None:
    """**None を送らない。** Zod は nullable を許すが、送らない方が意図が明確である。"""
    body = payload_of(record(margin_sigma=None, notes=None), activate=False)
    assert "marginSigma" not in body
    assert "notes" not in body


def test_payload_serialises_json_columns_as_text() -> None:
    body = payload_of(record(), activate=False)
    assert json.loads(str(body["featureList"])) == FEATURES
    assert json.loads(str(body["params"]))["l2"] == pytest.approx(1e-4)


# --- 登録 ---

def test_register_does_not_activate_by_default() -> None:
    """**既定で有効化しない。** 採用基準を通ったものだけを有効にする（4.6）。"""
    api = FakeApi()
    register(api, record())
    path, body = api.posted[0]
    assert path == "models"
    assert body["activate"] is False


def test_register_can_activate() -> None:
    api = FakeApi()
    register(api, record(), activate=True)
    assert api.posted[0][1]["activate"] is True


# --- 読み出し ---

def test_active_models_returns_the_rows() -> None:
    api = FakeApi({"models/active": {"models": [{"version": "winner-v1.0.0"}]}})
    assert active_models(api)[0]["version"] == "winner-v1.0.0"


def test_active_models_rejects_a_broken_response() -> None:
    with pytest.raises(RegistryError):
        active_models(FakeApi({"models/active": {"nope": 1}}))


def test_fetch_artifact_checks_the_digest() -> None:
    """**ハッシュを照合する。** 壊れた artifact をそのまま推論に使わない。"""
    text = dump_logistic(model(), FEATURES, MEANS)
    api = FakeApi({
        "models/winner-v1.0.0/artifact": {
            "artifactText": text, "artifactSha256": "0" * 64,
        },
    })
    with pytest.raises(RegistryError):
        fetch_artifact(api, "winner-v1.0.0")


def test_fetch_artifact_returns_the_text_when_the_digest_matches() -> None:
    text = dump_logistic(model(), FEATURES, MEANS)
    api = FakeApi({
        "models/winner-v1.0.0/artifact": {
            "artifactText": text, "artifactSha256": artifact_sha256(text),
        },
    })
    assert fetch_artifact(api, "winner-v1.0.0") == text


# --- 契約（詳細設計 3.7 / 4.5.1） ---

CONTRACT = Path(__file__).resolve().parents[2] / "contracts" / "public-shapes.json"


def contract(shape: str) -> set[str]:
    return set(json.loads(CONTRACT.read_text(encoding="utf-8"))[shape]["paths"])


def full_record() -> ModelRecord:
    """**すべての任意項目を埋めた**記録。契約はこれで固定する。

    `payload_of` は None の項目を落とすため、実際に送る本文はこの部分集合になる。
    固定するのは「`payload_of` が Zod のどのキーに写すか」である。
    """
    return ModelRecord(
        version="winner-v1.0.0", model_type="WINNER", algo="logistic",
        trained_at="2026-10-05T00:00:00Z", train_rows=3031,
        train_range="s1..s3", eval_window="s2..s3",
        params={"l2": 1e-4}, feature_list=["elo_diff"],
        artifact_text="{}", target="", league="PREMIER",
        win_prob_source="WINNER", margin_sigma=12.9,
        feature_null_rates={"elo_diff": 0.0},
        cv_accuracy=0.69, cv_brier=0.199, cv_logloss=0.58, cv_ece=0.025,
        baseline_home_accuracy=0.52, baseline_elo_brier=0.2027,
        calibrator="{}", notes="note",
    )


def test_payload_matches_the_contract() -> None:
    """**`api/src/schemas/models.ts` の Zod と1対1で対応させる。**

    片方だけ直すと本番で 400 を受けて初めて分かる（詳細設計 3.7 と同じ問題）。
    api 側は契約から組んだ本文が 200 で通ることまで見る。
    """
    assert set(payload_of(full_record(), activate=True)) == contract("internalModels")


def test_every_sent_key_is_in_the_contract() -> None:
    """任意項目が欠けた本文でも、**契約に無いキーは出さない**。"""
    lean = ModelRecord(
        version="margin-v1.0.0", model_type="MARGIN", algo="lightgbm",
        trained_at="2026-10-05T00:00:00Z", train_rows=10,
        train_range="s1..s3", eval_window="s2..s3",
        params={}, feature_list=["elo_diff"],
    )
    assert set(payload_of(lean, activate=False)) <= contract("internalModels")

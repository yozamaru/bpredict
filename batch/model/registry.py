"""モデルの保存・読み出し・バージョン管理（基本設計 2.3 / 詳細設計 1.6・3.4・4.5）。

**D1 へは Workers の `/internal/*` 経由でしか触らない**（絶対ルール3）。登録は
`POST /internal/models`、読み出しは `GET /internal/models/active` と
`GET /internal/models/:version/artifact` である。

**勝敗モデルは全特徴ロジスティック回帰である**（要件 6.1.1）。artifact は係数と
切片の JSON で、22列なら1KB未満に収まる。1.5MB 上限（A-18）は実際には LightGBM 側
だけの制約になるが、**判定は全モデルに課す**（詳細設計 4.5）。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

import numpy as np

from batch.model.baselines import Logistic

#: `model_versions.artifact_text` の上限（詳細設計 1.6 / 受け入れ基準 A-18）。
#: **アプリ層・Workers・DBトリガの三重で同じ値を見る。**
ARTIFACT_MAX_BYTES = 1_572_864

#: artifact の書式の版。**読み出し側が知らない版を黙って読まないため**に持つ。
LOGISTIC_ARTIFACT_VERSION = 1


class RegistryError(RuntimeError):
    """登録・読み出しの失敗。**例外に本文を入れない**（絶対ルール4）。"""


@dataclass(frozen=True)
class ModelRecord:
    """`model_versions` の1行として送る内容。

    **`version` は呼び出し側が決める。** ここで採番すると、同じ学習結果を2回
    登録したときに別の版として2行入る（冪等でなくなる）。
    """

    version: str
    model_type: str
    algo: str
    trained_at: str
    train_rows: int
    train_range: str
    eval_window: str
    params: dict[str, Any]
    feature_list: list[str]
    artifact_text: str | None = None
    target: str = ""
    league: str = "PREMIER"
    win_prob_source: str | None = None
    margin_sigma: float | None = None
    feature_null_rates: dict[str, float] | None = None
    cv_accuracy: float | None = None
    cv_brier: float | None = None
    cv_logloss: float | None = None
    cv_ece: float | None = None
    baseline_home_accuracy: float | None = None
    baseline_elo_brier: float | None = None
    calibrator: str | None = None
    notes: str | None = None


def dump_logistic(model: Logistic, feature_list: list[str]) -> str:
    """ロジスティック回帰を JSON にする。

    **列名を一緒に保存する。** 係数は列の順序に意味があり、順序が変わった行列へ
    当てはめると**静かに別のモデルになる**。読み出し時に照合する（`load_logistic`）。
    """
    if model.coefficients.size != len(feature_list):
        raise RegistryError("係数の数と特徴量の数が合わない")
    return json.dumps(
        {
            "format": "logistic",
            "version": LOGISTIC_ARTIFACT_VERSION,
            "intercept": float(model.intercept),
            "features": list(feature_list),
            "coefficients": [float(c) for c in model.coefficients],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def load_logistic(artifact_text: str, feature_list: list[str]) -> Logistic:
    """JSON からロジスティック回帰を戻す。**列名と順序を照合する。**

    照合しないと、特徴量を1つ足したあとに古い artifact を読んで**列がずれたまま
    推論が通る**。通ってしまうのが最も悪い壊れ方である。
    """
    try:
        payload = json.loads(artifact_text)
    except json.JSONDecodeError as error:
        raise RegistryError("artifact が JSON として読めない") from error
    if not isinstance(payload, dict):
        raise RegistryError("artifact が辞書でない")
    if payload.get("format") != "logistic":
        raise RegistryError("artifact の書式がロジスティック回帰でない")
    if payload.get("version") != LOGISTIC_ARTIFACT_VERSION:
        raise RegistryError("artifact の書式の版が読み出し側と違う")
    features = payload.get("features")
    if features != list(feature_list):
        raise RegistryError("artifact の特徴量が呼び出し側と一致しない")
    coefficients = payload.get("coefficients")
    intercept = payload.get("intercept")
    if not isinstance(coefficients, list) or not isinstance(intercept, (int, float)):
        raise RegistryError("artifact に係数または切片がない")
    return Logistic(
        intercept=float(intercept),
        coefficients=np.asarray(coefficients, dtype=np.float64),
    )


def artifact_sha256(artifact_text: str) -> str:
    return hashlib.sha256(artifact_text.encode("utf-8")).hexdigest()


def check_artifact_size(artifact_text: str) -> int:
    """バイト数で測り、上限を超えたら拒否する（A-18）。

    **文字数で測らない。** 日本語を含む `notes` のような列ではないが、
    バイト数と文字数が違う書式に将来変わったときに静かに通る。
    """
    size = len(artifact_text.encode("utf-8"))
    if size > ARTIFACT_MAX_BYTES:
        raise RegistryError(f"artifact が上限を超えている: {size} > {ARTIFACT_MAX_BYTES}")
    return size


def payload_of(record: ModelRecord, *, activate: bool) -> dict[str, Any]:
    """`POST /internal/models` に送る本文（詳細設計 3.4）。

    **`api/src/schemas/models.ts` の Zod と1対1で対応させる。** 片方だけ直すと
    本番で 400 を受けて初めて分かる（3.7 の契約ファイルと同じ問題）。
    """
    body: dict[str, Any] = {
        "version": record.version,
        "modelType": record.model_type,
        "target": record.target,
        "league": record.league,
        "algo": record.algo,
        "trainedAt": record.trained_at,
        "trainRows": record.train_rows,
        "trainRange": record.train_range,
        "evalWindow": record.eval_window,
        "params": json.dumps(record.params, ensure_ascii=False, sort_keys=True),
        "featureList": json.dumps(record.feature_list, ensure_ascii=False),
        "activate": activate,
    }
    if record.artifact_text is not None:
        check_artifact_size(record.artifact_text)
        body["artifactText"] = record.artifact_text
        body["artifactSha256"] = artifact_sha256(record.artifact_text)
    optional = {
        "winProbSource": record.win_prob_source,
        "marginSigma": record.margin_sigma,
        "cvAccuracy": record.cv_accuracy,
        "cvBrier": record.cv_brier,
        "cvLogloss": record.cv_logloss,
        "cvEce": record.cv_ece,
        "baselineHomeAccuracy": record.baseline_home_accuracy,
        "baselineEloBrier": record.baseline_elo_brier,
        "calibrator": record.calibrator,
        "notes": record.notes,
    }
    body.update({k: v for k, v in optional.items() if v is not None})
    if record.feature_null_rates is not None:
        body["featureNullRates"] = json.dumps(
            record.feature_null_rates, ensure_ascii=False, sort_keys=True)
    return body


# --- Workers 経由の読み書き（絶対ルール3。D1 を直接叩かない） ---


def register(api: Any, record: ModelRecord, *, activate: bool = False) -> int:
    """`POST /internal/models` で登録する。送った artifact のバイト数を返す。

    **`activate` の既定は False。** 登録と有効化を分けるのは、採用基準の判定
    （詳細設計 4.6）を通ったものだけを有効にするためである。**既定で有効化すると、
    基準未達のモデルが本番に入る経路ができる。**
    """
    api.post("models", payload_of(record, activate=activate))
    return 0 if record.artifact_text is None else len(record.artifact_text.encode("utf-8"))


def active_models(api: Any, league: str = "PREMIER") -> list[dict[str, Any]]:
    """`GET /internal/models/active`（メタのみ。artifact は含まれない）。"""
    data = api.get("models/active", {"league": league})
    if not isinstance(data, dict):
        raise RegistryError("有効モデルの応答が辞書でない")
    models = data.get("models")
    if not isinstance(models, list):
        raise RegistryError("有効モデルの応答に models がない")
    return [m for m in models if isinstance(m, dict)]


def fetch_artifact(api: Any, version: str, *, expected_sha256: str | None = None) -> str:
    """`GET /internal/models/:version/artifact` で本体を1本だけ取る。

    **`artifactSha256` を照合する**（詳細設計 3.4）。照合しないと、途中で壊れた
    artifact をそのまま推論に使う。
    """
    data = api.get(f"models/{version}/artifact")
    if not isinstance(data, dict):
        raise RegistryError("artifact の応答が辞書でない")
    text = data.get("artifactText")
    if not isinstance(text, str):
        raise RegistryError("artifact の応答に artifactText がない")
    digest = expected_sha256 or data.get("artifactSha256")
    if isinstance(digest, str) and digest and artifact_sha256(text) != digest:
        raise RegistryError("artifact のハッシュが一致しない")
    return text

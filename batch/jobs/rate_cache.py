"""30本の学習行列の構築とキャッシュ（詳細設計 4.5.1 / 2.2.1 / 2.3.1）。

**4枚の行列は作るのに時間がかかる。** 実測で第3段が 1,502秒（127,467行）、
第2段が 324秒である。`monthly_train` のタイムアウトは120分であり（要件 6.8.7）、
毎回4枚を作り直すと評価の時間が残らない。

**鍵は `train.py` と同じ2つ** — スナップショットの MANIFEST と、その行列が
読む特徴量のキー一覧である。**スナップショットだけを鍵にすると、特徴量を
増やしたときに古い行列が読まれる**（`train.py` の `feature_digest` と同じ理由。
しかも落ちないため気づけない）。

**pickle を使う。** 勝敗の行列は1枚の表なので parquet に収まるが、ここの
4枚は複数の DataFrame と list を持つ dataclass であり、parquet では
「どの列がどのフィールドか」を別に持つ必要がある。**置き場は
`batch/cache/`（`.gitignore` の対象）であり、自分が今作った物を自分で読み直す
だけである** — 外から来たファイルを読む経路ではない。
"""

from __future__ import annotations

import hashlib
import pickle
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from batch.features import player_rate, team_rate
from batch.features.dataset import Dataset, load_snapshot
from batch.model.dataset import (
    PlayerAvailData,
    PlayerMinutesData,
    PlayerRateData,
    TeamRateData,
    build_player_avail_matrix,
    build_player_minutes_matrix,
    build_player_rate_matrix,
    build_team_rate_matrix,
)

#: キャッシュの版。**行列の形を変えたら上げる。** 特徴量のキーが同じでも
#: dataclass のフィールドが変わると `pickle` の読み出しが静かに別物になる
CACHE_FORMAT = 2


class RateCacheError(RuntimeError):
    """行列を作れない／読めない。"""


def _digest(keys: Sequence[str]) -> str:
    return hashlib.sha256("\n".join(keys).encode("utf-8")).hexdigest()


#: 名前 → （組み立て関数, 鍵になるキー一覧）。**4枚を別々に鍵付けする** —
#: 第3段の列を変えたときに第2段まで作り直す必要はない
BUILDERS: dict[str, tuple[Callable[[Dataset], Any], Callable[[], Sequence[str]]]] = {
    "team-rate": (build_team_rate_matrix, lambda: team_rate.all_feature_keys()),
    "player-avail": (build_player_avail_matrix, lambda: player_rate.AVAIL_KEYS),
    "player-minutes": (build_player_minutes_matrix, lambda: player_rate.MINUTES_KEYS),
    "player-rate": (build_player_rate_matrix, lambda: player_rate.all_rate_matrix_keys()),
}


def load_or_build(
    name: str, *, snapshot: Path, cache_dir: Path, manifest_sha256: str,
    ds: Dataset | None = None, refresh: bool = False,
    log: Callable[[str], None] = print,
) -> Any:
    """1枚を読むか作る。**鍵が一致しなければ黙って捨てて作り直す。**"""
    if name not in BUILDERS:
        raise RateCacheError(f"知らない行列: {name}")
    build, keys = BUILDERS[name]
    path = cache_dir / f"{name}.pickle"
    meta = {
        "format": CACHE_FORMAT,
        "manifest_sha256": manifest_sha256,
        "feature_sha256": _digest(list(keys())),
    }
    if not refresh and path.exists():
        try:
            with path.open("rb") as handle:
                stored = pickle.load(handle)
        except Exception:  # noqa: BLE001 — 壊れていれば作り直す（型名も出さない）
            stored = None
        if isinstance(stored, dict) and stored.get("meta") == meta:
            data = stored["data"]
            log(f"train: {name} はキャッシュを使う（{len(data):,} 行）")
            return data

    log(f"train: {name} を作る（キャッシュが無効か未作成）")
    started = time.monotonic()
    data = build(ds if ds is not None else load_snapshot(snapshot))
    log(f"train: {name} を作った（{len(data):,} 行 / {time.monotonic() - started:.0f}秒）")
    cache_dir.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        pickle.dump({"meta": meta, "data": data}, handle)
    return data


def load_all(
    *, snapshot: Path, cache_dir: Path, manifest_sha256: str,
    refresh: bool = False, log: Callable[[str], None] = print,
) -> tuple[TeamRateData, PlayerAvailData, PlayerMinutesData, PlayerRateData]:
    """4枚まとめて。**スナップショットは必要になったとき1回だけ読む。**"""
    ds: Dataset | None = None

    def dataset() -> Dataset:
        nonlocal ds
        if ds is None:
            ds = load_snapshot(snapshot)
        return ds

    out: list[Any] = []
    for name in ("team-rate", "player-avail", "player-minutes", "player-rate"):
        path = cache_dir / f"{name}.pickle"
        # 作る必要があるときだけスナップショットを読む
        needed = refresh or not path.exists()
        out.append(load_or_build(
            name, snapshot=snapshot, cache_dir=cache_dir,
            manifest_sha256=manifest_sha256,
            ds=dataset() if needed else None, refresh=refresh, log=log,
        ))
    return out[0], out[1], out[2], out[3]

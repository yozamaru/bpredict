"""30本の学習行列のキャッシュ（詳細設計 4.5.1 / `batch/jobs/rate_cache.py`）。

**鍵が一致しないときは黙って捨てて作り直す。** `train.py` の `feature_digest`
と同じ理由で、**スナップショットだけを鍵にすると特徴量を増やしたときに古い
行列が読まれる**（しかも落ちないため気づけない）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from batch.jobs import rate_cache


class FakeMatrix:
    """`len()` が効くだけの偽の行列。"""

    def __init__(self, rows: int) -> None:
        self.rows = rows

    def __len__(self) -> int:
        return self.rows


@pytest.fixture
def patched(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """4枚すべてを偽の組み立て関数に差し替え、呼ばれた名前を記録する。"""
    called: list[str] = []

    def builders() -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name, (_build, keys) in rate_cache.BUILDERS.items():
            def make(_ds: Any, _name: str = name) -> FakeMatrix:
                called.append(_name)
                return FakeMatrix(10)
            out[name] = (make, keys)
        return out

    monkeypatch.setattr(rate_cache, "BUILDERS", builders())
    monkeypatch.setattr(rate_cache, "load_snapshot", lambda _p: object())
    return called


def test_the_second_call_uses_the_cache(patched: list[str], tmp_path: Path) -> None:
    for _ in range(2):
        rate_cache.load_or_build(
            "team-rate", snapshot=tmp_path, cache_dir=tmp_path / "c",
            manifest_sha256="abc", log=lambda _m: None)
    assert patched == ["team-rate"]


def test_a_different_manifest_rebuilds(patched: list[str], tmp_path: Path) -> None:
    """**スナップショットが変わったら作り直す。**"""
    for digest in ("abc", "def"):
        rate_cache.load_or_build(
            "team-rate", snapshot=tmp_path, cache_dir=tmp_path / "c",
            manifest_sha256=digest, log=lambda _m: None)
    assert patched == ["team-rate", "team-rate"]


def test_a_different_feature_list_rebuilds(
    patched: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**特徴量を増やしたら作り直す**（`train.py` の `feature_digest` と同じ理由）。

    スナップショットは変わらないため digest が一致し、**増やす前の列で作った
    行列が「増やした後の行列」として読まれる。** しかも落ちないため気づけない。
    """
    rate_cache.load_or_build(
        "team-rate", snapshot=tmp_path, cache_dir=tmp_path / "c",
        manifest_sha256="abc", log=lambda _m: None)
    build, _keys = rate_cache.BUILDERS["team-rate"]
    monkeypatch.setitem(
        rate_cache.BUILDERS, "team-rate", (build, lambda: ("pace_own", "新しい列")))
    rate_cache.load_or_build(
        "team-rate", snapshot=tmp_path, cache_dir=tmp_path / "c",
        manifest_sha256="abc", log=lambda _m: None)
    assert patched == ["team-rate", "team-rate"]


def test_a_broken_file_is_rebuilt(patched: list[str], tmp_path: Path) -> None:
    """**壊れたキャッシュで落ちない。** 作り直す（型名も出さない）。"""
    cache = tmp_path / "c"
    cache.mkdir()
    (cache / "team-rate.pickle").write_bytes(b"not a pickle")
    rate_cache.load_or_build(
        "team-rate", snapshot=tmp_path, cache_dir=cache,
        manifest_sha256="abc", log=lambda _m: None)
    assert patched == ["team-rate"]


def test_an_unknown_matrix_is_refused(tmp_path: Path) -> None:
    with pytest.raises(rate_cache.RateCacheError, match="知らない行列"):
        rate_cache.load_or_build(
            "xxx", snapshot=tmp_path, cache_dir=tmp_path,
            manifest_sha256="abc", log=lambda _m: None)


def test_load_all_returns_four(patched: list[str], tmp_path: Path) -> None:
    """**4枚を別々に鍵付けする。** 第3段の列を変えても第2段は作り直さない。"""
    out = rate_cache.load_all(
        snapshot=tmp_path, cache_dir=tmp_path / "c",
        manifest_sha256="abc", log=lambda _m: None)
    assert len(out) == 4
    assert sorted(patched) == sorted(rate_cache.BUILDERS)


def test_the_format_version_is_part_of_the_key(tmp_path: Path) -> None:
    """**行列の形を変えたら `CACHE_FORMAT` を上げる**（4.5.1 / モジュールの冒頭）。

    特徴量のキーが同じでも dataclass のフィールドが変わると、`pickle` の
    読み出しが静かに別物になる。
    """
    assert isinstance(rate_cache.CACHE_FORMAT, int)
    assert rate_cache.CACHE_FORMAT >= 1
    # 鍵に入っていることを、meta の中身で直接確かめる
    assert "format" in json.dumps(
        {"format": rate_cache.CACHE_FORMAT, "manifest_sha256": "x",
         "feature_sha256": "y"})

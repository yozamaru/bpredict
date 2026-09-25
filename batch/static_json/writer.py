"""静的JSON の書き出しと、窓から出たファイルの削除（詳細設計 3.7）。

**毎回、窓の全ファイルを書き直す。** 差分更新にすると、間が抜けたときに古い値が
残る。**窓から出たファイルは削除する** — ファイル数と書き換え量を一定に保つため
であり、削除もコミットに含める（リポジトリと配信内容を一致させる）。

**推論に失敗したら1ファイルも書かない**（基本設計 4.3）。前回のものを残し、画面は
`meta.generatedAt` で遅延を出す。呼び出し側がこの関数を呼ばないことで実現する。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from batch.static_json.builder import (
    GameDetailInput,
    GameListInput,
    MetaInput,
    build_game_detail,
    build_game_list,
    build_meta,
)

# 静的JSON の置き場所（詳細設計 3.7）。Pages の静的アセットとして配信される
DATA_DIR = Path("web/public/data")

# 一覧の窓。当日 ＋ 翌日以降7日（詳細設計 3.7）
SCHEDULE_WINDOW_DAYS = 7

# **ファイル数の上限。** 窓から出たファイルの削除漏れを機械で捕まえる（詳細設計 3.7）。
# 内訳は meta 1 ＋ today 1 ＋ 7日 ＋ 当日の試合13 = 約23 で、上限はその倍。
MAX_DATA_FILES = 50


@dataclass
class WriteResult:
    """書いたもの・消したものを呼び出し側が出力に出せる形で返す。"""

    written: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return f"静的JSON: 書き出し={len(self.written)} 削除={len(self.removed)}"


def _dump(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # **`ensure_ascii=False`**（クラブ名が `\u...` に膨らむと差分が読めない）。
    # 末尾に改行を入れるのは、git の差分が「\ No newline at end of file」に
    # ならないようにするため
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _read_previous_meta(root: Path) -> dict[str, Any] | None:
    """前回の `meta.json` を読む。**D1 も公開APIも読まない**（詳細設計 3.7）。

    静的配信が Worker の可用性に依存し始めると、要件 4.2 が消す前提そのものが
    崩れる。書き出し先は作業ツリーにあるため、そこから引き継ぐ。
    """
    path = root / "meta.json"
    if not path.exists():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # **壊れていたら無いものとして扱う。** 例外の本文はログに出さない
        # （絶対ルール4）。初回と同じ扱いになり、遅延の判定材料が無くなるだけ
        return None
    data = loaded.get("data") if isinstance(loaded, dict) else None
    return data if isinstance(data, dict) else None


def carry_forward_last_success(
    root: Path, generated_at: str, last_run_status: str
) -> str | None:
    """`lastSuccessAt` を決める（詳細設計 3.7）。

    `SUCCESS` ならこの回の時刻、それ以外は前回の値を引き継ぐ。`meta.json` が
    無ければ None（クライアントは遅延と判定しない。判定材料がない）。
    """
    if last_run_status == "SUCCESS":
        return generated_at
    previous = _read_previous_meta(root)
    if previous is None:
        return None
    value = previous.get("lastSuccessAt")
    return value if isinstance(value, str) else None


def write_static_json(
    *,
    today: GameListInput,
    upcoming: list[GameListInput],
    details: list[GameDetailInput],
    generated_at: str,
    data_as_of: str | None,
    last_run_status: str,
    model_versions: list[str],
    root: Path = DATA_DIR,
) -> WriteResult:
    """窓の全ファイルを書き直し、窓から出たファイルを削除する。

    `upcoming` は翌日以降の一覧（最大 `SCHEDULE_WINDOW_DAYS` 日）、`details` は
    **当日の試合だけ**の詳細である（7日窓にすると年460MB を履歴に積む）。
    """
    if len(upcoming) > SCHEDULE_WINDOW_DAYS:
        raise ValueError(
            f"一覧の窓は {SCHEDULE_WINDOW_DAYS} 日まで（受け取った日数: {len(upcoming)}）"
        )

    result = WriteResult()
    root.mkdir(parents=True, exist_ok=True)

    _dump(root / "today.json", build_game_list(today, generated_at))
    result.written.append("today.json")

    keep_schedule = set()
    for day in upcoming:
        name = f"schedule/{day.game_date}.json"
        _dump(root / name, build_game_list(day, generated_at))
        result.written.append(name)
        keep_schedule.add(f"{day.game_date}.json")

    keep_games = set()
    for detail in details:
        name = f"games/{detail.game.game_id}.json"
        _dump(root / name, build_game_detail(detail, generated_at))
        result.written.append(name)
        keep_games.add(f"{detail.game.game_id}.json")

    _dump(
        root / "meta.json",
        build_meta(
            MetaInput(
                generated_at=generated_at,
                data_as_of=data_as_of,
                last_run_status=last_run_status,
                last_success_at=carry_forward_last_success(
                    root, generated_at, last_run_status
                ),
                model_versions=model_versions,
            )
        ),
    )
    result.written.append("meta.json")

    # **窓から出たファイルを削除する。** 残すとファイル数が単調に増え、古い予測が
    # 配信され続ける
    for directory, keep in (("schedule", keep_schedule), ("games", keep_games)):
        target = root / directory
        if not target.is_dir():
            continue
        for path in sorted(target.glob("*.json")):
            if path.name not in keep:
                path.unlink()
                result.removed.append(f"{directory}/{path.name}")

    return result


def count_data_files(root: Path = DATA_DIR) -> int:
    """`data/` のファイル数。CI の検査に使う（詳細設計 3.7）。"""
    if not root.is_dir():
        return 0
    return sum(1 for path in root.rglob("*") if path.is_file())

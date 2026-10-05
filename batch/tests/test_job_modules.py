"""ジョブのモジュールの並び（詳細設計 4.5.1）。

| テスト | どの規約か |
|---|---|
| `test_the_main_guard_is_the_last_statement` | **`-m` で走らせたときに未定義の関数を呼ばない** |

**これは実際に起きた。** `batch/jobs/train.py` の `if __name__ == "__main__":` が
ファイルの途中にあり、**その後ろに `evaluate_rate_models` が定義されていた**
（2026-10-05）。`python -m batch.jobs.train --rates` は上から順に実行するため、
**`main()` が走る時点でその関数が存在せず `NameError` になる**。

**テストからは見えない誤りである。** テストはモジュールを import してから
`main()` を呼ぶため、全定義が揃った状態で走る。**`-m` の経路だけが壊れる。**

**ログからも分からない。** 想定外の例外は型名だけを出すため（絶対ルール4 /
基本設計 4.3）、`NameError` という3文字しか残らない。原因の特定に
一時的な改変と再実行が要った。**同じ誤りを二度しないために、並びを固定する。**
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

JOBS = sorted(Path("batch/jobs").glob("*.py"))


@pytest.mark.parametrize("path", JOBS, ids=lambda p: p.name)
def test_the_main_guard_is_the_last_statement(path: Path) -> None:
    """`if __name__ == "__main__":` の後ろに定義を置かないこと。"""
    body = ast.parse(path.read_text(encoding="utf-8")).body
    guards = [
        index for index, node in enumerate(body)
        if isinstance(node, ast.If) and "__name__" in ast.dump(node.test)
    ]
    if not guards:
        pytest.skip(f"{path.name} に __main__ ガードがない")
    assert len(guards) == 1, f"{path.name} にガードが2つ以上ある"
    after = body[guards[0] + 1:]
    names = [getattr(node, "name", type(node).__name__) for node in after]
    assert not after, f"{path.name}: ガードの後ろに {names}"

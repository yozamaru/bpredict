"""特徴量生成の例外。

**`base.py` と `prepared.py` の両方が使うため、別の場所に置く。** 同じ名前を
2つ定義すると、`except` の片方が捕まえられなくなる。
"""

from __future__ import annotations


class FeatureError(RuntimeError):
    """対象試合が見つからない、または必要な列が欠けている。"""

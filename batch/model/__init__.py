"""学習と評価（工程8以降）。入力は `batch/snapshot/*.parquet` だけである。

**D1 を入力として読まない**（CLAUDE.md 絶対ルール3）。モデルの登録・読み出しは
Workers の `/internal/models*` 経由で行う。
"""

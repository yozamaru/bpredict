"""特徴量で使う定数。**すべて文書の記述と対応させる。**

「実装しながら決めた値」を置かない。文書に根拠がない値が必要になったら、
実装を止めて確認する（CLAUDE.md「勝手な仕様補完をしない」）。
"""

# 直近N試合の窓（詳細設計 2.2 の `winrate_l5_diff` / `winrate_l10_diff`）
RECENT_WINDOWS = (5, 10)

# 当季集計の縮約（詳細設計 2.6）。winrate = (w + k * prior) / (n + k)
SHRINK_K = 10

# シーズン間回帰の初期値（詳細設計 2.5）。工程8 のグリッド探索で確定する。
# 前季勝率を平均（0.5）方向へ回帰させた値を縮約の prior に使う。
SEASON_REGRESSION_INITIAL = 0.65

# 休養日数の上限（詳細設計 2.2 の `rest_days_diff`。シーズン跨ぎをクリップする）
REST_DAYS_CLIP = 14

# 「直近出場時間上位N名」の N（詳細設計 2.2 の `top_players_out_diff` が「上位5名」と定める）
TOP_PLAYERS = 5

# 欠場者の「直近平均出場時間」に使う窓。詳細設計 2.2 は窓を明示していないため、
# 同文書 2.3 が定義する `minutes_l5_player`（直近5試合）に合わせる。
MINUTES_LOST_WINDOW = 5

# Elo の既定値（詳細設計 2.2 の欠損時の値）
ELO_DEFAULT = 1500.0

# フリースロー試投の係数（詳細設計 1.3 のポゼッション式と同じ値）。
# **NBA 由来であり、要件 P0-7 で日本のデータから再推定することになっている。**
# 同じ値が `batch/parser/validators.py`（ポゼッション）と
# `batch/static_json/builder.py`（TS%）にもある。**層をまたいで共有しない** —
# parser が features の定数に依存すると、責務の分離（CLAUDE.md）が崩れる。
# P0-7 で値が変わるときは3箇所すべてを直す。
FTA_COEFFICIENT = 0.44

"""表と列の説明。`scripts/describe_schema.py` が読む。

**なぜ DDL のコメントではなくここに書くのか。** `db/migrations/*.sql` は
**適用済みのファイルを編集しない**（CLAUDE.md「マイグレーションは追記のみ」）。
説明を足すためにコメントを書き換えることはできない。

**出典は設計文書であり、ここで仕様を決めない。** 各節は下記のとおり。
**設計が言っていない列は空のままにする**（推測で埋めない。CLAUDE.md
「勝手な仕様補完をしない」）。

| 内容 | 出典 |
|---|---|
| 表の役割と分類 | `docs/design-basic.md` 3.1〜3.2、`docs/design-detail.md` 1章 |
| ボックススコアの項目と取得元のフィールド名 | `docs/design-detail.md` 4.4 の対応表 |
| 予測・整合化の列 | `docs/design-detail.md` 1.5、`docs/requirements.md` 6.8 |
| モデルと評価の列 | `docs/design-detail.md` 1.6、4.6 |

**DDL にコメントがある列は、そちらを優先する。** ここに重ねて書かない。
"""

from __future__ import annotations

#: 表の役割。1行で、分類と行数は生成側が出すのでここには書かない
TABLES: dict[str, str] = {
    # マスタ（恒久）。**時間で変わるものを単一行の属性として持たない**（基本設計 3.2）
    "clubs": "恒久的なクラブ。改称・リーグ移動があっても不変。表示名は `club_seasons` が持つ",
    "players": "恒久的な選手の人物マスタ。所属は持たない（`player_seasons` と実績側が持つ）",
    "venues": "恒久的な会場。`id` は公式サイトの `StadiumCD`。`name` は初出の名称で固定する",
    "seasons": "シーズン。`id` にリーグを含める（同一シーズンの PREMIER と ONE を同時に持てるようにする）",
    # マスタ（年度断面・履歴・名寄せ）
    "club_seasons": "シーズンごとのクラブ断面。名称・リーグ・本拠は年度で変わる。**backfill が試合データから作る**",
    "club_source_ids": "公式サイトのチームIDを `club_id` に解決する対応表。旧B1と新リーグをまたいで名寄せする",
    "player_seasons": "選手の所属断面。シーズン途中の移籍にも対応する",
    "venue_revisions": "会場の改称と収容人数の履歴。過去試合は当時の値で表示する。**全期間を再計算して洗い替える派生**",
    "venue_source_keys": "公式の会場ID（`StadiumCD`）を `venue_id` に解決する対応表",
    # ファクト
    "games": "試合。主キーは公式試合ID（自然キーにしない — 延期で日付が変わると別レコードになる）",
    "team_games": "チーム視点の試合行。`games` への OR 条件つき JOIN を消すためにある。日程系の特徴量はここだけを読む",
    "team_game_stats": "チームのボックススコア。選手側と同じ粒度で持つ（整合化の基準になる）",
    "player_game_stats": "選手別のボックススコア。`fgm` / `fga` / `reb` は持たず導出する（冗長列は不整合の余地になる）",
    "game_entries": "試合ごとの出場登録。**取得のたびに全行を洗い替える**（推定行が残ると「暫定」が解除されない）",
    # 派生
    "team_ratings": "試合日ごとの Elo ほかのスナップショット。**1行はその試合日の終了時点の値である**",
    # 予測
    "predictions": "試合単位の予測。**追記のみ**で、再推論は旧行を `is_active = 0` にして新しい行を足す",
    "player_predictions": "選手単位の予測。**チーム予測へ整合化した後の値**を入れる。成功数と得点は導出するため列を持たない",
    "prediction_reasons": "判断根拠。個別特徴ではなく**要因グループ**に集約した SHAP 値を持つ",
    "prediction_factors": (
        "**この予測に使った項目**（詳細設計 2.7.2）。`prediction_reasons` が"
        "「なぜそうなったか」を要因グループに集約して述べるのに対し、こちらは"
        "「**何を見たか**」を列ごとに並べる。**有利不利を主張しない**ため"
        "打ち消しが起きず、21列すべてを出せる"
    ),
    "prediction_team_targets": "整合化の目標値。**試投数と成功率の組**で持ち、`成功数 ≤ 試投数` を構造的に保証する",
    "prediction_model_bundle": "その予測に使ったモデル一式。1本の予測は最大33本のモデルの合成である",
    # 評価
    "model_versions": "学習済みモデル。artifact をテキストで格納する（1.5MB 上限）。有効なものは種別ごとに常に1本",
    "prediction_results": "確定予測と実績の照合結果。**中止・延期は `VOID` として的中率の母数から外す**",
    # 集計（実績の閲覧用。詳細設計 1.9）。**洗い替えず upsert だけで更新する**
    "player_stat_summary": "選手の実績の集計。シーズン別（**クラブ別**）と通算。保存するのは合計で、1試合平均は API が導出する",
    "team_stat_summary": "クラブの実績の集計。**母数を2つ持つ**（`games` は勝敗・得点、`stat_games` はボックススコア）",
    "accuracy_summary": "的中率の集計層。日次で洗い替える（公開APIが3表の全件走査をしないため）",
    # 運用
    "ingestion_logs": "ジョブの実行履歴。**例外オブジェクトをそのまま入れない**（型名と自前の短いメッセージに限る）",
}

#: 表をまたいで同じ意味を持つ列。FK は生成側が自動で説明するため書かない
SHARED: dict[str, str] = {
    # 時刻（基本設計 3.2）
    "created_at": "行を作った時刻",
    "updated_at": "**値が変わった時刻**（`fetched_at` は取得時刻であって更新時刻ではない）",
    "fetched_at": "取得した時刻",
    "game_date": "その試合の **JST における暦日**（`tipoff_at` を JST へ変換して求める）",
    "finished_at": "試合終了時刻。**リーク判定の絞り込みはこの列で行う**（`tipoff_at` ではない）",
    "valid_from": "この行が有効になる日",
    "valid_to": "この行が有効な最後の日（終端は `9999-12-31`）",
    # 区分
    "league": "リーグ区分",
    "competition": "大会区分。取り込むのはこの2区分だけ（オールスター・入替戦・プレシーズンは入れない）",
    "status": "試合の状態",
    "is_home": "ホーム側か",
    # ボックススコア（詳細設計 4.4 の対応表）
    "minutes": "出場時間（分）。取得元は `MM:SS` 形式",
    "started": "スターターか",
    "pts": "得点。恒等式 `2FGM×2 + 3FGM×3 + FTM` の検証に使う",
    "fg2m": "2点シュート成功数",
    "fg2a": "2点シュート試投数",
    "fg3m": "3点シュート成功数",
    "fg3a": "3点シュート試投数",
    "ftm": "フリースロー成功数",
    "fta": "フリースロー試投数",
    "oreb": "オフェンスリバウンド",
    "dreb": "ディフェンスリバウンド",
    "ast": "アシスト",
    "tov": "ターンオーバー",
    "stl": "スティール",
    "blk": "ブロック",
    "pf": "自分が犯したファウル数",
    "fd": "被ファウル数（FIBA 系の `FD`。NBA の「テイクチャージ」に相当する）",
    "possessions": "ポゼッション（攻撃回数の推定値）。`fga - oreb + tov + 0.44 × fta`",
    "fg2_pct": "2点シュートの成功率",
    "fg3_pct": "3点シュートの成功率",
    "ft_pct": "フリースローの成功率",
    # 予測の世代（詳細設計 1.5）
    "revision": "同一試合・同一モデルの推論回数（世代通番）",
    "predicted_at": "推論した時刻",
    "is_provisional": "出場者未確定のまま算出した予測か（画面の「暫定」）",
    "is_final": "試合開始をもって凍結された予測か。**1 の行は更新・削除できない**",
    "is_active": "いま有効な世代か。1試合につき常に1本",
    # モデルと評価（詳細設計 1.6）
    "model_type": "モデルの種別",
    "model_version": "使ったモデルのバージョン",
    "target": "`*_RATE` のときの対象項目（14種）",
    "brier": "Brier Score。**主要な改善指標**（0に近いほど良い）",
    "n": "母数（試合数）",
}

#: 表ごとに意味が違う列、またはその表だけにある列
SPECIFIC: dict[tuple[str, str], str] = {
    # 集計（詳細設計 1.9）
    ("player_stat_summary", "scope"): "`SEASON`（季の合計）か `CAREER`（通算）",
    ("player_stat_summary", "club_id"): "季の行が指すクラブ。**`CAREER` では空文字**（NULL にしない）",
    ("player_stat_summary", "games"): "出場した試合数。**1試合平均の母数である**（要件 8.3）",
    ("player_stat_summary", "games_started"): "先発した試合数",
    ("team_stat_summary", "scope"): "`SEASON`（季の合計）か `CAREER`（通算）",
    ("team_stat_summary", "games"): "勝敗と得点の母数（`team_games` で結果が入っている試合数）",
    ("team_stat_summary", "points_for"): "総得点（`games` のスコアから取る）",
    ("team_stat_summary", "stat_games"): "ボックススコアの母数。**`games` と別に持つ**（スタッツが欠ける試合が実在する）",
    # マスタ
    ("clubs", "id"): "公式サイトのチームID。**シーズン・改称・リーグ再編をまたいで不変**",
    ("clubs", "name"): "現在の表示名。過去試合の表示には `club_seasons.name` を使う",
    ("clubs", "slug"): "`/teams/[slug]` の識別子。**手で決め、改称でも変えない**",
    ("players", "id"): "公式サイトの選手ID",
    ("players", "name"): "氏名",
    ("players", "height_cm"): "身長（cm）",
    ("venues", "id"): "公式サイトの会場ID（`StadiumCD`）。**文字列に正規化して持つ**",
    ("venues", "name"): "初出の名称で固定する。当時の名称は `venue_revisions` が持つ",
    ("venues", "prefecture"): "都道府県。住所から導く（国土地理院の候補で解決する）",
    ("venues", "lat"): "緯度。**1回だけ解決して CSV に固定**し、実行時に外部サービスへ依存しない",
    ("venues", "lng"): "経度。同上",
    ("seasons", "id"): "`'2026-27-PREMIER'` の形。**リーグを含める**",
    ("seasons", "label"): "画面に出す表記（`'2026-27'`）",
    ("seasons", "start_date"): "当季の **9月1日**。取り込む試合日の上位集合であればよく、**狭いと実在する試合日が404になる**",
    ("seasons", "end_date"): "翌年の **6月30日**。同上",
    ("club_seasons", "name"): "その年度の正式名称。出典はボックススコアの `TeamNameJ`（当時の名称が入る）",
    ("club_seasons", "short_name"): "短縮表記。出典は試合一覧のクラブ選択肢",
    ("club_seasons", "color_primary"): "クラブカラー。**公式のロゴ・エンブレムは使わない**",
    ("club_seasons", "color_secondary"): "同上",
    ("club_source_ids", "source_id"): "公式サイトのチームID",
    ("club_source_ids", "note"): "名寄せの根拠を残す欄",
    ("player_seasons", "number"): "背番号",
    ("player_seasons", "position"): "ポジション",
    ("player_seasons", "roster_type"): "登録区分",
    ("player_seasons", "joined_on"): "加入日",
    ("player_seasons", "left_on"): "退団日",
    ("venue_revisions", "name"): "当時の名称。`games.venue_name_at_game` から全期間を再計算する",
    ("venue_revisions", "capacity"): "**B.LEAGUE 開催時の観客席数**。公式サイトに無いため手入力（行ごとに出典URL）",
    ("venue_source_keys", "source_code"): "公式の会場ID",
    # game_entries
    ("game_entries", "source"): "公式確定か推定か。**`ESTIMATED` が1件でも残る試合は「暫定」**",
    ("game_entries", "confidence"): "推定の確度（公式確定なら使わない）",
    # games
    ("games", "id"): "公式サイトの試合ID",
    ("games", "finished_at_is_estimated"): "1 なら `finished_at` が実測ではなく `tipoff_at + 2時間` の推定値",
    ("games", "tipoff_at"): "試合開始時刻（UTC）。特徴量の `as_of` はこの値である",
    ("games", "home_club_id"): "ホームのクラブ",
    ("games", "away_club_id"): "アウェイのクラブ",
    ("games", "is_primary_venue"): "メイン会場か（代替会場ではホームアドバンテージが下がる）",
    ("games", "series_game_no"): "同一カード連戦の何戦目か。**Bリーグは土日2連戦が基本編成である**",
    ("games", "home_score"): "ホームの得点（終了後に入る）",
    ("games", "away_score"): "アウェイの得点（終了後に入る）",
    ("games", "attendance"): "入場者数",
    ("games", "source_url"): "取得元のページ。**公開APIのレスポンスには含めない**",
    # team_games
    ("team_games", "opponent_id"): "対戦相手のクラブ",
    ("team_games", "margin"): "得失点差（自チーム − 相手）",
    # player_game_stats
    ("player_game_stats", "plus_minus"): "＋/−。**実績としてのみ持ち、予測しない**（1試合の分散が大きく、情報も増えない）",
    # team_ratings
    ("team_ratings", "as_of_date"): "その試合日。**値はこの日の終了時点**であり、参照は `as_of_date < 対象試合日` で行う",
    ("team_ratings", "elo"): "Elo レーティング。**単一特徴として最も強い**",
    ("team_ratings", "off_rating"): "オフェンスレーティング。**集計窓が未決のため現在は NULL**",
    ("team_ratings", "def_rating"): "ディフェンスレーティング。同上",
    ("team_ratings", "pace"): "ペース。同上",
    ("team_ratings", "games_played"): "その時点の消化試合数。10未満は Elo の信頼度が低い",
    # predictions
    ("predictions", "id"): "予測の識別子",
    ("predictions", "run_id"): "この予測を作ったジョブ（`ingestion_logs.id`）",
    ("predictions", "as_of"): "特徴量が参照してよい情報の上限時刻（= その試合の `tipoff_at`）",
    ("predictions", "data_as_of"): "推論時点で DB にあった最新試合の終了時刻。**検証と本番の鮮度差を検出する**",
    ("predictions", "home_win_prob"): "ホームの勝率。アウェイは `1 - この値`（冗長列を持たない）",
    ("predictions", "pred_margin"): "予想得点差（ホーム − アウェイ）",
    ("predictions", "pred_total"): "予想合計得点",
    ("predictions", "pred_home_score"): "予想得点。`(total + margin) / 2` で導出する（独立に回帰しない）",
    ("predictions", "pred_away_score"): "同 `(total - margin) / 2`",
    ("predictions", "feature_snapshot"): "生成した特徴量ベクトル（JSON）。後から再構築と突き合わせる",
    # player_predictions
    ("player_predictions", "id"): "予測の識別子",
    ("player_predictions", "avail_prob"): "出場確率。**0.5 未満の選手は画面に出さない**",
    ("player_predictions", "pred_minutes"): "整合化後の期待出場時間。**1チームの合計が200分になる**",
    ("player_predictions", "err_minutes"): "誤差の目安（当該選手の直近N試合の絶対誤差の中央値）。主要4項目のみ持つ",
    ("player_predictions", "err_pts"): "同 得点",
    ("player_predictions", "err_reb"): "同 リバウンド",
    ("player_predictions", "err_ast"): "同 アシスト",
    # prediction_reasons
    ("prediction_reasons", "rank"): "表示順",
    ("prediction_reasons", "label_ja"): "画面に出すラベル。**選手個人の能力・資質への評価を含む表現を使わない**",
    ("prediction_reasons", "value_text"): "画面に出す値の文言",
    ("prediction_reasons", "favors"): "有利な側",
    # prediction_factors
    ("prediction_factors", "rank"): (
        "表示順。**寄与の大きさではない**（要因グループの順 → 列の順）"
    ),
    ("prediction_factors", "group_key"): "要因グループ。画面には言い換えを出す",
    ("prediction_factors", "label_ja"): (
        "画面に出すラベル。**生の特徴量名を出さない**（要件 6.9）"
    ),
    ("prediction_factors", "value_text"): "画面に出す値の文言。**符号を付けない**",
    ("prediction_factors", "larger"): (
        "**値が大きい側。「有利な側」ではない** — 係数が負の列（`drtg_diff`）では"
        "両者が逆を向く。向きを持たない列（6本）と差が 0 の列は NULL"
    ),
    # model_versions
    ("model_versions", "algo"): "アルゴリズム",
    ("model_versions", "trained_at"): "学習した時刻",
    ("model_versions", "train_rows"): "学習に使った行数",
    ("model_versions", "train_range"): "学習データの範囲",
    ("model_versions", "cv_accuracy"): "walk-forward の Accuracy。**表示専用で、採用判定には使わない**",
    ("model_versions", "cv_brier"): "walk-forward の Brier。**ただし採用判定では同一ウィンドウで再評価した値を使う**",
    ("model_versions", "cv_logloss"): "walk-forward の Log Loss（学習時の目的関数）",
    ("model_versions", "baseline_home_accuracy"): "「ホームが必ず勝つ」の Accuracy（実測 52.7%）",
    ("model_versions", "artifact_sha256"): "artifact のハッシュ。取得後に照合する",
    ("model_versions", "notes"): "備考",
    # prediction_results
    ("prediction_results", "outcome"): "照合の結果",
    ("prediction_results", "predicted_home_win"): "ホーム勝利と予想したか",
    ("prediction_results", "is_correct"): "的中したか",
    ("prediction_results", "score_mae"): "予想スコアの絶対誤差",
    ("prediction_results", "was_provisional"): "暫定の段階で出した予測だったか（暫定/確定の内訳に使う）",
    ("prediction_results", "evaluated_at"): "照合した時刻",
    # accuracy_summary
    ("accuracy_summary", "scope"): "集計の単位",
    ("accuracy_summary", "scope_key"): "その単位の中の鍵（シーズンID・確率帯など）",
    ("accuracy_summary", "accuracy"):
        "的中率。**画面では必ず母数を併記する**。ただし `BUCKET` 行だけは「予想した確率の平均」である（較正曲線の横軸。的中率は `hit_rate`）",
    ("accuracy_summary", "baseline_accuracy"): "比較対象（ホーム必勝）の的中率",
    ("accuracy_summary", "score_mae"):
        "予想スコアの誤差。**1チームあたりの平均絶対誤差**で、得点差の MAE とは別物である。母数は `n` と同じ（食い違う場合は NULL）",
    ("accuracy_summary", "hit_rate"):
        "その確率帯の的中率。**`BUCKET` 行だけが持つ**（他のスコープは `accuracy` がそのまま的中率）。**`actual_rate`（ホーム勝率）と別の量で、50%未満の帯では符号が逆になる**",
    # ingestion_logs
    ("ingestion_logs", "id"): "実行の識別子",
    ("ingestion_logs", "job"): "ジョブ名",
    ("ingestion_logs", "started_at"): "開始時刻",
    ("ingestion_logs", "rows_affected"): "書き込んだ行数（無料枠の監視に使う）",
}

#: 取得元のフィールド名（詳細設計 4.4 の対応表）。ボックススコアの列に付ける
SOURCE_FIELD: dict[str, str] = {
    "minutes": "PlayTime",
    "started": "StartingFlg",
    "fg2m": "PT2M",
    "fg2a": "PT2A",
    "fg3m": "PT3M",
    "fg3a": "PT3A",
    "ftm": "FTM",
    "fta": "FTA",
    "oreb": "RB_OFF",
    "dreb": "RB_DEF",
    "ast": "AS",
    "tov": "TO",
    "stl": "ST",
    "blk": "BS",
    "pf": "FOUL",
    "fd": "FOULON",
    "plus_minus": "PLUSMINUS",
    "pts": "Point",
}

#: 取得元フィールドを付ける表。他の表の同名列には付けない
SOURCE_FIELD_TABLES = ("player_game_stats", "team_game_stats")


def describe_table(table: str) -> str:
    return TABLES.get(table, "")


#: 接頭辞 → 説明の前置き。**接頭辞を外して共通の説明を引く**（同じ説明を書き写さない）
PREFIXES: tuple[tuple[str, str], ...] = (
    ("pred_", "予測値"),
    ("tgt_", "チーム目標"),
    ("err_", "誤差の目安"),
)


def describe_column(table: str, column: str) -> str:
    """表固有 → 共通 → 接頭辞からの導出 の順に引く。

    **どれにも当たらなければ空**（推測で埋めない。CLAUDE.md）。
    """
    found = SPECIFIC.get((table, column)) or SHARED.get(column, "")
    if found:
        return found
    for prefix, label in PREFIXES:
        if column.startswith(prefix):
            base = SHARED.get(column[len(prefix):], "")
            if base:
                return f"{label}: {base}"
    return ""


def source_field(table: str, column: str) -> str:
    if table not in SOURCE_FIELD_TABLES:
        return ""
    return SOURCE_FIELD.get(column, "")

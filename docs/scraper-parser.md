# スクレイパ・パーサ（工程5）

調査の根拠は [取得元一覧](scraping-sources.md)。HTTPの制約と解析規則の正本は
[詳細設計4.3〜4.4](design-detail.md)。本資料は実装の利用方法を記録する。

## 範囲

- `batch/scraper/`：HTTP、取得前確認、取得URLの構築。レスポンスの試合データは解釈しない。
- `batch/parser/`：日程と終了済みボックススコアの解析、正規化、検証。ネットワークやDBを使わない。
- `batch/jobs/parser_canary.py`：取得前確認後、指定された終了済み試合詳細1件を取得・解析する。
- DB投入・過去全件取得は工程6。公式エントリー、選手属性のDB正規化、会場履歴は引き続き未決。

## 取得前確認と運用

`SCRAPER_USER_AGENT` は識別名と実在する連絡先URLを含める。
`SCRAPER_ROBOTS_SHA256` / `SCRAPER_TERMS_SHA256` は、内容を確認した運営者が設定する。
未設定・不一致では試合データを取得しない。秘密情報ではないためGitHubのVariablesで設定できる。
通常のCLI実行では基準値未設定・不正なら通信せず終了コード1を返す。

```bash
# 規約の基準候補を取得。ハッシュだけ表示し、本データは取得しない
SCRAPER_USER_AGENT='BPredict/1.0 (+https://github.com/yozamaru/bpredict/issues)' \
  python -m batch.jobs.parser_canary --inspect-policy

# 上記の内容確認と基準値の設定後に実行
python -m batch.jobs.parser_canary --game-id 505497 --event 2
```

確認コマンドはJSONの `robots_sha256` / `terms_sha256` で候補を表示するだけで、
基準値を保存・承認しない。実際の [robots.txt](https://www.bleague.jp/robots.txt) と
[利用規約](https://www.bleague.jp/site/) を確認してから、対応する環境変数へ設定する。
通常実行の既定は試合 `505497`・大会 `2`。必須項目や値域の検証も行い、
失敗は終了コード1、成功は件数のみを表示する。本文・URL・例外の詳細をログに出さない。

デフォルトの状態ファイルは `batch/.scraper-state/state.json`。
`SCRAPER_STATE_PATH` で変更できる。同じホスト上のジョブは必ず同じファイルを使う。
UTC日単位の件数、最後のリクエスト時刻、429/503による当日の停止状態を保存し、
ファイルロックで並列取得を防ぐ。**ロックは60秒で諦めて失敗する**（保持側が停止した
ときに後続を無言で止めないため。詳細設計4.3）。これは取得本文やD1のDBではない。
新しい空ファイルを実行ごとに作ると通算上限が失われる。CIの状態共有はワークフローを参照。

### 日次カナリア

`.github/workflows/parser-canary.yml` は毎日07:00 JSTに、既定ブランチで実行する。
手動の `workflow_dispatch` も既定ブランチに限る。上記3つのVariablesのいずれかが
未設定なら**警告を残してスキップ**し、設定された後だけ実サイトの取得を有効にする
（設定し忘れている間はサイト構造の変更を検知できないため、notice では気づけない）。
不正なハッシュ・規約の変更・パース失敗はActionsの失敗になる。

状態は [GitHub公式のcache restore/save](https://github.com/actions/cache) で共有する。
同じUTC日の最新キャッシュを復元し、実行ID・試行番号ごとの別キーへ、失敗時も保存する。
`concurrency: canary` で復元から保存までを直列化する。キャッシュ対象は状態JSONだけで、
取得本文は含めない。PR実行はこのワークフローを起動しない。

初回のキャッシュミスは空の状態から開始する。キャッシュは永続台帳ではなく、期限・
容量制限による削除や保存失敗では過去の使用数を復元できない。また、ローカル実行との
日次上限は自動で共有しない。再実行時は状態の復元・保存を確認し、同日の状態が失われた
場合は当日の再取得を止める。工程6で別の取得ジョブを追加する前に、全取得ジョブの
排他制御と、ホストをまたぐ状態の永続化・共有方法を統一する。

## 解析の契約

`parse_club_options(html)` は年度別の日程HTMLのクラブ選択肢から短縮名→公式IDを返す。
`parse_schedule(body, year=..., event=..., clubs=..., previous_date=...)` は日程JSONを読み、
`SchedulePage` を返す。`previous_date` はページ先頭に日付見出しがないときだけ使う。
空の `topics` と `index=null` が終端。非空なのに試合が取れない応答は正常な空ページにしない。
スコア・時刻が未公表ならNoneを返す。時刻未公表の行を `tipoff_at NOT NULL` のDBへそのまま投入しない。
試合中・未認識の表示はParseErrorとして扱い、未開始や終了へ推測変換しない。

`parse_boxscore(body, event=..., clubs=..., expected_game_id=...)` は終了済み試合を読み、
`BoxScore` を返す。`clubs` は公式TeamID→内部club_idの対応表。名前でIDを推測しない。
返すのは許可した項目だけで、元JSONは返さない。公式合計行はCategory=3を1チーム1件要求する。
未開始のデータ欠落と、構造が壊れた終了試合は `DataUnavailable` / `ParseError` で区別する。
NULLの値を含む恒等式は評価不能として扱い、揃っている値だけ検証する。
終了時刻が欠ける終了試合は既存設計どおり開始+2時間、推定フラグを付ける。

エラーが続く処理は `ParseFailureTracker` を使い、成功で連続回数を戻す。
3件連続で `ParseErrorStreak`。ジョブはPARTIAL相当の非ゼロ終了にする。

## 確認コマンド

```bash
.venv/bin/ruff check batch
.venv/bin/mypy batch
.venv/bin/pytest batch/tests -q
python scripts/check_fixtures.py
```

テストは合成データと注入したHTTP応答・時計を使う。実サイトやD1を変更しない。

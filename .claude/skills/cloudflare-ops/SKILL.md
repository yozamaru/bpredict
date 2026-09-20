---
name: cloudflare-ops
description: Cloudflare Workers・D1・Pages の実装と運用、GitHub Actions のワークフロー、CI、シークレット管理、無料枠の監視を行うときに使う。api/ db/ .github/workflows/ を触る前に読むこと。
---

# インフラ実装・運用規約

キャッシュTTLとAPI仕様の実体は `docs/design-detail.md` 3章、インフラ設計は `docs/design-basic.md` 7章にある。

## コスト制約（最優先）

年間運用費は原則ゼロ、上限5,000円（Apple Developer 年会費を除く）。**上限のない従量課金構成は採用しない。**

| リソース | 無料枠 |
|---|---|
| Workers リクエスト | 10万per日 |
| **Workers CPU** | **10ms per リクエスト** |
| Workers サブリクエスト | 50 per リクエスト |
| D1 読取行 | 500万per日 |
| **D1 書込行** | **10万per日** |
| **D1 クエリ数** | **50 per Worker 呼び出し（Free）/ 1,000（Paid）** |
| **D1 バインドパラメータ** | **100 per 文** |
| **D1 1行のサイズ** | **2,000,000 バイト** |
| D1 ストレージ | 5GB |
| Pages 静的アセット | 無制限 |
| **Pages ファイル数** | **20,000 per デプロイ** |
| Pages ファイルサイズ | 25 MiB per ファイル |
| Pages ビルド | 500回per月 |
| **WAF レートリミット（Free）** | **1ルール / カウント10秒のみ / ブロック10秒のみ / IP のみ** |
| GitHub Actions | public のため無制限 |

**最初に当たる制約は「D1 の1呼び出し50クエリ」である。** ストレージでも CPU でもない。旧設計のバッチサイズ（一律500行）は9テーブル中6つでこの上限を超えていた。

**Workers CPU 10ms は制約ではない。** 実測で500行の JSON（141KB）はパース 0.56ms・検証 0.28ms・`batch()` 組立 0.50ms の合計 1.34ms。Zod で3〜4ms と見込まれるが予算内。**バッチサイズは CPU ではなくクエリ数で決める。**

読取行数については、**学習・特徴量生成がスナップショットを読むため D1 を消費しない**（後述）。

**Workers Paid（年額約9,000円）へ移行しない。** 有料サービス・有料APIが必要と判断した場合は、実装せずに相談する。

**独自ドメイン（年1,000〜1,500円）は例外として認める。** `*.pages.dev` はゾーンを持たないため WAF・レートリミット・キャッシュルールが一切使えず、無料枠を守る手段がなくなる。これは「性能のための課金」ではなく「無料枠を守るための投資」である。

## 配信アーキテクチャ

**主要導線（今日の予測・試合一覧）は Workers も D1 も経由させない。**

バッチが `web/public/data/*.json` を書き出し、Pages の静的アセットとして配信する。1日分30KB程度で、更新は1日3回しかない。これにより:

- Workers 枠と D1 読取をゼロ消費
- 枠が枯渇しても当日の予測が表示され続ける（真の縮退）

**ISR を使わない。** `next-on-pages` は ISR 非対応で `revalidate` 指定のルートはビルド時に固定され二度と更新されない（＝今日の予測がデプロイ時点で凍結する）。`@opennextjs/cloudflare` は対応するが R2/KV（従量課金）が必須で、コスト制約に抵触する。加えて ISR/SSR に落ちると **1ページ表示あたり Workers を2回消費**する（Pages Function → Workers API）。

Workers API は日付指定・チーム別・的中率といった動的クエリと、書き込みに限定する。

## Workers API

### 書き込みの関門としての責務

`/internal/*` は D1 への唯一の書き込み経路である。以下を必ず行う。

1. Bearer を**定数時間比較**する（両者を SHA-256 ハッシュ化してから比較。`timingSafeEqual` は長さ不一致で例外を投げる）
2. `Bearer ` プレフィックスを正規表現で厳格に検証する（`.replace("Bearer ", "")` はスキーム検証にならない）
3. Zod で型と**値域**を検証する
4. **`games.tipoff_at <= now` の試合への予測書き込みを 409 `ALREADY_FINAL` で拒否する**
5. 旧行の非活性化と新行の挿入を**単一 `batch()`** で実行する
6. UPDATE には必ず `WHERE is_final = 0` を付ける
7. トークンを用途で分離する（`INGEST_TOKEN` / `FINALIZE_TOKEN`）。2キー方式（`INGEST_TOKEN_NEXT`）で無停止回転できるようにする
8. 日次の書き込み回数上限を設ける（予測1日2000件）。漏洩時の被害を「全DB汚染」から「1日分」に抑える

**D1 REST API を直接叩く経路を作らない。** REST 直叩きを許すと上記すべてが迂回可能になり、予測の不変性を守る関門が存在しなくなる。`CF_API_TOKEN` はマイグレーション専用で、スコープは対象D1のEditのみに絞る。

### 入力検証

**すべてのパラメータを境界で検証し、不一致は D1 にもキャッシュにも触れず 400 で返す。**

- `date` は形式・実在日付・**シーズン範囲内**を検証する
- `slug` `gameId` も正規表現で制約する
- `limit` は既定20・最大100でクランプする

範囲外の日付を弾かないと URL 空間が無限になり、クローラの総当たりで無料枠が枯渇する。

**D1 クエリは必ず `env.DB.prepare(sql).bind(...)` を使う。** テンプレートリテラルや文字列連結でSQLを組まない。

### キャッシュ

エッジとブラウザで TTL を分離する（`s-maxage` と `max-age`）。

**`max-age` を長く取らない。** 一度 `max-age=86400` を返すとそのクライアントは24時間必ず古い値を見る。エッジをパージしても届かない。

**「パージする」に依存しない。** Workers の Cache API の `delete()` はそのコロにしか効かず、`*.pages.dev` ではゾーンパージも使えない。**キャッシュキーにデータ世代（最終取り込み時刻）を含める**ことで、新しい世代が出れば自動的に別キーになり、パージ自体が不要になる。

キャッシュキーは**許可リストにないクエリパラメータを除去して正規化する**。`?fbclid=` のようなパラメータで無限にフラグメント化すると、キャッシュを迂回して D1 に直撃する。

`stale-while-revalidate` を付け、TTL 失効時に並列リクエストが全部 D1 に落ちるのを防ぐ。

### レート制限

**CORS はレート制限の代替ではない。** ブラウザ内の制限であり、curl やスクリプトには一切効かない。しかもフロントは静的出力でサーバサイド fetch するため `Origin` すら付かない。

**無料プランの WAF レートリミットは制約が強い。** 1ルールのみ、カウント期間は10秒のみ、ブロック時間も10秒のみ、対象は IP のみ、照合できるフィールドは Path と Verified Bot のみ。**「60req/分を超えたら1分ブロック」は無料枠では設定できない。**

```
式:       (http.request.uri.path contains "/api/v1/") and (not cf.bot_management.verified_bot)
しきい値: 10秒あたり 30 リクエスト
動作:     ブロック 10秒
```

10秒ブロックは総量規制としては弱く、攻撃者は10秒待てば再開できる。これは「暴走したクライアント」と「素朴なクローラ」を止めるための措置である。真の防御は次の2つで、WAF はその補助。

1. **主要導線が静的配信で Workers を通らない**（枠が枯渇しても当日の予測は出る）
2. **URL 空間の有限化**（シーズン範囲外の日付は D1 到達前に404）

Workers 側にも Cache API の簡易カウンタを置き 429 + `Retry-After` を返す。Cache API はコロ単位で厳密な総量規制にはならないが、それを承知で階層の1つとして持つ。

10 req/s のクローラ1体で3.5時間で1日分の枠を使い切れる。リセットは 00:00 UTC（09:00 JST）であり、**日中に枯渇すると日本の夕方から夜が丸ごと使えなくなる**。

## D1

### マイグレーション

```bash
wrangler d1 migrations create bpredict <name>
wrangler d1 migrations apply bpredict --local
wrangler d1 migrations apply bpredict --remote
```

- **追記のみ。適用済みのファイルを編集しない。** 必ず新しい番号のファイルを追加する。適用済みを書き換えるとローカルと本番でスキーマが分岐し、`migrations apply` が沈黙して壊れる
- 本番適用は手動承認を経る
- 破壊的変更は避ける。追加で対応する。`DROP TABLE` / `DROP COLUMN` は事前に確認を取る
- **列挙値と値域には必ず CHECK 制約を付ける**（スクレイピングは入力が信用できないパイプライン）
- 単一性は部分ユニークインデックスで担保する（`is_active`、`is_final`、`model_versions.is_active`）
- テストのスキーマは `db/migrations/*.sql` をそのまま in-memory SQLite に適用する（二重管理しない）

### D1 固有の制約

| 制約 | 対応 |
|---|---|
| **1 Worker 呼び出しあたり50クエリ（Free）** | **テーブルごとに `max_rows_per_request = floor(100 / 列数) × 40` を算出する。一律500件にしない** |
| バインドパラメータは1ステートメント100個まで | 1文の行数は `floor(100 / 列数)` |
| **1行あたり2,000,000バイト** | `model_versions.artifact_text` に1.5MB上限チェックを置く |
| リクエストを跨ぐトランザクションがない | 原子性が必要な操作は単一 `batch()` に入れる |
| SQL文長 100KB | バッチサイズの上限と併せて考慮する |

**テーブルごとのバッチサイズ**

| テーブル | 列数 | 1文の行数 | 1リクエスト上限 |
|---|---|---|---|
| `player_predictions` | 26 | 3 | **120** |
| `player_game_stats` | 22 | 4 | **160** |
| `games` / `team_game_stats` | 20 | 5 | **200** |
| `predictions` | 18 | 5 | **200** |
| `prediction_team_targets` | 17 | 5 | **200** |
| `game_entries` / `team_ratings` | 6–7 | 14–16 | **560–640** |

列数から機械的に算出する関数を `api/src/config/batch-limits.ts` に置き、結果が50クエリ以内であることをテストで検証する。列を1つ増やしたときに静かに超えるのを防ぐ。

> **`batch()` 内の各文が50クエリ制限にどう計上されるかは公式に記載がない。** 「1文＝1クエリ」という最も厳しい前提で設計し、Phase 0（P0-13）で実測する。**実測前に緩めない。**

**単一 `batch()` に入れるべき操作**

1. 予測の非活性化＋新規挿入
2. `team_ratings` の期間 DELETE＋INSERT（別リクエストに分かれると、Eloが一時的に消えた状態を特徴量生成とAPIが読む）
3. `model_versions` の切替

### 予測データの扱い

```sql
-- 再推論: UPDATE ではなく追記
UPDATE predictions SET is_active = 0 WHERE game_id = ? AND is_active = 1;
INSERT INTO predictions (...) VALUES (...);   -- 同一 batch() で
```

**`is_final = 1` の行を UPDATE / DELETE しない。** トリガで禁止されている。アプリ層の規律だけに頼らない。

**凍結の範囲に例外を設けない。** `predictions` だけでなく `player_predictions` / `prediction_reasons` / `prediction_team_targets` / `prediction_model_bundle` にも同じトリガを置く。子テーブルだけ書き換えられるなら不変性の主張が成立しない。

### 読取行数の削減

**バッチは D1 を読まない。** 特徴量生成も学習も推論も、入力は `batch/snapshot/*.parquet` のみとする。「一括エクスポートして読む」経路も作らない。一度でも例外を許すと逐次クエリへの退行を設計で止められなくなる。

逐次クエリ方式では `games` の全件スキャンを試合ごとに繰り返すため1日150万〜1,450万行に達し、上限（500万行per日）を超える。一括エクスポート方式でも読取枠を消費し、学習が Cloudflare の可用性に依存する。**スナップショットを唯一の入力と決めれば、D1 の読取は公開APIの動的クエリ分だけになる。**

スナップショットは `daily_ingest` が D1 へ書くのと同じデータから書き出し、`MANIFEST.json` の行数と SHA256 で整合を検証する。破損・欠損時は学習を中止して現行モデルを継続使用する。

インデックスは `games` の `home_club_id` と `away_club_id` の**両方**に張る。片方だけだと `OR` 条件が全件スキャンになり、日程系の特徴量すべてがその経路を通る。

## GitHub Actions

### CI（必須）

**push / PR で必ずテストを走らせる。** 最重要の `test_leakage.py` を人間の規律に任せると、3ヶ月で形骸化する。

| 対象 | 内容 |
|---|---|
| Python | `ruff` / `mypy` / `pytest batch/tests` |
| api | `tsc --noEmit` / `eslint .` / vitest / `test_batch_size_within_query_limit` |
| web | `tsc --noEmit` / **`eslint .`** / `next build` / トークンのコントラスト検証 / **`out/` のファイル数が18,000以下** |
| リポジトリ | fixtures に実サイト由来文字列がないことの検査 / スナップショットと D1 ローカルの一致検査 |

**`next lint` を使わない。Next.js 16 で削除されている。** `next build` もリントを実行しない。ESLint は CLI を直接呼び（`eslint .`）、設定は Flat Config（`eslint.config.mjs`）とする。

**静的生成は直近5シーズンまで。** Pages の1デプロイ20,000ファイル上限に対し、全10シーズンでは 20,184 ファイル（101%）で超過する。直近5シーズンなら 10,184（51%）。1ルートあたりのファイル数は Next.js のバージョンで変わるため、CI の件数検査を省略しない。

`monthly-train` の先頭にもリークテストを置き、失敗したらモデル登録に到達させない。

**`parser-canary`** を日次で回す。実サイトの日程ページ1枚のみを取得してパースし、必須項目が取れなければ Actions を fail させる。fixture はスナップショットであるため構造変更を検知できず、これが唯一の早期検知手段になる。

### セキュリティ

| 項目 | 設定 |
|---|---|
| `permissions` | 既定は `contents: read`。**`daily-ingest` と `gameday-update` の2つのみ `contents: write`**（静的JSONとスナップショットをコミットするため） |
| `concurrency` | D1 へ書き込むジョブは `group: d1-write` / `cancel-in-progress: false`。cron 遅延で重なると `revision` の採番が競合する |
| リポジトリ設定 | Workflow permissions を「Read repository contents」既定に |
| サードパーティ Action | 増やさない。増やす場合はコミットSHAでピン留め |
| `pull_request_target` | **使用禁止** |
| Dependabot | pip / npm / github-actions を weekly で登録 |
| Secret scanning | Push protection を有効化 |
| 依存固定 | `requirements.txt` は `==` 固定、`package-lock.json` をコミットし `npm ci` |

`permissions` を書かないと `GITHUB_TOKEN` がリポジトリ既定値を引き継ぎ、依存パッケージの侵害でリポジトリへの push が可能になる。

**設定1回で効く3つを先にやる**（合計30分）: Workflow permissions を read に / Secret scanning + Push protection を有効化 / Dependabot を3行設定。

### スケジュール

**cron は UTC で記述する。** JST から9時間引く。日付をまたぐ場合は曜日指定もずらす。

- `workflow_dispatch` を必ず付ける
- `timeout-minutes` を必ず設定する
- GitHub Actions の cron は**数十分〜数時間遅延しうる**。遅延しても壊れない設計（tipoff ガード）を前提とする
- 試合時刻に依存するジョブを固定時刻にしない。当日の最小 `tipoff_at` を基準にスロットを組む

`finalize` は Workers の Cron Trigger（毎時）で実行する。外部アクセスに依存しない純粋な D1 操作であり、スクレイピングの失敗に巻き込まれてはならない。

## シークレット管理

**このリポジトリは public であり、Actions のログも全世界から閲覧できる。**

| 名称 | 保管先 |
|---|---|
| `INGEST_TOKEN` / `INGEST_TOKEN_NEXT` | GitHub Secrets + Workers Secret |
| `FINALIZE_TOKEN` | 同上 |
| `CF_API_TOKEN` | GitHub Secrets（マイグレーション専用、スコープ限定） |

- コード・設定・ログ・エラーメッセージに値を出力しない
- **例外オブジェクトをそのままログ・DBに入れない**。型名と自前メッセージに限定する
- `/api/v1/health` に `error_message` を含めない
- `.gitignore` に `.env`、`*.db`、`batch/cache/`、`**/fixtures/**/*.html`、`models/*.pkl` を含める
- 学習済みモデルは D1 に格納する（Actions の artifact は90日で失効し、別ワークフローから取得できない）
- **`artifact_text` は1.5MB以下。** D1 の1行上限は2,000,000バイト。`num_leaves=7`・木300〜800本なら 0.27〜0.70MB だが、木2,000本で 1.75MB、`num_leaves=15`・1,500本で 2.56MB（超過）。`num_boost_round` の上限を1,200に置き、登録時にサイズを検査する

`D1_DATABASE_ID` は機密ではない（操作には API トークンが別途必要）ため `wrangler.toml` に直書きしてよい。`d1_databases` の `database_id` は必須フィールドで環境変数注入が効かない。

### トークンの無停止回転

1. 新トークンを Workers Secret に `INGEST_TOKEN_NEXT` として追加
2. 認証は現行・次期のいずれかに一致すれば通す
3. GitHub Secrets を更新
4. 次のバッチ成功を確認
5. 旧トークンを削除

## セキュリティヘッダ

`web/public/_headers` に設定する。

```
/*
  X-Content-Type-Options: nosniff
  Referrer-Policy: strict-origin-when-cross-origin
  Permissions-Policy: geolocation=(), camera=(), microphone=()
  Content-Security-Policy-Report-Only: default-src 'self'; script-src 'self' 'sha256-<hash>'; ...
```

テーマ適用の inline script は**最初から `sha256-` ハッシュ指定で許可する形で書く**。後から CSP を入れると真っ先に壊れ、「CSPを入れたらテーマがちらつくのでCSPをやめる」という後退が起きる。Report-Only で数週間様子を見てから強制に切り替える。

## 監視

- `/api/v1/health` にジョブ別の最終成功時刻と**当日カウンタ**（Workers リクエスト数、D1 rows_read 積算、書込行数、キャッシュヒット率、前回バッチ所要時間）を出す。これが受け入れ基準 A-10 の検証手段になる
- `stale` は「**`status = 'SUCCESS'` の最新レコードから24時間**」で判定する。`status` を見ないと、毎日失敗しても最新レコードの時刻は毎日更新されるため遅延と判定されない
- `PARTIAL` は exit 1 で終え、Actions の失敗通知に乗せる
- 直近50試合の Brier が通算より 0.03 以上悪化したら `degraded` を立てる

24時間監視・即時復旧は要件ではない。**壊れても表示が止まらない**ことを優先する。

## 将来のユーザー機能への備え

現在の設計は「公開データのみ・全レスポンスが共有キャッシュ可能・認証は内部書き込み用の共有トークン1本」という3つの前提で最適化されている。ユーザー機能を足すとこれが同時に崩れる。

**`Cache-Control: public` を付けてよいのは、認証を要さず全訪問者に同一の内容を返すエンドポイントに限る。** 認証付きエンドポイントは `private, no-store` を既定とし、Workers の Cache API を経由させない。既存のキャッシュミドルウェアを流用すると、最初にアクセスしたユーザーのレスポンスがエッジにキャッシュされ、他人のデータが配信される。

## 禁止事項

- 有料プラン・有料APIの導入
- D1 REST API の直接呼び出し
- ISR の使用
- 主要導線を Workers 経由にすること
- 特徴量生成・学習での D1 逐次クエリ
- シークレットのコード内記述、例外のそのままのログ出力
- `is_final = 1` の予測レコードの変更
- 生データの一括取得API
- `timeout-minutes` / `permissions` なしのワークフロー
- `pull_request_target` の使用
- 学習済みモデルのコミット
- 文字列連結によるSQL構築

---
title: B.PREDICT 運用ガイド
subtitle: 運営者・開発者向け
version: ver. 1.0.0 beta
---

# この文書の位置づけ

**設計の出典ではありません。** 仕様の正本は次の 4 つで、矛盾したらそちらが優先します。

1. `CLAUDE.md`（絶対ルール）
2. `docs/requirements.md`
3. `docs/design-basic.md`
4. `docs/design-detail.md`

この文書は **「どう動かすか」だけ**をまとめたものです。
「なぜそうなっているか」は上の 4 つにあります。

---

# 構成

3 つの実行環境に役割を分けています。

| 環境 | 役割 | 言語 |
|---|---|---|
| GitHub Actions | 取得・特徴量・学習・推論・静的 JSON の書き出し | Python |
| Cloudflare Workers | **書き込みの関門**、動的クエリの配信 | TypeScript |
| Cloudflare Pages | 画面、**予測データの静的配信** | Next.js（静的出力） |

## 守らなければならない 3 点

| | 内容 |
|---|---|
| **1** | **D1 への書き込みは Workers 経由だけ。** D1 REST API を直接叩かない |
| **2** | **バッチは D1 を入力として読まない。** 学習・特徴量・推論の入力は `batch/snapshot/*.parquet` だけ |
| **3** | **確定した予測は変更しない。** 再推論は旧行を無効化して新しい行を足す |

2 の例外は「運用上の読み取り」です（取得済み ID の照会、結果照合、モデルの読み出し）。
これらは `/internal/*` の GET で行います。**入力データとしての読み取りとは混ぜません。**

---

# 日次の流れ

`daily-ingest` が 1 日 4 回、次の順に走ります。

```
1   前日・当日の結果を取得して取り込む
1b  未実施の試合を取り込む（最初のスロットだけ）
2   スナップショットを更新する
3   結果照合 → 的中率の集計 → 実績の集計 → Elo 再計算
4   推論（tipoff > now の試合）
5   静的 JSON を書き出してコミットする
6   コミットがあった回だけ Pages へ配る
```

**外部アクセスの失敗が内部処理を巻き添えにしません。** 429 / 503 で中止するのは
取得区間だけで、照合・Elo・推論・書き出しは実行して `PARTIAL` で終わります。

## 定期実行の時刻と遅延

| ワークフロー | cron (UTC) | JST |
|---|---|---|
| `daily-ingest` | `17 2` / `17 6` / `17 10` / `17 14`（**4 本の別々の式**） | 11:17 / 15:17 / 19:17 / 23:17 |
| `train` | 毎月 1 日 19:00 | 毎月 2 日 04:00 |
| `parser-canary` | 毎日 22:00 | 毎日 07:00 |

**GitHub の定期実行は数時間遅れます。** 2026-10-15〜10-21 の実測で **+2時間59分〜+6時間53分**
でした。設計は遅延しても壊れないようにしてあります（開始時刻を過ぎた試合への書き込みは
API が 409 で拒否します）。

**4 つを 1 つの cron 式にまとめないでください。** `github.event.schedule` は式を
そのまま返すため、まとめると 4 スロットが同じ文字列になり、ステップ 1b の分岐が
全スロットで真になります。

---

# 手で回すワークフロー

| ワークフロー | 用途 | 外部アクセス |
|---|---|---|
| `backfill` | 過去シーズンの一括取得（再開可能） | あり |
| `rebuild-derived` | 派生テーブル（Elo・会場履歴）を全期間洗い替えて D1 へ | なし |
| `resolve-venue-geo` | 会場の座標を 1 回だけ解決して CSV に固定 | あり |
| `derive-primary-venues` | 本拠会場を導出して CSV に固定 | なし |
| `ingest-rosters` | 登録選手一覧の取り込み（1 回だけ） | あり |

起動は `gh workflow run <名前>.yml` です。

## backfill の上限

**「1 日 1 シーズン」ではなく「1 日の書き込み行数」で管理します。**
1 シーズンは実測で約 65,800 行（枠の 66%）です。

```
1 日の見積りの上限 = 92,000 行（枠の 8% を誤差ぶんとして残す）
1 試合あたり       = 118.6 行（実測）
端数に渡す --limit = floor((92,000 − 当日の消費 − 200) / 118.6)
```

当日の消費は `ingestion_logs` の当日 UTC の `rows_affected` の合計 × 118.6 で求めます。
**1 日の起動は最大 2 回**にしてください（1 回ごとに日程ページを取り直すため）。

---

# スクレイピングの作法

**これを破ると取得を止められます。** 実装（`batch/scraper/client.py`）が強制します。

| 項目 | 値 |
|---|---|
| リクエスト間隔 | 最低 3 秒 ＋ 0〜1 秒のジッタ |
| 並列度 | 1（並列取得しない） |
| 日次リクエスト上限 | 3,000（同じ UTC 日。プロセスを跨いで保たれる） |
| レスポンスサイズ上限 | 5 MB |
| タイムアウト | 30 秒 |
| 429 / 503 | **取得区間のみ中止。同じ UTC 日には再試行しない** |
| User-Agent | 識別名と連絡先 URL を含める |

**`robots.txt` と利用規約をハッシュで照合します。** 変更があればジョブを中止します。

## 規約が変わったとき

ジョブのログに「第 N 節『〜』が変わった」と出ます。手順は次のとおりです。

1. 何が変わったかをログで確認する
2. **運営者が公式サイトを読んで判断する**（承認するのは人であって、コマンドではありません）
3. GitHub Variables の `SCRAPER_TERMS_SHA256` を更新する
4. 指紋を作り直してコミットする

```bash
SCRAPER_TERMS_SHA256=<新しい値> \
  python -m batch.jobs.parser_canary --write-terms-fingerprint
```

---

# ローカルでの作業

## 検査（CI と同じもの）

```bash
# Python
ruff check batch && mypy batch
pytest batch/tests -v
pytest batch/tests/test_leakage.py -v    # 特徴量を触ったら必ず

# API
cd api && npm ci && npx tsc --noEmit && npx eslint . && npm run test

# フロント（next lint は Next.js 16 で削除。eslint を直接呼ぶ）
cd web && npm ci && npx tsc --noEmit && npx eslint . && npm run build
find web/out -type f | wc -l             # 18,000 以下であること
```

`web` には画面を守る検査が別にあります。

| コマンド | 見ていること |
|---|---|
| `npm run test:tokens` | 両テーマでコントラスト比 4.5:1 以上 |
| `npm run test:budget` | 初期 JS が gzip 180 KB 以下 |
| `npm run test:disclaimer` | **全ページ**に出典・版・著作権表示があり、禁じた表記が無い |
| `npm run test:games` | 一覧に出ている試合に必ず詳細ページがある |
| `npm run test:nav` | タブの現在地とテーマ切替の読み上げ |
| `npm run test:links` / `test:bar` / `test:notes` / `test:stats` / `test:unit` | リンク・勝率バー・注記・集計・`lib/` の単体 |

## ローカル実行

```bash
wrangler d1 migrations apply bpredict --local
wrangler d1 execute bpredict --local --file=db/seeds/dev/seed.sql
cd api && npm run dev                    # wrangler dev

API_BASE_URL=http://127.0.0.1:8787 INGEST_TOKEN=dev \
  python -m batch.jobs.daily_ingest --only-results --dry-run
```

**本番 D1 に書きながら開発しないでください。** 確定済みのデータを壊す危険を
常時抱えることになります。

---

# 障害対応

| 症状 | 最初に見るもの | 対応 |
|---|---|---|
| 更新遅延バナーが出る | Actions のログ | 失敗したジョブを特定して再実行 |
| `parser-canary` が落ちた | 対象ページの構造 | **合成 fixture を作ってテストを書いてから**直す |
| パースエラーが多発 | `ingestion_logs.error_type` | 同上 |
| 予測が出ない | `model_versions.is_active` | 有効なモデルが揃っているか |
| 精度が急落 | 的中率ページ、`degraded` | 直前のモデル変更を確認。ロールバック |
| 取り込みが 0 件 | ログの要約行 | 飛ばした理由ごとの件数を見る |

## モデルのロールバック

```sql
UPDATE model_versions SET is_active = 0
 WHERE model_type = 'WINNER' AND target = '' AND league = 'PREMIER';
UPDATE model_versions SET is_active = 1 WHERE version = 'winner-v1.0.0';
```

**この順序でのみ成功します**（部分ユニークインデックスが有効モデル 2 本を防ぎます）。
**確定済みの予測は変更しません。**

## 本番 D1 を読むとき

障害調査で手元から SELECT を流す場合は、**読み取り専用スコープの別トークン**を
手元にだけ置いてください。CI とリポジトリには入れません。

---

# 無料枠

**年間運用費は原則ゼロ、上限 5,000 円です。** 上限のない従量課金構成は採りません。

| リソース | 枠 | 想定 |
|---|---|---|
| **D1 クエリ数** | **50 / Worker 呼び出し** | **最初に当たる制約** |
| D1 バインドパラメータ | 100 / 文 | `floor(100 ÷ 列数) × 40` 行 |
| D1 書込行 | 10 万 / 日（00:00 UTC にリセット） | 日次 約 8,000 行 |
| D1 読取行 | 500 万 / 日 | 約 1 万 / 日 |
| Workers リクエスト | 10 万 / 日 | 数百〜数千 |
| Pages ファイル数 | 20,000 / デプロイ | 約 7,700（**1 ルート 5 ファイル**） |
| Pages ビルド | 500 / 月 | 最大 120 |

**主要導線（今日の予測・試合一覧）は Workers も D1 も経由しません。**
枠が枯れても当日の予測は表示され続けます。

---

# 秘密情報

**このリポジトリは public で、Actions のログも全世界から読めます。**

| 名称 | 置き場 | 用途 |
|---|---|---|
| `INGEST_TOKEN` / `INGEST_TOKEN_NEXT` | GitHub Secrets ＋ Workers Secret | 内部 API（2 キー方式） |
| `FINALIZE_TOKEN` | 同上 | freeze 専用 |
| `CF_API_TOKEN` | GitHub Secrets | マイグレーション専用（D1 Edit） |
| `CF_PAGES_TOKEN` | GitHub Secrets | Pages へ配る専用 |
| `CF_API_TOKEN_RO` | **手元のみ** | 障害調査の SELECT（Read のみ） |

**用途ごとにトークンを分けます。** `CF_API_TOKEN` では配れず、`CF_PAGES_TOKEN` で
D1 は触れません。

## やってはいけないこと

- 例外オブジェクトをそのままログ・DB に入れる（URL に ID が含まれます）
- `/api/v1/health` に `error_message` を含める
- 取得した生 HTML、`.env` の実ファイル、学習済みモデルバイナリをコミットする
- D1 クエリを文字列連結で組む（必ず `prepare().bind()`）

---

# 止まるべきとき

次のいずれかに当たったら、作業を続けずに報告してください。

- 文書間に矛盾がある
- 設計にない仕様を決める必要がある
- 無料枠を超える構成が必要に見える
- 外部サイトの構造が設計の前提と違う
- **検証精度が 85% を超えた**（データリークの疑い）
- 整合化が収束しない、`InfeasibleTargetError` が頻発する
- 法務・著作権に関わる判断が必要になった

---

# 公開前に満たすこと

**受け入れ基準 A-01〜A-19 がすべて「満たしている」ことが条件です。
「検証不能」が 1 件でもあればリリースしません。**

加えて、CI がグリーンであること、`test_leakage.py` が全件通ること、
Phase 0 の実機検証（P0-11〜P0-16）が終わって設計に反映されていること。

> **2026-10-22 時点で A-01（根拠 3 件以上）は未達です。**
> 出場者情報（`game_entries`）が未取得のため、根拠の「選手」グループが定数で
> 寄与が厳密に 0 になり、出る根拠が 2 件にとどまります。

---

© 2026 B.PREDICT

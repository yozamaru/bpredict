# P0-13  D1 `batch()` のクエリ計上を測る

`batch()` の中の各ステートメントが、Free プランの「1 Worker 呼び出しあたり50クエリ」に
どう数えられるかは Cloudflare の公式ドキュメントに記載がない。設計は **1文＝1クエリ**という
最も厳しい前提で書いてあり、**実測するまで緩めない**（`docs/design-detail.md` 3.4）。

## 使い捨ての環境で測る

**本番の `bpredict` データベースには絶対に向けないこと。** 計測専用を新しく作る。

```bash
cd verification/07_d1_batch_query_count

# 1. 計測専用の D1 を作る（出力された database_id を控える）
wrangler d1 create d1-batch-probe

# 2. 設定を用意して database_id を貼る
cp wrangler.toml.example wrangler.toml
$EDITOR wrangler.toml

# 3. 計測用テーブルを作る
wrangler d1 execute d1-batch-probe --remote --file=schema.sql

# 4. デプロイ
wrangler deploy
```

## 測る

```bash
# 文数を 1 → 1000 まで増やして、失敗し始める本数を探す
curl -s https://d1-batch-probe.<サブドメイン>.workers.dev/probe | jq

# 気になる本数を単発で試す
curl -s "https://d1-batch-probe.<サブドメイン>.workers.dev/probe?n=60" | jq
```

## 結果の読み方

| `firstFailureAt` | 意味 | 設計への影響 |
|---|---|---|
| 51 前後 | **1文＝1クエリ。** 設計の前提どおり | バッチサイズをそのまま維持する。緩めない |
| なし（1000文でも通る） | `batch()` 全体が1クエリとして数えられている | 1リクエストあたりの行数を上げられる。ただし**バインドパラメータ100/文の制約は残る**ので、1文の行数（`floor(100/列数)`）は変わらない |
| 51 より大きい特定の値 | 50クエリ制限とは別の上限（SQL文長 100KB など）に当たっている | その上限を `docs/design-detail.md` 3.4 に追記する |

`firstFailureAt` と `maxSucceeded` を `verification/RESULTS.md` の P0-13 の行に記録する。
**「問題なし」とだけ書かない。** 何文まで通ったかの実数を書く。

## 片付け

計測が済んだら、課金対象を残さないために消す。

```bash
wrangler delete                       # Worker を削除
wrangler d1 delete d1-batch-probe     # 計測用 DB を削除
rm wrangler.toml                      # database_id を含むので消す
```

## おまけ

`/cpu?rows=500` は、同一リクエスト内で JSON パース → batch 文の組み立てまでを行う。
P0-15（Zod の検証コスト）を実機で確かめる足場として使える。ただし Workers の
`Date.now()` は粗いため、**CPU 時間は Cloudflare のダッシュボード（Workers → Metrics）で確認する**。

---
description: 無料枠に収まっているか検査する
---

現在の構成が無料枠に収まっているか検査してください。

```bash
# アプリ側のカウンタ（まずこれで足りるか確認する）
curl -s "$API_BASE_URL/api/v1/health" | jq '.data.quota'

# D1 の読取行数（直近のバッチ）。読み取り専用スコープのトークンで実行し、
# CF_API_TOKEN（マイグレーション用の Edit スコープ）を流用しない
wrangler d1 execute bpredict --remote --command \
  "SELECT job, started_at, d1_rows_read FROM ingestion_logs
   WHERE d1_rows_read IS NOT NULL ORDER BY started_at DESC LIMIT 10;"
```

Cloudflare 側の実測値は GraphQL Analytics API で取得する（ダッシュボード目視に頼らない）。

確認する観点:

| リソース | 無料枠 |
|---|---|
| Workers リクエスト | 10万/日 |
| D1 読取行 | 500万/日 |
| D1 書込行 | 10万/日 |
| D1 ストレージ | 5GB |

1. **いずれの1日も**枠を超えていないか（月間平均ではスパイクを検出できない）
2. 直近のコード変更で D1 読取や Workers 呼び出しが増えていないか
3. 主要導線が静的配信のままか（Workers を経由していないか）
4. キャッシュ TTL が `docs/design-detail.md` 3.5 の仕様通りか

有料プランへの移行が必要になりそうなら、**実装せずに報告する**。年額上限は5,000円（Apple Developer 年会費を除く）で、Workers Paid は年額約9,000円のため移行しない。

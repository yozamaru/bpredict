---
description: データ取り込みの状態を確認する
---

データ取り込みの状態を確認してください。

**まず公開・内部エンドポイントで見る。** `wrangler d1 execute --remote` は最後の手段で、使う場合は `CF_API_TOKEN`（Edit スコープ）を流用せず、**読み取り専用スコープの別トークン**を手元で使う。

```bash
# 稼働状態と無料枠の消費（ジョブ別の最終成功時刻・stale・当日カウンタ）
curl -s "$API_BASE_URL/api/v1/health" | jq

# 照合待ちの確定予測（Bearer 必須）
curl -s -H "Authorization: Bearer $INGEST_TOKEN" \
  "$API_BASE_URL/internal/predictions/pending?limit=50" | jq

# 上記で足りない場合のみ: 直近の実行ログ（読み取り専用トークンで実行する）
wrangler d1 execute bpredict --remote --command \
  "SELECT job, status, started_at, finished_at, rows_affected, error_type
   FROM ingestion_logs ORDER BY started_at DESC LIMIT 10;"

# 予測が生成されていない直近の試合（同じく読み取り専用トークンで実行する）
wrangler d1 execute bpredict --remote --command \
  "SELECT g.id, g.game_date FROM games g
   LEFT JOIN predictions p ON p.game_id = g.id AND p.is_active = 1
   WHERE g.status = 'SCHEDULED' AND p.id IS NULL
   ORDER BY g.game_date LIMIT 20;"
```

確認する観点:

1. 失敗・中断（`PARTIAL` / `ABORTED` / `FAILED`）がないか
2. `stale` が立っていないか（`SUCCESS` の最新から24時間以上）
3. 無料枠の消費が想定内か
4. 予測が欠けている試合がないか

パースエラーがあれば対象URLを特定し、サイト構造の変更を調査する。修正は `.claude/skills/scraping/SKILL.md` に従い、**本番アクセスで試行錯誤せず、合成 fixture をテストに固定してから**直すこと。

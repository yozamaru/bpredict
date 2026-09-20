---
description: データ取り込みの状態を確認する
---

データ取り込みの状態を確認してください。

```bash
# 直近の実行ログ
wrangler d1 execute bpredict --remote --command \
  "SELECT job, status, started_at, finished_at, rows_affected, error_type
   FROM ingestion_logs ORDER BY started_at DESC LIMIT 10;"

# 稼働状態と無料枠の消費
curl -s "$API_BASE_URL/api/v1/health" | jq

# 予測が生成されていない直近の試合
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

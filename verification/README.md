# Phase 0 検証スクリプト

設計上の前提を数値で確かめるためのスクリプト。実装前に実行し、結果を設計書へ反映する。

| ファイル | 検証内容 | 依存 |
|---|---|---|
| `01_ece_noise_floor.py` | 完全較正下での ECE 分布。採用ゲートの閾値をnに応じて決める | numpy |
| `02_acceptance_criteria_power.py` | 受け入れ基準とモデル比較の統計的検出力 | numpy |
| `03_reconciliation_logit.py` | 個人スタッツのチーム整合化アルゴリズムの収束 | numpy, scipy |
| `04_artifact_size_and_batch_limits.py` | artifact サイズ推定と D1 バッチ上限の算術 | numpy |
| `05_workers_cpu_budget.mjs` | JSON パース＋検証の CPU コスト | Node のみ |

## このサンドボックスで実行できなかった項目

PyPI と npm レジストリが組織ポリシーで 403 のため、以下は未実行。
実機（開発環境）で必ず実行すること。

- **LightGBM の実 artifact サイズ** — `04` は書式からの推定値。
  実測: 設計のパラメータで学習し `booster.save_model()` の出力バイト数を測る
- **Next.js の実ビルド** — `app/games/[date]` と `app/games/[id]` の衝突再現、
  および static export の1ルートあたり生成ファイル数
- **D1 `batch()` のクエリ計上方法** — バッチ内の各ステートメントが
  「1呼び出しあたり50クエリ（Free）」にどう数えられるか。実アカウントで計測
- **Zod の検証コスト** — `05` は手書き検証での測定。Zod は数倍遅い

# マイグレーション

`docs/design-detail.md` 1章 の DDL を、適用順に並べたもの。

| ファイル | 内容 | オブジェクト |
|---|---|---|
| `0001_master.sql` | マスタ（恒久・年度断面・名寄せ） | 9テーブル + 3インデックス |
| `0002_facts.sql` | ファクト（試合・スタッツ・エントリー） | 5テーブル + 7インデックス |
| `0003_derived.sql` | 派生（レーティング） | 1テーブル |
| `0004_models.sql` | モデルのバージョン管理 | 1テーブル + 1インデックス + サイズガード |
| `0005_predictions.sql` | 予測（親子5テーブル） | 5テーブル + 4インデックス |
| `0006_evaluation.sql` | 評価（照合結果・集計層） | 2テーブル + 2インデックス |
| `0007_ops.sql` | 運用（ジョブ実行履歴） | 1テーブル + 1インデックス |
| `0008_freeze_triggers.sql` | 確定予測の凍結トリガ | 12トリガ |
| `0009_venue_name_at_game.sql` | `games` に当時の会場名を追加 | 1列 |
| `0010_games_drop_natural_key_unique.sql` | `games` の自然キー UNIQUE を外す（テーブル再作成） | 1テーブル + 4インデックス |
| `0011_accuracy_summary_score_mae.sql` | 的中率の集計に予想スコアの誤差を追加 | 1列 |
| `0012_stat_summary.sql` | 実績の集計（選手・クラブ） | 2テーブル + 1インデックス |
| `0013_accuracy_summary_hit_rate.sql` | 帯ごとの的中率を追加（`actual_rate` と別の量） | 1列 |

合計 **26テーブル / 19インデックス / 13トリガ**。`games` は25列、`accuracy_summary` は11列。
0010 の `games_rebuild` は再作成の作業名であり、最終の表名は `games` である（数に含めない）。**この合計は `docs/db-schema.md` が生成する表と突き合わせる** —あちらは D1 の実データから作る。

## 規約

- **追記のみ。適用済みのファイルを編集しない。** 必ず新しい番号のファイルを追加する。
  適用済みを書き換えるとローカルと本番でスキーマが分岐し、`migrations apply` が沈黙して壊れる
- ファイル名は `NNNN_name.sql`（4桁ゼロ埋め・連番・小文字とアンダースコア）。
  `test_migration_filenames_are_ordered_and_unique` が検査する
- **列挙値と値域には必ず CHECK 制約を付ける。** スクレイピングは入力が信用できないパイプラインであり、
  値域の防壁をパーサだけに置くのは弱い
- 単一性は部分ユニークインデックスで担保する（`is_active` / `is_final` / `model_versions.is_active`）
- `DROP TABLE` / `DROP COLUMN` を含む変更は、事前に確認を取る
- **設計を変えるときは先に `docs/design-detail.md` を直す。**
  `test_migrations_match_design_doc` が文書とスキーマの一致を検査するため、
  文書を直さずにマイグレーションだけ足すとテストが落ちる

## テストとの関係

テストはこのディレクトリを**唯一のスキーマの出典**とする（`docs/design-basic.md` 8.1）。
`batch/tests/conftest.py` が `*.sql` をファイル名順に in-memory SQLite へ適用する。
テスト用に別の DDL を書くと、テストが本番のスキーマを反映しなくなる。

```bash
pytest batch/tests -v
```

## 適用

```bash
wrangler d1 migrations apply bpredict --local     # まずローカルで検証する
wrangler d1 migrations apply bpredict --remote    # 本番適用は運営者の承認を経る
```

**データベース名は `bpredict`。** `api/wrangler.toml` の `d1_databases` が
`migrations_dir = "../db/migrations"` を指しているため、`api/` から実行する。

**本番は 0012 まで適用済み**（2026-10-07）。**0013 は未適用**（2026-10-08 に追加）。**`--remote` は運営者の承認を経て実行する** —
`CF_API_TOKEN` は Edit スコープであり、取り消せない操作を含む。
適用の状態は `wrangler d1 migrations list bpredict --remote` で確認する。

スキーマ自体の妥当性は、in-memory SQLite への適用で検証済みである（`pytest batch/tests`）。

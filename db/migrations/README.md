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

合計 24テーブル / 18インデックス / 13トリガ。

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
wrangler d1 migrations apply <DB名> --local     # まずローカルで検証する
wrangler d1 migrations apply <DB名> --remote    # 本番適用は手動承認を経る
```

**まだ実行していない。** 理由は2つ。

1. **データベース名が決まっていない。** プロダクト名は仮称で（未決事項 U-01）、
   `wrangler d1 create` で付けた名前は後から変えると `wrangler.toml` と CI が連動する
2. **`wrangler.toml` がまだない**（工程4）。`migrations apply` は既定で
   プロジェクト直下の `migrations/` を見るため、`wrangler.toml` に
   `migrations_dir = "../db/migrations"` を設定する必要がある

スキーマ自体の妥当性は、in-memory SQLite への適用で検証済みである（`pytest batch/tests`）。

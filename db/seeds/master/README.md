# マスタのシードデータ

`python -m batch.jobs.seed_master` が読み込み、Workers の `POST /internal/masters` へ送る（`docs/design-detail.md` 3.4）。**D1 REST API を直接叩かない**（CLAUDE.md 絶対ルール3）。

ここに置くのは**事前に列挙できるもの**だけである。`club_seasons` と会場マスタは集合が事前に決まらないため、backfill が試合データから作る（詳細設計 1.2 / 4.4）。

## ファイル

| ファイル | 行数 | 投入先 |
|---|---:|---|
| `seasons.csv` | 11 | `seasons` |
| `clubs.csv` | 30 | `clubs` |
| `club_source_ids.csv` | 30 | `club_source_ids` |

**DB の列にない列も置いてある。** 出典と決定の根拠を残すためで、`seed_master` は送信時に落とす。後から「なぜこの値なのか」を復元できない状態にしないこと。

## 出典

| 列 | 出典 |
|---|---|
| `seasons.id` / `label` / `league` | シーズン表記から機械的に構成（`2016-17-B1` 形式。リーグを含めるのは詳細設計 1.1 の理由による） |
| `seasons.start_date` / `end_date` | **`/schedule/?year=YYYY&mon=all` の試合日の最小・最大。** 大会区分で絞らず、取り込む試合日の**上位集合**にしてある（狭いと実在する日付が404になる）。導出は `verification/` のスクリプトで再現できる |
| `clubs.id` / `club_source_ids.source_id` | 公式サイトの `TeamID`。**改称・リーグ再編をまたいで不変**（`verification/RESULTS.md`） |
| `clubs.name` | `/standings/?year=YYYY` の**最終在籍シーズン**の表示名 |
| `clubs.slug` | **運営者が決めた値。** 公式サイトに対応するものがない |

## `clubs.slug` の規約

- 公式の英語表記（`/club/en/`）を kebab-case にしたものを基準にする
- **改称があっても変更しない。** `clubs` は恒久エンティティで、slug は `/teams/[slug]` の識別子である。表示名は `club_seasons.name` が持つ
- したがって slug と現在の表示名が一致しない行が生じうる。**URL の安定を優先する**（詳細設計 1.1）

## 更新するとき

- **新シーズンが始まったら** `seasons.csv` に1行足す。`start_date` / `end_date` は日程一覧から導出する（推測で入れない）
- **新規参入クラブが出たら** `clubs.csv` と `club_source_ids.csv` に1行ずつ足す。slug は上の規約に従う
- **改称があっても `slug` と `id` は変えない。** 変えるのは `clubs.name` だけ
- 投入は冪等（サーバ側が `ON CONFLICT DO UPDATE`）。何度流しても同じ結果になる

## 検証

```bash
pytest batch/tests/test_seed_master.py -v
python -m batch.jobs.seed_master --dry-run   # 検証と件数表示のみ。送信しない
```

`test_seed_master.py` は CSV を実際のスキーマ（`db/migrations/*.sql`）に適用し、**公式IDがすべて `club_id` に解決すること**を確認する。これが工程2の完了条件である。

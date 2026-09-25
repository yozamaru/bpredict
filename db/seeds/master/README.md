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

## `venue_revisions.csv` — 会場の収容人数（手入力）

**公式サイトに収容人数がない**（Phase 0 で確認）。`venue_revisions` の名称は
`games.venue_name_at_game` から自動で作られるが、収容人数だけは手入力になる
（詳細設計 1.2 / 4.9）。

| 列 | 内容 |
|---|---|
| `venue_id` | 公式の会場ID（`StadiumCD`）。`venues.id` と同じ |
| `valid_from` | **名称区間の開始日に一致させる。** 一致しない行があるとジョブが中止する |
| `capacity` | **「B.LEAGUE 開催時の観客席数」** に定義を固定する（建物の最大収容と混ぜない）。空欄は NULL（未確認） |
| `source` | 行ごとの出典URL。空欄にしない |

**書き方の手順。**

1. backfill を流し、スナップショットを作る（`scripts/rebuild_snapshot.py`）
2. `python -m batch.jobs.build_venue_revisions --dry-run` を実行し、出力された
   区間の `venue_id` と `valid_from` を見る
3. 2026-27 の26クラブのメイン会場について、開始日を区間に合わせて行を足す。
   代替会場は空欄のまま進めてよい（設計が NULL を許容している）
4. 再実行する。`valid_from` が区間に当たらない行があれば中止するので、そこで直す

**「入場者数の最大値を収容人数とみなす」自動化をしない。** 未来の試合から値を作ることに
なりデータリークの禁止に触れ、観客制限期間では最大値そのものが抑制されていて判定が
循環する（要件 5.3）。

## `venues_geo.csv` — 会場の緯度経度（ジョブが作る。手で直してよい）

**公式サイトに座標はない。** 会場詳細ページ（`/arena_detail/?ArenaCD=<id>`）に住所は
あるため、住所を国土地理院の住所検索でジオコーディングして**1回だけ解決し、この CSV に
固定する**（詳細設計 1.2 / 4.10）。実行時（日次バッチ）には外部サービスへ行かない。

```bash
# 取り込みが全部終わってから流す（会場は取り込みとともに増える）
python -m batch.jobs.resolve_venue_geo --dry-run   # 解決するが書かない
python -m batch.jobs.resolve_venue_geo            # CSV に書く
python -m batch.jobs.resolve_venue_geo --load     # CSV を D1 に送る
```

| 列 | 内容 |
|---|---|
| `venue_id` | 公式の会場ID（`StadiumCD`）。`venues.id` と同じ |
| `name` | 会場名（照合用。DB へは送るが `venues.name` は上書きされない） |
| `prefecture` | 住所の先頭から決めた都道府県。読めなければ空欄 |
| `lat` / `lng` | 国土地理院の住所検索の結果。小数6桁 |
| `address` | ジオコーディングに使った住所（`/arena_detail/` の値） |
| `source` | 出典。国土地理院は**出典表記が条件**である（政府標準利用規約） |

**座標が入っている行は再実行しても取りに行かない。** 手で直した値もそのまま残る。
埋まらなかった会場は `skip <venue_id> <理由>` として出力に出る（住所がない、
候補が返らない、候補の都道府県が住所と食い違う）。**座標が NULL でも先に進む** —
使うのは特徴量 #16（移動距離・検証区分）だけである。

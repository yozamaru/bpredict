# B.PREDICT 詳細設計書

| 項目 | 内容 |
|---|---|
| 版数 | **1.16** |
| 作成日 | 2026-09-19 |
| 改訂 | v1.1: 9領域レビューの指摘を反映（DDL全面改訂） / v1.2: 個人スタッツをフルボックススコアに拡張 / v1.3: 実装前検証の結果を反映（整合化アルゴリズム、DDL の試投数+成功率化、子テーブル凍結、バッチサイズ、WAF、Next.js 16、実装順序） / v1.4: 文書レビューの指摘を反映（チーム目標の整合化、内部GETの追加、列数の検算、`finished_at_is_estimated`、`spectator_restricted` の NULL、freeze の親子同時実行、レスポンス形状の統一） / **v1.5: 実装着手前の再点検を反映（`accuracy_summary` の主キー、`updated_at` の適用範囲、調査用トークンの分離、Phase 0 の記録先） / **v1.6: ボックススコアが埋め込みJSONで配信されている実地確認を反映（`parser/` の責務を「レスポンス本文の解釈」に変更） / v1.7: `player_predictions` に親参照の凍結トリガを追加（凍結の網羅を完成） / v1.8: Phase 0（P0-5）の結果を反映（大会区分 `competition` の追加、`club_seasons` の出典と構築工程、復帰クラブの Elo 初期値） / v1.9: 会場マスタの出典を確定（`venues.id` に公式の `StadiumCD` を採用、会場行は backfill が構築、座標は国土地理院で1回だけ解決、収容人数は手入力） / v1.10: 工程2の前提を確定（`POST /internal/masters` の追加、`clubs.slug` は手入力で改称でも不変、`seasons` の開始・終了日は日程一覧から1回だけ導出） / v1.11: 工程3の CI を実態に合わせた（api / web のジョブは `detect` で分岐、ESLint は工程4、Dependabot の npm は後追い、ワークフローの不変条件をテストで固定） / v1.12: 工程4a（Workers API の土台と `POST /internal/masters`）を実装し、工程2の D1 投入を完了させた / **v1.13: 工程4b（残りの `/internal/*` と freeze の Cron Trigger）を実装した / **v1.14: 工程5（スクレイパ・パーサ）を実装し、Phase 0 の実地確認で判明した非選手行2種の区別・旧年度の項目欠損・カナリアの検査対象を反映した / **v1.15: 工程11a の実測で外れた前提を反映（初期JS の上限を 180KB、静的生成の範囲を直近3シーズン）と、未決事項 U-10 の解決 / **v1.16: 工程7（特徴量生成とリーク検証）を実装し、`team_ratings` の1行の意味（その試合日の終了時点）と `rest_days` の定義（中N日）を明記した** |
| 上位文書 | `docs/design-basic.md` |

---

## 1. データベース定義

D1（SQLite）を使用する。日時は ISO 8601 文字列（UTC）、日付は `YYYY-MM-DD`。

**ID 体系の方針**: マスタ・試合の `id` は**公式サイトの ID を採用し、独自採番しない**。**会場も例外ではない** — 試合の埋め込みJSONに `StadiumCD`（公式の会場ID）があり、全シーズンに存在する（Phase 0 で確認）。`venues.id` にはこれを使い、独自採番も会場名による名寄せも行わない。`venue_source_keys` は `club_source_ids` と同じ「公式ID → 内部ID」の対応表として持つ。

> v1.8 まで「公式 ID が存在しないエンティティ（会場など）のみ `v_` プレフィックスで採番し、`venue_source_keys` で名寄せする」と書いていたが、**会場には公式IDがあったため誤りだった**（`verification/RESULTS.md`）。

**列挙値には必ず CHECK 制約を付ける。** スクレイピングは入力が信用できないパイプラインであり、値域の防壁をパーサだけに置くのは弱い。

### 1.1 マスタ（恒久）

```sql
-- 恒久的なクラブ。名称変更・リーグ移動があっても不変
CREATE TABLE clubs (
  id         TEXT PRIMARY KEY,
  slug       TEXT NOT NULL UNIQUE,
  name       TEXT NOT NULL,                    -- 現在の表示名
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 恒久的な選手。人物マスタに純化する
CREATE TABLE players (
  id         TEXT PRIMARY KEY,                 -- 公式サイトの選手ID
  name       TEXT NOT NULL,
  height_cm  INTEGER,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE venues (
  id         TEXT PRIMARY KEY,
  name       TEXT NOT NULL,                    -- 現在の表示名
  prefecture TEXT,
  lat        REAL,
  lng        REAL,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE seasons (
  id         TEXT PRIMARY KEY,                 -- '2026-27-PREMIER'
  label      TEXT NOT NULL,                    -- '2026-27'
  league     TEXT NOT NULL
             CHECK (league IN ('B1','B2','B3','PREMIER','ONE','NEXT')),
  start_date TEXT NOT NULL,
  end_date   TEXT NOT NULL
);
```

`seasons.id` にリーグを含める。旧版の `'2026-27'` 単独では、同一シーズンの PREMIER と ONE を同時に持てず、B.ONE 拡張時に主キー変更＝全ファクトテーブルの FK 移行が必要になる。実データ投入前の今なら修正コストはゼロ。

#### `clubs.slug` は手で決め、改称でも変えない

`slug` は `/teams/[slug]` の識別子である。**`db/seeds/master/clubs.csv` に手で持ち、改称があっても変更しない。**

**機械的に導出しない。** 英語名（`TeamNameE`）の kebab-case が候補だったが、2016-17 の試合で `TeamNameE` が空文字だった実例があり、全クラブで取れる保証がない。さらに改称すると英語名も変わるため、導出した slug は改称のたびに変わり **URL が変わる**。

**改称で変えないのは、`clubs` が恒久エンティティだからである。** 表示名は `club_seasons.name` が持ち、画面にはそれを出す。slug は恒久な識別子に徹する。結果として `703` の slug と現在の表示名（宇都宮ブレックス）が一致しない状態が生じうるが、URL の安定を優先する。

出典が公式サイトではなく運営者の決定であるため、**CSV に決定の根拠を列として残す**。形式は `^[a-z0-9-]{1,40}$`（3.2）に従い、`UNIQUE` 制約で重複を防ぐ。

#### `seasons.start_date` / `end_date` は一度だけ導出して固定する

**出典はそのシーズンの日程一覧（`mon=all`、大会区分で絞らない）から得た試合日の最小・最大。** 1回だけ導出して `db/seeds/master/seasons.csv` に固定し、**実行時には取得しない**（会場の座標と同じ扱い。1.2）。

**大会区分で絞らずに取る。** 取り込むのは `REGULAR` と `PLAYOFF` だけだが、範囲はプレシーズンやオールスターを含む**上位集合**にする。狭いと実在する試合日が404になる一方、広い分には害がない（該当日は「試合がありません」と表示される）。絞る手間を増やしてまで範囲を詰める理由がない。

**公式の「年間スケジュール」ページは出典にならない。** 当季のみを扱い、日付もガント風の描画で、11シーズン分の範囲が取れない（Phase 0 で確認）。

**試合投入後に求める経路は循環する。** `games.season_id REFERENCES seasons(id)` のため seasons が先に必要で、両列は NOT NULL である。

この2列は **URL 空間の有限化**（シーズン範囲外の日付を D1 到達前に404）に使われ、無料枠を守る仕組みの一部である（3.5）。**実際の試合日を必ず含む範囲**でなければならない。狭いと実在する試合日が404になる。

### 1.2 マスタ（年度断面・履歴）

```sql
-- シーズンごとのクラブ断面。名称・リーグ・本拠は年度で変わる
CREATE TABLE club_seasons (
  club_id          TEXT NOT NULL REFERENCES clubs(id),
  season_id        TEXT NOT NULL REFERENCES seasons(id),
  name             TEXT NOT NULL,
  short_name       TEXT NOT NULL,
  league           TEXT NOT NULL
                   CHECK (league IN ('B1','B2','B3','PREMIER','ONE','NEXT')),
  primary_venue_id TEXT REFERENCES venues(id),
  color_primary    TEXT,
  color_secondary  TEXT,
  PRIMARY KEY (club_id, season_id)
);
CREATE INDEX idx_club_seasons_league ON club_seasons(season_id, league);

-- 公式サイトのチームID（旧B1 / 新リーグ）を club_id に解決する
CREATE TABLE club_source_ids (
  source_id  TEXT PRIMARY KEY,
  club_id    TEXT NOT NULL REFERENCES clubs(id),
  valid_from TEXT NOT NULL,
  valid_to   TEXT NOT NULL DEFAULT '9999-12-31',
  note       TEXT
);
CREATE INDEX idx_csi_club ON club_source_ids(club_id);

-- 選手の所属断面。シーズン途中の移籍にも対応する
CREATE TABLE player_seasons (
  player_id   TEXT NOT NULL REFERENCES players(id),
  season_id   TEXT NOT NULL REFERENCES seasons(id),
  club_id     TEXT NOT NULL REFERENCES clubs(id),
  number      TEXT,
  position    TEXT CHECK (position IN ('PG','SG','SF','PF','C')),
  roster_type TEXT CHECK (roster_type IN ('JP','NATURALIZED','ASIA','FOREIGN')),
  joined_on   TEXT,
  left_on     TEXT,
  PRIMARY KEY (player_id, season_id, club_id)
);
CREATE INDEX idx_pseason_club ON player_seasons(club_id, season_id);

-- 会場の改称・収容人数変更の履歴。過去試合は当時の値で表示する
CREATE TABLE venue_revisions (
  venue_id   TEXT NOT NULL REFERENCES venues(id),
  valid_from TEXT NOT NULL,
  valid_to   TEXT NOT NULL DEFAULT '9999-12-31',
  name       TEXT NOT NULL,
  capacity   INTEGER,
  PRIMARY KEY (venue_id, valid_from)
);

-- 公式の会場ID（StadiumCD）を venue_id に解決する。club_source_ids と同じ役割
CREATE TABLE venue_source_keys (
  source_code TEXT PRIMARY KEY,
  venue_id    TEXT NOT NULL REFERENCES venues(id)
);
```

#### 会場マスタの出典

| 列 | 出典 | 取得の経路 |
|---|---|---|
| `venues.id` | 試合JSONの **`StadiumCD`** | 取り込みで自動 |
| `venues.name` / `venue_revisions.name` | 試合JSONの `StadiumNameJ`（その試合時点の名称） | 取り込みで自動 |
| `venues.prefecture` / `lat` / `lng` | `/arena_detail/?ArenaCD=<cd>` の**住所**を国土地理院でジオコーディング | **1回だけ解決し `db/seeds/master/venues_geo.csv` に固定** |
| **`venue_revisions.capacity`** | **公式サイトに存在しない** | **手入力。`db/seeds/master/venue_revisions.csv` に行ごとの出典URLつき** |

**`StadiumCD` は型がシーズンで違う。** 2016-17 は整数 `3`、2025-26 は文字列 `"169"`。**取り込み前に文字列へ正規化する**（4.4 の正規化5）。

**座標を実行時に取りに行かない。** 会場は一度確定すれば動かないため、日次バッチで外部サービスに依存する理由がない。1回だけ解決して CSV に固定し、リポジトリにコミットする。国土地理院の住所検索は無料・APIキー不要で、出典表記が条件である（政府標準利用規約）。座標を使うのは特徴量 #16（移動距離・**検証**区分）だけで、距離は数百kmの単位であるため精度の要求は低い。

**`capacity` は 2026-27 の26クラブのメイン会場から埋める。** 代替会場は NULL のまま進める。定義は**「B.LEAGUE 開催時の観客席数」に固定**する（建物の最大収容と混ぜると動員率が比較不能になる）。

NULL のときに失うもの: `spectator_restricted` が NULL（= 通常のホームアドバンテージ。**2020-21 / 2021-22 は期間指定で強制的に 1 なのでコロナ期には影響しない**）、`attendance <= capacity × 1.2` の検証がスキップ、動員率（#29・検証区分）が当該会場で欠損。

**「その会場の入場者数の最大値を収容人数とみなす」自動化を行わない。** 出典が不要で定義も正しく見えるが、**未来の試合から値を作ることになりデータリークの禁止に触れる**。さらに観客制限期間は最大値そのものが抑制されており、`spectator_restricted` の判定が循環する。

#### `venue_revisions` の作り方（U-10 の解決）

**名称は取り込みが試合データから作り、収容人数は手入力の CSV から後入れする。** 役割を分ける。

| 列 | 作り方 |
|---|---|
| `name` | **取り込みが自動で作る。** 出典は `StadiumNameJ`（その試合時点の名称）。会場ごとに、名称が変わった最初の試合の `game_date` を `valid_from` とし、直前の行の `valid_to` をその前日にする |
| `capacity` | **手入力。** `db/seeds/master/venue_revisions.csv` に `venue_id` / `valid_from` / `capacity` / 出典URL を持ち、`(venue_id, valid_from)` で突き合わせる |

**名称を手入力にしない。** 日本のアリーナは命名権で頻繁に改称し、11シーズン分を手で追うと必ず抜ける。
`StadiumNameJ` は取り込みで必ず通るレスポンスに含まれ、当時の名称が入っている（4.4）。

**衝突したら止めて報告する。** CSV の行が指す期間の名称と、試合データから作った名称が食い違う場合、
自動でどちらかを採らない。どちらかが誤りであり、**黙って上書きすると過去試合の会場表示が遡って変わる**。

**表記ゆれを自動で名寄せしない。** 名称が変わったら revision を追加するが、**同一シーズン内で名称が
変わった会場は報告する**。命名権の変更はシーズン境界で起きるのが通例であり、シーズン内の変更は
表記ゆれ（全角・半角、「市立」の有無など）の疑いがある。名寄せの規則を推測で作らない。

**revision 行がない会場の表示名は `venues.name`（現在の表示名）にフォールバックする。** ただし
**フォールバックした会場は `ingestion_logs` に記録する**。黙ってフォールバックすると、過去試合の
会場が現在名で表示されていることに気づけない。

**取り込み時に `club_source_ids` で `club_id` に解決してから保存する。** 旧IDのまま保存すると `team_ratings.team_id` が分断され、旧B1の Elo を新リーグへ引き継げない。

日本のアリーナは命名権で頻繁に改称する。`venues.name` を上書きすると過去試合の会場表示が遡って変わり、収容人数の変更で過去の動員率も歪む。

### 1.3 ファクト

```sql
CREATE TABLE games (
  id               TEXT PRIMARY KEY,          -- 公式サイトの試合ID
  season_id        TEXT NOT NULL REFERENCES seasons(id),
  league           TEXT NOT NULL,             -- API応答と一致させるため非正規化
  competition      TEXT NOT NULL              -- REGULAR = リーグ戦 / PLAYOFF = チャンピオンシップ
                   CHECK (competition IN ('REGULAR','PLAYOFF')),
  game_date        TEXT NOT NULL,             -- 変更されうる属性
  tipoff_at        TEXT NOT NULL,
  finished_at      TEXT,                      -- 試合終了時刻。リーク判定の絞り込みはこの列で行う
  finished_at_is_estimated INTEGER NOT NULL DEFAULT 0
                   CHECK (finished_at_is_estimated IN (0,1)),
                                              -- 1 = 実測が取れず tipoff_at + 2時間 を書いた
  home_club_id     TEXT NOT NULL REFERENCES clubs(id),
  away_club_id     TEXT NOT NULL REFERENCES clubs(id),
  venue_id         TEXT REFERENCES venues(id),
  is_primary_venue INTEGER NOT NULL DEFAULT 1 CHECK (is_primary_venue IN (0,1)),
  series_game_no   INTEGER,
  status           TEXT NOT NULL
                   CHECK (status IN ('SCHEDULED','FINISHED','POSTPONED','CANCELLED')),
  rescheduled_to   TEXT REFERENCES games(id), -- 延期先。旧行は POSTPONED で残す
  home_score       INTEGER,
  away_score       INTEGER,
  attendance       INTEGER,
  spectator_restricted INTEGER              -- NULL = 判定不能（attendance か capacity が欠損）
                   CHECK (spectator_restricted IS NULL
                          OR spectator_restricted IN (0,1)),
  result_revision  INTEGER NOT NULL DEFAULT 0, -- スコア訂正のたびに +1
  source_url       TEXT,
  fetched_at       TEXT,
  created_at       TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at       TEXT NOT NULL DEFAULT (datetime('now')),
  CHECK (home_club_id <> away_club_id),
  UNIQUE (season_id, game_date, home_club_id, away_club_id)  -- 重複検出用。upsertキーではない
);
CREATE INDEX idx_games_date     ON games(game_date, status);
CREATE INDEX idx_games_finished ON games(finished_at);
CREATE INDEX idx_games_home     ON games(home_club_id, game_date);
CREATE INDEX idx_games_away     ON games(away_club_id, game_date);
```

**取り込むのはリーグ戦とチャンピオンシップだけである。** 公式サイトの試合一覧は大会区分
（`event`）で分かれており、そこにはクラブ同士の対戦ではない試合が混ざっている。

| サイトの `event` | 内容 | `competition` |
|---:|---|---|
| 2 | B1リーグ / B.PREMIER | `REGULAR` |
| 3 | B1チャンピオンシップ | `PLAYOFF` |
| 4 | B1残留プレーオフ | **取り込まない** |
| 5 | オールスターゲーム | **取り込まない** |
| 11 | B1・B2入替戦 | **取り込まない** |
| 20 | アーリーカップ（2017-18〜2019-20） | **取り込まない** |

**値は不変でラベルだけが変わる。** `2` は 2016-17〜2025-26 が「B1リーグ」、2026-27 から
「B.PREMIER」である（Phase 0 で確認）。ラベルではなく**値で分岐する**。

**ただし `event` の値だけでは絞り込めない。** `event=2` は「そのシーズンの日程」であり、
リーグ戦のほかに**チャンピオンシップ・オールスター・国際試合を含む**。2016-17 の実測では
`event=2` が555試合（リーグ戦540 ＋ CS15）で、加えて非リーグ戦2試合が混ざっていた。
絞り込みは要件 5.3 の3段で行う（CS を先に確定 → `event=2` の残りを REGULAR →
その年度のクラブ一覧にないチームの試合を飛ばす）。

**オールスターゲームを取り込むと Elo が壊れる。** 選抜チーム同士の対戦であってクラブの試合では
なく、`clubs` に存在しないチームが現れる。入替戦は相手が B2 のクラブで、`clubs` にも `seasons`
にも存在しないため対戦相手を解決できない。アーリーカップはプレシーズンの地区大会である。

**列として持ち、取り込み段階でも絞る。** 二重にするのは役割が違うためで、絞り込みは
「クラブの成績に直結しない試合を DB に入れない」ため、列は「リーグ戦とチャンピオンシップを
区別する」ためである。区別を捨てると特徴量 #17（順位・プレーオフ争いの状況）が算出できず、
後から分離するには backfill をやり直すことになる。

**upsert キーは `id`（公式試合ID）とする。** 自然キーに `game_date` を含めると、延期で日付が変わった瞬間に別レコードとして挿入され、旧行が `SCHEDULED` のまま永久に残る。ゴースト試合の予測が的中率の分母を汚染する。

**`finished_at` は列として保持する。読み出し時に計算しない。** すべてのリーク判定クエリがこの値を通るため、`tipoff_at + 2時間` という推定式を呼び出し側に置くと、一箇所直し忘れただけでリークが復活する。取り込み時に次のとおり確定させる。

| 状況 | `finished_at` | `finished_at_is_estimated` |
|---|---|---|
| 終了時刻が取得できた | 実測値 | 0 |
| 終了時刻が取得できない（`status = 'FINISHED'`） | `tipoff_at + 2時間` | **1** |
| 未実施（`SCHEDULED` / `POSTPONED` / `CANCELLED`） | NULL | 0 |

推定値であることを区別できるようにするのは、後から実測が取れたときに上書き対象を特定するためと、リークテストの調査時に「境界に効いているのが実測か推定か」を切り分けるためである。

**`spectator_restricted` は取り込み時に判定して列に書く。** 特徴量生成のたびに計算し直さない（計算式が二箇所に分かれると、片方だけ直したときに Elo と特徴量が食い違う）。

| 条件 | 値 |
|---|---|
| `attendance / capacity < 0.2` | 1 |
| 上記以外で両方の値が揃っている | 0 |
| `attendance` または `capacity` が NULL | **NULL（判定不能）**。Elo 更新では通常のホームアドバンテージを使う |
| シーズンが 2020-21 / 2021-22 | **期間指定で強制的に 1**（入場者数が非公開の試合を取りこぼさないため、上記の判定より優先する） |

公式試合IDが取得できない場合に限り自然キーを使うが、その場合は日程変更の名寄せ処理を明示的に実装する。

```sql
-- チーム視点の試合行。OR 条件を消し、未実施試合も扱える
CREATE TABLE team_games (
  game_id     TEXT NOT NULL REFERENCES games(id),
  club_id     TEXT NOT NULL REFERENCES clubs(id),
  opponent_id TEXT NOT NULL REFERENCES clubs(id),
  season_id   TEXT NOT NULL REFERENCES seasons(id),
  game_date   TEXT NOT NULL,
  finished_at TEXT,
  is_home     INTEGER NOT NULL CHECK (is_home IN (0,1)),
  competition TEXT NOT NULL CHECK (competition IN ('REGULAR','PLAYOFF')),
  result      INTEGER CHECK (result IN (0,1)),   -- NULL = 未実施
  margin      INTEGER,
  PRIMARY KEY (club_id, game_date, game_id)
);
CREATE INDEX idx_team_games_finished ON team_games(club_id, finished_at);
```

**`competition` を `team_games` にも持たせる。** この表は `games` への JOIN を消すために
作ったものであり、勝率・得失点差・連戦の特徴量はここだけを読む。`games` 側にしか持たないと、
大会区分で絞るたびに JOIN が復活し、表の存在理由が失われる。

```sql
CREATE TABLE team_game_stats (
  game_id     TEXT NOT NULL REFERENCES games(id),
  club_id     TEXT NOT NULL REFERENCES clubs(id),
  game_date   TEXT NOT NULL,                   -- JOIN とソートを消すための非正規化
  is_home     INTEGER NOT NULL CHECK (is_home IN (0,1)),
  pts INTEGER,
  fg2m INTEGER, fg2a INTEGER,      -- 選手側と同じ粒度で持つ（整合化の基準になる）
  fg3m INTEGER, fg3a INTEGER,
  ftm  INTEGER, fta  INTEGER,
  oreb INTEGER, dreb INTEGER,
  ast INTEGER, tov INTEGER, stl INTEGER, blk INTEGER,
  pf INTEGER, fd INTEGER,
  possessions REAL,
  fetched_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (game_id, club_id),
  CHECK (fg2m IS NULL OR fg2a IS NULL OR fg2m <= fg2a),
  CHECK (fg3m IS NULL OR fg3a IS NULL OR fg3m <= fg3a),
  CHECK (ftm  IS NULL OR fta  IS NULL OR ftm  <= fta),
  CHECK (pts IS NULL OR (pts >= 0 AND pts <= 250))
);
CREATE INDEX idx_tgs_club ON team_game_stats(club_id, game_date);

CREATE TABLE player_game_stats (
  game_id   TEXT NOT NULL REFERENCES games(id),
  player_id TEXT NOT NULL REFERENCES players(id),
  club_id   TEXT NOT NULL REFERENCES clubs(id),
  game_date TEXT NOT NULL,
  started   INTEGER CHECK (started IN (0,1)),
  minutes   REAL CHECK (minutes IS NULL OR (minutes >= 0 AND minutes <= 60)),
  -- シュート。2P と 3P を別建てで持ち、FG は導出する
  fg2m INTEGER, fg2a INTEGER,
  fg3m INTEGER, fg3a INTEGER,
  ftm  INTEGER, fta  INTEGER,
  -- リバウンド
  oreb INTEGER, dreb INTEGER,
  -- プレー
  ast INTEGER, tov INTEGER, stl INTEGER, blk INTEGER,
  -- ファウル
  pf INTEGER,                -- F  : 自分が犯したファウル
  fd INTEGER,                -- FD : 被ファウル数
  -- 実績としてのみ保持（予測しない）
  plus_minus INTEGER,
  pts INTEGER,               -- 取得値。恒等式の検証に使う
  fetched_at TEXT NOT NULL,
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (game_id, player_id),
  CHECK (fg2m IS NULL OR fg2a IS NULL OR fg2m <= fg2a),
  CHECK (fg3m IS NULL OR fg3a IS NULL OR fg3m <= fg3a),
  CHECK (ftm  IS NULL OR fta  IS NULL OR ftm  <= fta),
  CHECK (pf IS NULL OR (pf >= 0 AND pf <= 6))
);
CREATE INDEX idx_pgs_player ON player_game_stats(player_id, game_date);
```

`fgm` / `fga` / `reb` の列は持たない。`fg2m + fg3m` / `fg2a + fg3a` / `oreb + dreb` で導出できる冗長列であり、持つと不整合の余地が生まれる。

取り込み時に以下の恒等式を検証する。不一致は当該試合をスキップする。

```
pts  = fg2m × 2 + fg3m × 3 + ftm
fg2m ≤ fg2a,  fg3m ≤ fg3a,  ftm ≤ fta
チーム集計の各項目 = その試合の所属選手の合計
```

**FD（被ファウル数）と 2FGM/2FGA の取得可否は Phase 0（P0-9）で検証する。** 用語集には定義があるが、個別試合のボックススコアページでの公開状況は未確認である。取得できない場合は該当項目を予測対象から外す。

`possessions` は取り込み時に算出する。必要項目が欠ける場合は NULL とし、依存する特徴量を無効化する。

```
fga = fg2a + fg3a
possessions = fga - oreb + tov + 0.44 * fta
```

係数 0.44 は NBA のフリースロー試投係数であり、日本のバスケットに当てはまる保証はない。**Phase 0（P0-7）で実データから再推定する。**

```sql
CREATE TABLE game_entries (
  game_id    TEXT NOT NULL REFERENCES games(id),
  player_id  TEXT NOT NULL REFERENCES players(id),
  status     TEXT NOT NULL CHECK (status IN ('ENTRY','OUT','UNKNOWN')),
  source     TEXT NOT NULL CHECK (source IN ('OFFICIAL','ESTIMATED')),
  confidence REAL CHECK (confidence IS NULL OR (confidence BETWEEN 0 AND 1)),
  fetched_at TEXT NOT NULL,
  PRIMARY KEY (game_id, player_id)
);
```

**取得ごとに当該試合の全行を DELETE してから INSERT する（洗い替え）。** 公式エントリーはエントリーした選手だけを返すため、追加のみの upsert では「出場するはず」と推定した行のうち実際には登録外だった選手の行が残り続け、`is_provisional` が永久に解除されない。

`source = 'ESTIMATED'` の行が1件でも残る試合は `is_provisional = 1` とする。

### 1.4 派生

```sql
CREATE TABLE team_ratings (
  club_id      TEXT NOT NULL REFERENCES clubs(id),
  as_of_date   TEXT NOT NULL,
  season_id    TEXT NOT NULL REFERENCES seasons(id),
  elo          REAL NOT NULL,
  off_rating   REAL,
  def_rating   REAL,
  pace         REAL,
  games_played INTEGER NOT NULL,
  PRIMARY KEY (club_id, as_of_date)
);
```

試合日ごとのスナップショット。**1行はその試合日の「終了時点」の値である**（同じ日に複数試合がある場合は、その日の最後の試合まで反映した値）。特徴量生成時は `as_of_date < 対象試合日` の最新行を参照する。この構造自体がリーク防止になっている。**Elo をその場で計算する実装にしない**（全試合を走査する過程で対象試合を含めてしまう事故が起きる）。

**「終了時点」であることは省略できない規約である。** 開始前の値を書くと、`as_of_date < 対象試合日` で絞ったときに前日の結果が反映されず、土日2連戦の日曜の試合が土曜の結果を知らないまま予測される。逆に「終了時点」の行を `as_of_date <= 対象試合日` で読むと当日の結果が混入する（リーク）。**行の意味と絞り込みの不等号は対で決まっている。**

### 1.5 予測

```sql
CREATE TABLE predictions (
  id                TEXT PRIMARY KEY,
  game_id           TEXT NOT NULL REFERENCES games(id),
  season_id         TEXT NOT NULL,             -- accuracy 集計用に非正規化
  model_version     TEXT NOT NULL REFERENCES model_versions(version),
  revision          INTEGER NOT NULL,          -- 世代通番 1,2,3...
  run_id            TEXT NOT NULL,             -- ingestion_logs.id
  predicted_at      TEXT NOT NULL,
  as_of             TEXT NOT NULL,             -- 特徴量が参照してよい上限時刻
  data_as_of        TEXT NOT NULL,             -- 実行時点でDBにあった最新試合の終了時刻
  home_win_prob     REAL NOT NULL CHECK (home_win_prob BETWEEN 0 AND 1),
  pred_margin       REAL,
  pred_total        REAL,
  pred_home_score   REAL,
  pred_away_score   REAL,
  is_provisional    INTEGER NOT NULL DEFAULT 1 CHECK (is_provisional IN (0,1)),
  is_final          INTEGER NOT NULL DEFAULT 0 CHECK (is_final IN (0,1)),
  is_active         INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)),
  feature_snapshot  TEXT NOT NULL,             -- JSON
  created_at        TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE (game_id, model_version, revision)
);
-- 1試合につき有効な予測は常に1本
CREATE UNIQUE INDEX uq_pred_active ON predictions(game_id) WHERE is_active = 1;
-- 確定済みも1本
CREATE UNIQUE INDEX uq_pred_final  ON predictions(game_id) WHERE is_final  = 1;
CREATE INDEX idx_pred_eval ON predictions(model_version, is_final, season_id);
```

`away_win_prob` 列は持たない（`1 - home_win_prob` で導出できる冗長列）。

`home_win_prob` がどちらの経路（Winner 直接 / Margin から導出）で算出されたかは `model_versions.win_prob_source` に記録する。予測行ごとには持たない（同一モデルバージョン内では常に同一のため）。

```sql
-- 整合化の目標値。チーム側も「試投数 + 成功率」で持つ
CREATE TABLE prediction_team_targets (
  prediction_id TEXT NOT NULL REFERENCES predictions(id),
  club_id       TEXT NOT NULL REFERENCES clubs(id),
  is_home       INTEGER NOT NULL CHECK (is_home IN (0,1)),

  tgt_fg2a REAL NOT NULL CHECK (tgt_fg2a >= 0),
  tgt_fg3a REAL NOT NULL CHECK (tgt_fg3a >= 0),
  tgt_fta  REAL NOT NULL CHECK (tgt_fta  >= 0),
  tgt_fg2_pct REAL NOT NULL CHECK (tgt_fg2_pct BETWEEN 0 AND 1),
  tgt_fg3_pct REAL NOT NULL CHECK (tgt_fg3_pct BETWEEN 0 AND 1),
  tgt_ft_pct  REAL NOT NULL CHECK (tgt_ft_pct  BETWEEN 0 AND 1),

  tgt_oreb REAL NOT NULL CHECK (tgt_oreb >= 0),
  tgt_dreb REAL NOT NULL CHECK (tgt_dreb >= 0),
  tgt_ast  REAL NOT NULL CHECK (tgt_ast  >= 0),
  tgt_tov  REAL NOT NULL CHECK (tgt_tov  >= 0),
  tgt_stl  REAL NOT NULL CHECK (tgt_stl  >= 0),
  tgt_blk  REAL NOT NULL CHECK (tgt_blk  >= 0),
  tgt_pf   REAL NOT NULL CHECK (tgt_pf   >= 0),
  tgt_fd   REAL NOT NULL CHECK (tgt_fd   >= 0),

  PRIMARY KEY (prediction_id, club_id)
);
```

**目標値を「成功数」ではなく「試投数 + 成功率」で持つ理由。** 成功数と試投数を独立に保存すると、約2%の確率で `成功数 > 試投数` という**実行不能な目標**が生まれる。この状態ではどんな整合化アルゴリズムも目標に到達できない。試投数（非負）と成功率（0〜1）の組で持てば、`成功数 = 率 × 試投数 ≤ 試投数` が**構造的に保証される**。CHECK 制約はこの構造を DB レベルで固定するものであり、外さない。

得点の目標値は別列として持たず、`tgt_fg2_pct × tgt_fg2a × 2 + tgt_fg3_pct × tgt_fg3a × 3 + tgt_ft_pct × tgt_fta` で導出する。

**この表に入る値は、TeamRates の生出力ではなく `reconcile_team_targets()` を通した後の値である（2.4）。** TeamRates と Margin / Total は別モデルであり、生出力の導出得点が `pred_home_score` / `pred_away_score` と一致する保証がない。**予想スコアを正とし、成功率3項目を共通のロジットシフトで寄せてから保存する。** 試投数（`tgt_fg2a` / `tgt_fg3a` / `tgt_fta`）は動かさない。

導出得点が `predictions.pred_home_score` / `pred_away_score` と ±0.5点以内で一致することは `test_team_targets_match_predicted_score`（A-16）で検証する。制約（`成功数 ≤ 試投数`、恒等式）は `test_team_targets_are_feasible` が見る。

**運用規則**

- 再推論時は旧行の `is_active` を 0 にし、新行を `revision + 1` で追加する。**この2文は必ず単一の D1 `batch()` に入れる**（D1 にはリクエストを跨ぐトランザクションがない）。子テーブル（`player_predictions`）の非活性化も同じ `batch()` に含める
- 試合開始時刻を過ぎたら、その時点の `is_active = 1` の行に `is_final = 1` を立てる。**`predictions` と `player_predictions` の2つを単一 `batch()` で、子 → 親の順に UPDATE する**（4.1）
- `is_final = 1` の行は**一切更新・削除しない**。トリガで禁止し、アプリ層の規律だけに頼らない
- 的中率の算出は `is_final = 1` の行のみを使用する

```sql
CREATE TABLE player_predictions (
  id             TEXT PRIMARY KEY,
  prediction_id  TEXT NOT NULL REFERENCES predictions(id),  -- 親の世代に紐付ける
  game_id        TEXT NOT NULL REFERENCES games(id),
  player_id      TEXT NOT NULL REFERENCES players(id),
  club_id        TEXT NOT NULL REFERENCES clubs(id),
  model_version  TEXT NOT NULL,
  revision       INTEGER NOT NULL,
  predicted_at   TEXT NOT NULL,

  avail_prob     REAL NOT NULL CHECK (avail_prob BETWEEN 0 AND 1),
  pred_minutes   REAL NOT NULL CHECK (pred_minutes >= 0),  -- 整合化後の期待出場時間

  -- 整合化後の試投数（カウント。期待値なので REAL）
  pred_fg2a REAL NOT NULL CHECK (pred_fg2a >= 0),
  pred_fg3a REAL NOT NULL CHECK (pred_fg3a >= 0),
  pred_fta  REAL NOT NULL CHECK (pred_fta  >= 0),

  -- 整合化後の成功率（0〜1）。成功数はこれと試投数の積で導出する
  pred_fg2_pct REAL NOT NULL CHECK (pred_fg2_pct BETWEEN 0 AND 1),
  pred_fg3_pct REAL NOT NULL CHECK (pred_fg3_pct BETWEEN 0 AND 1),
  pred_ft_pct  REAL NOT NULL CHECK (pred_ft_pct  BETWEEN 0 AND 1),

  -- その他のカウント
  pred_oreb REAL NOT NULL CHECK (pred_oreb >= 0),
  pred_dreb REAL NOT NULL CHECK (pred_dreb >= 0),
  pred_ast  REAL NOT NULL CHECK (pred_ast  >= 0),
  pred_tov  REAL NOT NULL CHECK (pred_tov  >= 0),
  pred_stl  REAL NOT NULL CHECK (pred_stl  >= 0),
  pred_blk  REAL NOT NULL CHECK (pred_blk  >= 0),
  pred_pf   REAL NOT NULL CHECK (pred_pf   >= 0),
  pred_fd   REAL NOT NULL CHECK (pred_fd   >= 0),

  -- 誤差の目安（当該選手の直近N試合の絶対誤差の中央値）。主要4項目のみ
  err_minutes REAL, err_pts REAL, err_reb REAL, err_ast REAL,

  is_provisional INTEGER NOT NULL DEFAULT 1,
  is_final       INTEGER NOT NULL DEFAULT 0,
  is_active      INTEGER NOT NULL DEFAULT 1,
  UNIQUE (game_id, player_id, model_version, revision)
);
CREATE UNIQUE INDEX uq_ppred_active ON player_predictions(game_id, player_id) WHERE is_active = 1;
```

**成功数（`pred_fg2m` 等）の列を持たない。** 旧版は成功数と試投数の両方を列に持ち、`pred_fg2m <= pred_fg2a` を CHECK で守っていた。この構造では整合化の各ステップで両者を別々にスケールする必要があり、**1.0 に張り付いた成功率を持つ選手が出た時点で合計を目標に合わせられなくなる**（実測で失敗ケースの73%が FT で発生）。試投数と成功率の組で持てば、成功率をロジット空間でシフトするだけで制約が自動的に満たされ、CHECK による事後防御そのものが不要になる。

**`pred_pts` / `pred_fgm` / `pred_fga` / `pred_reb` の列も持たない。** すべて下表のとおり導出できる。列として持つと、整合化の後に恒等式が崩れても検出できなくなる。

| 導出項目 | 式 |
|---|---|
| 2FGM / 3FGM / FTM | `pred_fg2_pct × pred_fg2a` / `pred_fg3_pct × pred_fg3a` / `pred_ft_pct × pred_fta` |
| PTS | `2FGM × 2 + 3FGM × 3 + FTM` |
| FGM / FGA | `2FGM + 3FGM` / `pred_fg2a + pred_fg3a` |
| TR | `pred_oreb + pred_dreb` |
| FG%（表示用） | `FGM / FGA` |
| EFG% | `(FGM + 0.5 × 3FGM) / FGA` |
| TS% | `PTS / (2 × (FGA + 0.44 × pred_fta))` |

**保存する成功率を画面にそのまま出さない。** これは「この選手のこの試合における期待成功率」という連続量であり、表示する `FGM / FGA` とは別物である。画面では必ず分数と併記し、試投数が閾値未満なら率を出さない（5.3 PlayerStatTable）。

**＋/－ の予測列を持たない。** 1試合の個人プラスマイナスは分散が極端に大きく（MAE 9〜12点規模）、「チーム得点差予測 × 出場時間シェア」以上の情報を持たない。実績（`player_game_stats.plus_minus`）のみ表示する。

行数の見積もり: 1試合あたり最大24人、日次で7日分×最大13試合 = 約2,200行/日。D1 の書込枠（10万行/日）に対して問題ない。

```sql
CREATE TABLE prediction_reasons (
  prediction_id TEXT NOT NULL REFERENCES predictions(id),
  rank          INTEGER NOT NULL,
  group_key     TEXT NOT NULL,     -- 'TEAM_STRENGTH'|'SCHEDULE'|'PLAYER'|'VENUE'
  label_ja      TEXT NOT NULL,
  value_text    TEXT NOT NULL,
  favors        TEXT NOT NULL CHECK (favors IN ('HOME','AWAY')),
  contribution  REAL NOT NULL,     -- グループ集約したSHAP値（ログオッズ空間）
  base_value    REAL NOT NULL,     -- explainer の期待値。事後検証用
  PRIMARY KEY (prediction_id, rank)
);
```

### 1.6 評価

```sql
CREATE TABLE model_versions (
  version        TEXT PRIMARY KEY,          -- 'winner-v1.0.0'
  model_type     TEXT NOT NULL
                 CHECK (model_type IN ('WINNER','MARGIN','TOTAL','TEAM_RATE',
                                       'PLAYER_AVAIL','PLAYER_MIN','PLAYER_RATE')),
  target         TEXT NOT NULL DEFAULT '',  -- *_RATE のみ: 'fg2a'|'fg2_pct'|... の14種
  league         TEXT NOT NULL DEFAULT 'PREMIER',
  win_prob_source TEXT CHECK (win_prob_source IN ('WINNER','MARGIN')),
                                             -- WINNER/MARGIN のみ。採用した勝率導出経路
  margin_sigma   REAL,                       -- MARGIN のみ。P(home)=Φ(margin/σ) の σ
  algo           TEXT NOT NULL,
  trained_at     TEXT NOT NULL,
  train_rows     INTEGER NOT NULL,
  train_range    TEXT NOT NULL,
  eval_window    TEXT NOT NULL,             -- 比較の公平性のため固定した評価対象
  params         TEXT NOT NULL,             -- JSON。seed 系を必ず含める
  feature_list   TEXT NOT NULL,             -- JSON配列
  feature_null_rates TEXT,                  -- JSON。欠損率30%超の検出用
  cv_accuracy    REAL,
  cv_brier       REAL,
  cv_logloss     REAL,
  cv_ece         REAL,                      -- 等頻度10ビン
  baseline_home_accuracy REAL,
  baseline_elo_brier     REAL,              -- Elo単体ロジスティック回帰
  artifact_text  TEXT,                      -- LightGBM の save_model() 出力
  artifact_sha256 TEXT,
  calibrator     TEXT,                      -- 較正器のパラメータ。使う場合のみ
  is_active      INTEGER NOT NULL DEFAULT 0 CHECK (is_active IN (0,1)),
  notes          TEXT
);
-- 同一 (model_type, target, league) で有効モデルは常に1本
CREATE UNIQUE INDEX uq_model_active
  ON model_versions(model_type, target, league) WHERE is_active = 1;
```

**モデルバイナリは D1 に格納する。** GitHub Actions の artifact はデフォルト90日で失効し、別ワークフローから取得するには run-id 解決が必要なため、日次推論の依存先として成立しない。LightGBM の `save_model()` はテキスト形式で数百KB〜数MBに収まり、5GB のストレージ枠に対して影響は無視できる。

**ただし D1 の1行上限は 2,000,000 バイトである。** 書式からの推定では `num_leaves=7` / 木300〜800本で 0.27〜0.70MB に収まるが、木が2,000本まで伸びると 1.75MB（上限の88%）、`num_leaves=15` / 1,500本では 2.56MB で上限を超える。

```sql
-- 登録時のサイズガード（アプリ層でも同じ判定を行い、二重化する）
CREATE TRIGGER trg_model_artifact_size
BEFORE INSERT ON model_versions
WHEN NEW.artifact_text IS NOT NULL AND length(NEW.artifact_text) > 1572864  -- 1.5 MiB
BEGIN
  SELECT RAISE(ABORT, 'artifact_text exceeds 1.5MB limit');
END;
```

上限を超えた場合は**登録を拒否し、現行モデルを継続使用する**（学習ジョブは `PARTIAL` + exit 1 で終える）。`num_boost_round` の上限は 1,200 とし、`num_leaves` を 7 から増やす変更は artifact サイズの再測定とセットでのみ許可する。実サイズは Phase 0（P0-12）で測定する。

**登録と読み出しは Workers 経由で行う。** 学習ジョブは `POST /internal/models` で登録し（アプリ層でも同じ1.5MB判定を行い、トリガと二重化する）、日次推論は `GET /internal/models/active` で有効モデル一式を取得する。バッチが D1 を直接読み書きすることも、D1 REST API を叩くこともしない（3.4）。

`uq_model_active` があれば、ロールバック時に「先に 0 にしてから 1 にする」順序でのみ成功し、有効モデル2本という状態が構造的に作れなくなる。

```sql
-- 推論に使ったモデル一式の記録。予測1本は複数モデルの合成である
CREATE TABLE prediction_model_bundle (
  prediction_id TEXT NOT NULL REFERENCES predictions(id),
  model_type    TEXT NOT NULL,
  target        TEXT NOT NULL DEFAULT '',
  model_version TEXT NOT NULL REFERENCES model_versions(version),
  PRIMARY KEY (prediction_id, model_type, target)
);
```

**`predictions.model_version` だけでは出自を復元できない。** 1本の予測は Winner / Margin / Total / TeamRate×14 / PlayerAvail / PlayerMin / PlayerRate×14 の合成であり、単一の列にはチームモデルのバージョンしか書けない。個人スタッツのどのモデルで出た値かが記録されないと、「あの試合の個人予測はどのモデルか」を後から特定できず、モデル別の精度比較（`/accuracy` の byModel）も個人スタッツについては成立しない。

`predictions.model_version` は「代表バージョン（Winner または Margin のうち採用経路のもの）」として残し、全体は `prediction_model_bundle` で記録する。1予測あたりの行数は **WINNER / MARGIN / TOTAL / PLAYER_AVAIL / PLAYER_MIN の5 + TEAM_RATE 14 + PLAYER_RATE 14 = 最大33行**で、日次で最大13試合 × 33 = 約429行であり書込枠に対して問題ない。

```sql
CREATE TABLE prediction_results (
  prediction_id      TEXT PRIMARY KEY REFERENCES predictions(id),
  game_id            TEXT NOT NULL REFERENCES games(id),
  season_id          TEXT NOT NULL,             -- 非正規化
  model_version      TEXT NOT NULL,
  home_win_prob      REAL NOT NULL,             -- 非正規化。calibration 用
  prob_bucket        INTEGER NOT NULL,          -- 0-9。GROUP BY 用
  outcome            TEXT NOT NULL
                     CHECK (outcome IN ('WIN','LOSS','VOID')),
  predicted_home_win INTEGER,
  actual_home_win    INTEGER,                   -- VOID のとき NULL
  is_correct         INTEGER,
  brier              REAL,
  score_mae          REAL,
  was_provisional    INTEGER NOT NULL,
  evaluated_at       TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_predres_season ON prediction_results(model_version, season_id);
CREATE INDEX idx_predres_calib  ON prediction_results(model_version, prob_bucket);
```

**中止・延期試合は `outcome = 'VOID'` とし、的中率の母数から除外する。** 旧版は `actual_home_win INTEGER NOT NULL` だったため、中止試合が1件出ると NOT NULL 制約違反でバッチが毎日失敗し続け、取り込みが完全停止していた。

```sql
-- 日次で洗い替える集計層
CREATE TABLE accuracy_summary (
  scope         TEXT NOT NULL
                CHECK (scope IN ('OVERALL','SEASON','MODEL','BUCKET','PROVISIONAL')),
  scope_key     TEXT NOT NULL,
  model_version TEXT NOT NULL DEFAULT '',   -- モデル横断の集計では空文字。NULL にしない
  n             INTEGER NOT NULL,
  accuracy      REAL NOT NULL,
  brier         REAL NOT NULL,
  actual_rate   REAL,            -- calibration 用
  baseline_accuracy REAL,
  updated_at    TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (scope, scope_key, model_version)
);
```

**`model_version` を NULL 許容にしない。** SQLite は主キー列の NULL 重複を許すため、`('OVERALL','all',NULL)` のような行が何行でも入り、`ON CONFLICT(scope, scope_key, model_version)` も衝突を検出しない（実測で確認済み）。モデル横断の集計（`OVERALL` / `SEASON` / `BUCKET` / `PROVISIONAL`）では**空文字を入れる**。`model_versions.target TEXT NOT NULL DEFAULT ''` と同じ書き方である。

`scope` にも CHECK を付ける。列挙値に CHECK を置くのは本設計の原則であり、集計層だけ例外にしない。

### 1.7 運用

```sql
CREATE TABLE ingestion_logs (
  id            TEXT PRIMARY KEY,
  job           TEXT NOT NULL,
  started_at    TEXT NOT NULL,
  finished_at   TEXT,
  status        TEXT NOT NULL
                CHECK (status IN ('RUNNING','SUCCESS','PARTIAL','FAILED','ABORTED')),
  rows_affected INTEGER,
  d1_rows_read  INTEGER,                      -- 無料枠の監視用
  error_type    TEXT,                         -- 例外の型名のみ
  error_message TEXT                          -- 自前の短いメッセージのみ
);
CREATE INDEX idx_inglog_job ON ingestion_logs(job, started_at DESC);
```

**例外オブジェクトをそのまま保存しない。** D1 REST API の URL にはアカウントIDとDB IDが含まれるため、`str(e)` を保存して `/health` で公開すると、それらが公開APIから漏れる。型名と自前メッセージに限定し、URLを残す場合はホスト名とパスのみでクエリ文字列とID部分をマスクする。

### 1.8 トリガ

```sql
CREATE TRIGGER trg_predictions_final_immutable
BEFORE UPDATE ON predictions
WHEN OLD.is_final = 1
BEGIN
  SELECT RAISE(ABORT, 'final prediction is immutable');
END;

CREATE TRIGGER trg_predictions_final_nodelete
BEFORE DELETE ON predictions
WHEN OLD.is_final = 1
BEGIN
  SELECT RAISE(ABORT, 'final prediction cannot be deleted');
END;

-- 子テーブルにも例外なく同じ凍結を適用する
CREATE TRIGGER trg_ppred_final_immutable
BEFORE UPDATE ON player_predictions
WHEN OLD.is_final = 1
BEGIN
  SELECT RAISE(ABORT, 'final player prediction is immutable');
END;

CREATE TRIGGER trg_ppred_final_nodelete
BEFORE DELETE ON player_predictions
WHEN OLD.is_final = 1
BEGIN
  SELECT RAISE(ABORT, 'final player prediction cannot be deleted');
END;

-- player_predictions は自身の is_final に加えて、親の確定でも守る。
-- 自テーブルの列だけで守ると、freeze が子への UPDATE を取りこぼした場合に
-- 「親は確定済みなのに子は書き換えられる」状態が残る。
CREATE TRIGGER trg_ppred_parent_final_immutable
BEFORE UPDATE ON player_predictions
WHEN (SELECT is_final FROM predictions WHERE id = OLD.prediction_id) = 1
BEGIN
  SELECT RAISE(ABORT, 'player predictions of a final prediction are immutable');
END;

CREATE TRIGGER trg_ppred_parent_final_nodelete
BEFORE DELETE ON player_predictions
WHEN (SELECT is_final FROM predictions WHERE id = OLD.prediction_id) = 1
BEGIN
  SELECT RAISE(ABORT, 'player predictions of a final prediction cannot be deleted');
END;

-- prediction_reasons は is_final を持たないため、親を参照して判定する
CREATE TRIGGER trg_reasons_final_immutable
BEFORE UPDATE ON prediction_reasons
WHEN (SELECT is_final FROM predictions WHERE id = OLD.prediction_id) = 1
BEGIN
  SELECT RAISE(ABORT, 'reasons of a final prediction are immutable');
END;

CREATE TRIGGER trg_reasons_final_nodelete
BEFORE DELETE ON prediction_reasons
WHEN (SELECT is_final FROM predictions WHERE id = OLD.prediction_id) = 1
BEGIN
  SELECT RAISE(ABORT, 'reasons of a final prediction cannot be deleted');
END;

CREATE TRIGGER trg_team_targets_final_immutable
BEFORE UPDATE ON prediction_team_targets
WHEN (SELECT is_final FROM predictions WHERE id = OLD.prediction_id) = 1
BEGIN
  SELECT RAISE(ABORT, 'team targets of a final prediction are immutable');
END;

CREATE TRIGGER trg_team_targets_final_nodelete
BEFORE DELETE ON prediction_team_targets
WHEN (SELECT is_final FROM predictions WHERE id = OLD.prediction_id) = 1
BEGIN
  SELECT RAISE(ABORT, 'team targets of a final prediction cannot be deleted');
END;

CREATE TRIGGER trg_bundle_final_immutable
BEFORE UPDATE ON prediction_model_bundle
WHEN (SELECT is_final FROM predictions WHERE id = OLD.prediction_id) = 1
BEGIN
  SELECT RAISE(ABORT, 'model bundle of a final prediction is immutable');
END;

CREATE TRIGGER trg_bundle_final_nodelete
BEFORE DELETE ON prediction_model_bundle
WHEN (SELECT is_final FROM predictions WHERE id = OLD.prediction_id) = 1
BEGIN
  SELECT RAISE(ABORT, 'model bundle of a final prediction cannot be deleted');
END;
```

**凍結に例外を設けない。** 旧版は `predictions` にのみトリガを置いていたが、それでは「勝率は変えられないが、個人スタッツ・根拠・目標値・モデル構成は後から書き換えられる」状態になる。根拠は「なぜその予測になったか」の記録であり、試合後に差し替えられるなら予測の不変性という主張そのものが成立しない。

表示上の誤り（ラベルの誤字など）が見つかった場合も、**過去の行は修正しない**。以後の生成ロジックのみを直す。`is_final = 1` を立てる freeze 操作自体は `predictions` の UPDATE であり、この2つのトリガは `OLD.is_final = 1` を条件にしているため、`0 → 1` の遷移は通る（`1 → 1` は通らない）。

**子テーブルはすべて「親の確定」で凍結される。** `is_final` 列を持つのは `predictions` と `player_predictions` の2つだけだが、**4つの子テーブルすべてに親参照トリガを置く**。したがって freeze が UPDATE するのは `predictions` と `player_predictions` の2つで、残り3つは何もしなくても凍結される。

| テーブル | `is_final` 列 | 守るトリガ |
|---|---|---|
| `predictions` | あり | `trg_predictions_final_*`（自テーブル参照） |
| `player_predictions` | あり | `trg_ppred_final_*`（自テーブル参照）**＋ `trg_ppred_parent_final_*`（親参照）** |
| `prediction_reasons` | なし | `trg_reasons_final_*`（親参照） |
| `prediction_team_targets` | なし | `trg_team_targets_final_*`（親参照） |
| `prediction_model_bundle` | なし | `trg_bundle_final_*`（親参照） |

**`player_predictions` にだけ二重にかける理由。** 自テーブルの `is_final` だけで守ると、`finalize` が子への UPDATE を取りこぼした場合に「**親は確定済みなのに子は書き換えられる**」状態が残る。DBトリガは「アプリ層の規律だけに頼らない」ための関門であり、`finalize` の実装ミスで無効化されるなら関門として機能していない。他の3つは列を持たないため、親参照トリガだけで同じ保護になる。

**freeze の `batch()` 内では、子（`player_predictions`）→ 親（`predictions`）の順に UPDATE する。** `trg_ppred_parent_final_*` があるため、**親を先に `is_final = 1` にすると子の `0 → 1` が拒否され、freeze 自体が失敗する**。順序は任意ではなく必須である。

```sql
-- 単一 batch() 内。この順序で固定する
UPDATE player_predictions SET is_final = 1 WHERE prediction_id = ? AND is_final = 0;
UPDATE predictions        SET is_final = 1 WHERE id = ?            AND is_final = 0;
```

`AND is_final = 0` を付けるのは冪等性のため。2回目の実行では0行が一致し、トリガも発火しない（`test_finalize_is_idempotent`）。

---

## 2. 特徴量定義

### 2.1 共通規約

```python
def build_features(game_id: str, as_of: datetime, ds: Dataset) -> dict[str, float]:
    """as_of 時点で確定している情報のみから特徴量を生成する。

    as_of  : 参照してよい情報の上限時刻（= games.tipoff_at）
    ds     : batch/snapshot/*.parquet から読み込んだメモリ内データセット
             （D1 は読まない。CLAUDE.md 絶対ルール3）

    絞り込みは finished_at で行う。tipoff_at で絞ると、17:05開始・19:05終了の
    試合が 19:05開始の試合の確定情報として混入する。
    """
```

すべての特徴量関数は以下を守る。

1. 引数 `as_of` を必ず受け取る
2. 参照条件に `finished_at <= as_of` を含める（`tipoff_at` ではない）
3. 対象試合自身の `team_game_stats` / `player_game_stats` / `games.attendance` を参照しない
4. `status = 'SCHEDULED'` の試合を集計に含めない
5. 欠損時は None を返し、呼び出し側で既定値へ変換する（関数内で0埋めしない）
6. **チーム所属の判定は `players` の現在の所属ではなく、`player_game_stats.club_id`（実績）または `player_seasons`（当該シーズン断面）を使う**
7. **`competition` で絞らない。** `REGULAR` と `PLAYOFF` の両方を集計に含める

規約7の理由: DB に入っているのはこの2区分だけであり（1.3）、どちらもクラブ同士の公式戦である。プレーオフを除外すると、**プレーオフ期間中だけ当季勝率と得失点差が止まったまま Elo だけが動く**不連続が生じる。区分で絞る必要が出た場合は、この規約を先に書き換える（`team_games.competition` は絞れるように持たせてあるが、v1 では絞らない）。

規約6を守らないと、移籍が発生した瞬間に「そのチームの過去の主力」が誤る。移籍した選手が過去の所属チームから消え、新チームに過去の出場時間ごと移動する。`as_of` 規約には違反しないため、テストでも検出されない静かなバグになる。

**D1 を逐次クエリしない。** 入口でテーブル単位に一括エクスポートし、以降はメモリ上で処理する。

### 2.2 チーム勝敗モデルの特徴量

命名規則: `{カテゴリ}_{内容}_{対象}`。対象は `home` / `away` / `diff`。相関を減らすため、可能な限り `diff` に集約する。

#### 採用

| キー | 定義 | 欠損時 |
|---|---|---|
| `elo_diff` | Elo の差 | 0 |
| `elo_home` / `elo_away` | `team_ratings` の直前スナップショット | 1500 |
| `winrate_l5_diff` / `winrate_l10_diff` | 直近勝率の差 | 0 |
| `winrate_season_diff` | 当季通算勝率の差（縮約後） | 0 |
| `margin_l5_diff` / `margin_season_diff` | 平均得失点差の差 | 0 |
| `series_game_no` | 同一カード連戦の何戦目か | 1 |
| `prev_result_diff` | 前戦の勝敗の差 | 0 |
| `prev_margin_diff` | 前戦の得失点差の差 | 0 |
| `rest_days_diff` | 休養日数の差。**「中N日」の N**（要件 6.2 の #12 の語彙。暦日の差ではなく、試合の間に空いた日数。土日2連戦の日曜は0）。14でクリップし、シーズン跨ぎもこの値に収める | 0 |
| `away_streak_away` | アウェイ側の連続アウェイ試合数 | 0 |
| `minutes_lost_diff` | 欠場者の直近平均出場時間の合計の差 | 0 |
| `top_players_out_diff` | 直近出場時間上位5名の欠場者数の差 | 0 |
| `entry_is_official` | 両チームのエントリーが公式確定なら1 | 0 |

`is_home` は持たない（ホーム視点固定学習のため常に1の定数列になる）。

#### 検証

| キー | 定義 | 前提 |
|---|---|---|
| `ortg_diff` / `drtg_diff` | 100ポゼッションあたり得失点の差 | `possessions` が算出可能 |
| `pace_home` / `pace_away` | 1試合あたりポゼッション数 | 同上 |
| `efg_diff` `tov_rate_diff` `oreb_rate_diff` `ft_rate_diff` | Four Factors の差 | ボックススコア取得可 |
| `sos_diff` | 対戦相手の平均 Elo の差 | — |
| `is_primary_venue` | メイン会場なら1 | 会場マスタ |
| `travel_km_diff` | 前戦会場からの直線距離の差 | 緯度経度 |
| `rank_diff` | 順位差 | — |
| `playoff_race_diff` | プレーオフ圏との勝率差の差 | — |
| `season_progress` | 当季の消化率 0-1 | — |
| `day_of_week` / `tipoff_hour` | 曜日・開始時刻 | — |
| `minutes_concentration_diff` | 出場時間のハーフィンダール指数の差 | — |
| `foreign_count_diff` / `naturalized_diff` | ロスター構成の差 | `player_seasons` |
| `venue_capacity` | 収容人数（当時の値） | `venue_revisions` |
| `attendance_avg_home` / `attendance_rate_home` / `attendance_avg_venue` | 過去試合からの平均動員 | 観客制限期間を除外 |

**特徴量を一度に全部入れない。** まず `elo_diff` と日程系と欠場系の10〜15列で基礎モデルを作り、1つずつ追加して walk-forward Brier の改善を測る。8,000行に対して70列は、初期 fold（1,600行）では明確に過剰である。

### 2.3 個人モデルの特徴量

| キー | 定義 | 対象モデル |
|---|---|---|
| `minutes_l5_player` / `minutes_l10_player` | 直近の平均出場時間 | Avail / Minutes |
| `games_played_ratio_l10` | 直近10試合の出場率 | Avail |
| `entry_status` | エントリー情報（公式/推定） | Avail |
| `days_since_last_played` | 最終出場からの日数 | Avail |
| `{count}_per_min_l10` | **カウント11項目それぞれの**単位時間あたり実績（直近10試合） | Rates |
| `{count}_per_min_season` | 同 当季平均 | Rates |
| `{pct}_shrunk_l10` | **成功率3項目**のシュリンク済み実績（`(made + k×prior) / (att + k)`） | Rates |
| `{pct}_shrunk_season` | 同 当季 | Rates |
| `{att}_l10` | 各成功率に対応する試投数（成功率モデルの学習重み） | Rates |
| `usage_l10` | 使用率の近似 | Rates |
| `pred_minutes` | Minutes モデルの出力 | Rates |
| `team_minutes_lost` | 同僚の欠場合計 | Minutes / Rates |
| `opponent_drtg` / `opponent_pace` | 相手の守備力とペース | Rates |
| `opponent_{stat}_allowed_l10` | 相手が直近10試合で許した当該項目の水準 | Rates |
| `is_starter_l5` | 直近のスターター率 | Minutes |
| `position` | ポジション（OR/BS はビッグマンに偏る） | Rates |

`{count}` は `fg2a, fg3a, fta, oreb, dreb, ast, tov, stl, blk, pf, fd` の11項目。`{pct}` は `fg2_pct, fg3_pct, ft_pct` の3項目。合わせて予測対象14項目。

**カウントは per-minute で学習する。** カウントをそのまま学習すると出場時間の分散に支配され、出場時間の予測誤差がそのまま全項目に伝播する。レートで学習し、整合化の段階で出場時間を掛ける。

**成功率は per-minute にしない。** 率は時間当たりの量ではないため per-minute 化は無意味である。目的変数はシュリンク済み成功率、学習重みは試投数とし、出力は `clip(0.01, 0.99)` に収める（ロジット変換の定義域確保）。特徴量の側も `{stat}_per_min` ではなくシュリンク済み率と試投数を与える。

### 2.4 個人スタッツの整合化

選手ごとに独立予測した値をそのまま使わない。**5人の得点合計がチームの予想スコアと一致せず、同じ画面に矛盾した数字が並ぶ。**

#### 前提: 予測の持ち方

| 予測する | 導出する |
|---|---|
| `2FGA` `3FGA` `FTA`（試投数、カウント） | `2FGM = 2FG% × 2FGA` など |
| `2FG%` `3FG%` `FT%`（成功率、0〜1） | `PTS = 2FGM×2 + 3FGM×3 + FTM` |
| `OR` `DR` `AS` `TO` `ST` `BS` `F` `FD`（カウント） | `TR = OR + DR`、`FGA = 2FGA + 3FGA` |

**チーム側の目標値も同じ構造で持つ**（`prediction_team_targets`、1.5）。各項目を独立に生成すると、約2%の確率で「FT成功数 > FT試投数」のような実行不能な目標が生まれ、整合化が原理的に成立しない（検証済み）。

#### 前段: チーム目標の整合化

**TeamRates の生出力をチーム目標にしない。** TeamRates（試投数・成功率の回帰14本）と Margin / Total は別モデルであり、TeamRates から導出した得点が予想スコアと一致する保証がない。一致しないまま選手側へ渡すと「選手の合計＝チーム目標」は満たされるが「チーム目標＝画面に出る予想スコア」が崩れ、**矛盾の位置が一段ずれるだけ**になる。

**予想スコアを正とし、チームの成功率3項目を共通のロジットシフトで寄せる。** 選手側と同じ `shift_to_target()` の考え方を、得点の恒等式に対して1回だけ適用する。

```python
def reconcile_team_targets(rates, pred_score):
    """TeamRates の出力を予想スコア（Margin/Total 由来）に整合させる。

    試投数は動かさず、成功率3項目に共通のシフト量 d を入れる。

        pts(d) = 2*sigmoid(l2+d)*A2 + 3*sigmoid(l3+d)*A3 + 1*sigmoid(lf+d)*Af

    sigmoid は単調増加、係数（2/3/1）と試投数 A は非負であるため pts(d) は d について
    単調増加であり、pts(d) = pred_score の解は一意に存在する。反復は不要。
    値域は (0, 2*A2 + 3*A3 + Af) で、この外の目標は到達不能。
    """
    A = {a: float(rates[a]) for a in ATTEMPTS}          # fg2a, fg3a, fta
    W = {"fg2a": 2.0, "fg3a": 3.0, "fta": 1.0}          # 得点の重み
    lo = {p: logit(np.clip(rates[p], EPS, 1 - EPS)) for p, _ in PCTS}

    def pts(d):
        return sum(W[a] * sigmoid(lo[p] + d) * A[a] for p, a in PCTS)

    if pts(-50) > pred_score or pts(50) < pred_score:
        raise InfeasibleTargetError(pred_score, sum(W[a] * A[a] for a in A))

    d = brentq(lambda x: pts(x) - pred_score, -50, 50)
    out = dict(rates)
    for p, _ in PCTS:
        out[p] = float(sigmoid(lo[p] + d))              # [0,1] を出ない。クリップ不要
    return out
```

- **試投数は動かさない。** ペースと配分の予測をそのまま残し、得点の帳尻は成功率だけで合わせる
- **クリップを使わない。** ロジット空間で動かすため `[0,1]` を原理的に出ない
- **反復しない。** 1回で `pts(d) = pred_score` が厳密に成立する
- カウント8項目（`oreb` `dreb` `ast` `tov` `stl` `blk` `pf` `fd`）は得点の恒等式に関与しないため、この前段では触らない
- 到達不能なら `InfeasibleTargetError` を投げる。無言でクリップしない（後述の「例外時の扱い」と同じ）

ホーム・アウェイそれぞれについて独立に解く（`pred_score` は `(total + margin) / 2` と `(total - margin) / 2`）。この前段を通した値だけを `prediction_team_targets` に保存し、選手側の整合化へ渡す。

> この前段は `verification/03_reconciliation_logit.py` と同型の構造であり、同スクリプトにチーム目標のケースを追加して収束と制約充足を再検証できる。

#### アルゴリズム

```python
import numpy as np
from scipy.optimize import brentq

EPS = 1e-4
ATTEMPTS = ('fg2a', 'fg3a', 'fta')
COUNTS   = ('oreb', 'dreb', 'ast', 'tov', 'stl', 'blk', 'pf', 'fd')
PCTS     = (('fg2_pct', 'fg2a'), ('fg3_pct', 'fg3a'), ('ft_pct', 'fta'))

def logit(p):    return np.log(p / (1 - p))
def sigmoid(z):  return 1 / (1 + np.exp(-z))

def shift_to_target(pct, att, target_made):
    """成功率をロジット空間で一律シフトし、Σ(pct_i × att_i) = target_made を満たす。

    ロジットは (0,1) を (-∞,∞) に写すため、どれだけシフトしても [0,1] を出ない。
    f(d) は d について単調増加なので、解は一意に存在する。クリップは不要。
    """
    if att.sum() <= 0:
        return pct
    lo = logit(np.clip(pct, EPS, 1 - EPS))
    f  = lambda d: float((sigmoid(lo + d) * att).sum()) - target_made
    if f(-50) > 0 or f(50) < 0:       # 目標が到達不能（= チーム目標が不正）
        raise InfeasibleTargetError(target_made, float(att.sum()))
    return sigmoid(lo + brentq(f, -50, 50))

def reconcile(players, target, overtime=0, iters=3):
    """選手予測をチームレベル目標に整合させる。"""
    # 1. 期待出場時間
    m = np.array([p.avail_prob * p.minutes_if_plays for p in players])

    # 2. 総出場時間を正規化（5人 × 40分。延長は +25分/回。予測時は延長を仮定しない）
    m = m * (200 + 25 * overtime) / m.sum()

    # 3. レート × 出場時間 → カウント
    x = {s: np.array([p.rate[s] for p in players]) * m
         for s in ATTEMPTS + COUNTS}
    # 成功率はレートではなくそのまま使う
    pct = {k: np.clip(np.array([p.pct[k] for p in players]), EPS, 1 - EPS)
           for k, _ in PCTS}

    for _ in range(iters):
        # 4. 試投数・その他カウントをチーム目標へ比例スケール
        for s in ATTEMPTS + COUNTS:
            tot = x[s].sum()
            if tot > 0:
                x[s] = x[s] * target[s] / tot

        # 5. 成功率をチーム目標へロジット空間シフト
        for k, a in PCTS:
            target_made = target[k] * target[a]        # チームの成功数
            pct[k] = shift_to_target(pct[k], x[a], target_made)

    # 6. 得点・成功数は導出（独立に持たない）
    made = {k: pct[k] * x[a] for k, a in PCTS}
    pts  = made['fg2_pct'] * 2 + made['fg3_pct'] * 3 + made['ft_pct']
    return m, x, pct, pts
```

#### 禁止事項

**手順5でクリップを使ってはならない。** 成功率を `[0, 1]` の範囲で一律に掛け算すると、1.0 に張り付いた選手が出た時点で合計を目標に合わせられなくなり、反復が振動する。実測では失敗ケースの **73% が FT 成功率**で発生した（`pf` のクリップは無関係であることを、外して再測定して確認済み）。

**`pf` の 5.0 クリップも行わない。** 期待値としての反則数が5を超えることは実運用上ほぼないうえ、クリップは整合を壊す。値域は表示側で扱う。

#### 収束と許容誤差（実測）

1,500ケースのランダム入力で測定した結果。

| 反復 | 項目誤差 中央値 | 99%点 | 最大 | 得点誤差 最大 |
|---|---|---|---|---|
| 1 | 0.0000% | 3.39% | 10.73% | 0.00000 |
| 2 | 0.0000% | 0.94% | 3.51% | 0.00000 |
| 3 | 0.0000% | 0.27% | 1.87% | 0.00000 |

**反復回数は3とする。** 得点誤差は反復1回でも厳密にゼロになる（手順6が恒等式による導出であり、手順5で成功数が目標に厳密一致するため）。残る誤差は手順4と手順5の相互作用による試投数側のずれで、3回で99%点が 0.27% に収まる。

許容誤差は**得点 ±0.5点、その他 ±2%、制約違反0件**とする（受け入れ基準 A-13）。

旧版の「試投数と成功数を別々に比例スケールし、2回反復」方式は収束しない。同一条件での実測は項目誤差の最大 24.7%、得点誤差 4.42点だった。

> この手順は `verification/03_reconciliation_logit.py` で再検証できる。

#### 例外時の扱い

`InfeasibleTargetError` はチーム目標が不正（`成功数 > 試投数` など）であることを意味し、**整合化の失敗ではなく上流の欠陥である**。当該試合の個人スタッツ予測を破棄し、チーム予測（勝率・予想スコア）のみを保存する。画面には「この試合の個人スタッツ予測は算出できませんでした」と表示する。無言でクリップして辻褄を合わせない。

### 2.5 Elo の算出

```
K, HOME_ADVANTAGE, SEASON_REGRESSION は walk-forward の Brier を
目的関数としてグリッド探索する。以下は探索の初期値。

K ∈ {12, 16, 20, 24, 32}                 初期 20
HOME_ADVANTAGE ∈ {40, 55, 70, 85}        初期 70
SEASON_REGRESSION ∈ {0.5, 0.65, 0.75, 0.85} 初期 0.65
```

```python
# spectator_restricted は 1 / 0 / NULL（判定不能）の3値。
# NULL は「制限されていた証拠がない」であって「制限されていた」ではないため、
# 通常のホームアドバンテージを使う（1 のときだけ 0 にする）。
ha = 0 if game.spectator_restricted == 1 else HOME_ADVANTAGE

expected_home = 1 / (1 + 10 ** ((elo_away - elo_home - ha) / 400))
actual_home   = 1 if home_score > away_score else 0

margin = abs(home_score - away_score)
multiplier = log(margin + 1) * (2.2 / (0.001 * abs(elo_home - elo_away) + 2.2))

elo_home += K * multiplier * (actual_home - expected_home)
elo_away -= K * multiplier * (actual_home - expected_home)
```

**観客制限期間はホームアドバンテージを0にする。** 2020-21 / 2021-22 の約1,500〜2,000試合が無観客または収容制限下にあり、この期間はホームアドバンテージ自体が消失することが各国リーグで確認されている。固定値のまま更新すると Elo が系統的に歪む。

`HOME_ADVANTAGE` の検算: Elo 差60点は勝率 58.6%、ホーム勝率60%に対応するのは 70.5点。旧版の60はオーダーは合うが実測値ではない。

**シーズン間の回帰**

```
elo_new_season = 1500 + (elo_prev_season_end - 1500) * SEASON_REGRESSION
```

Bリーグは NBA よりロスター変動が大きいため、NBA 標準の 0.75 より強い回帰（0.5〜0.65）が適切な可能性が高い。実データで探索する。

**昇格クラブ・復帰クラブ**: 1500（＝旧B1平均）を与えない。前季 PREMIER 下位層の水準（1400前後）を初期値にする。`games_played < 10` の間は Elo の信頼度を特徴量として渡す。

**「前季にトップリーグの試合がないクラブ」はすべてこの扱いにする。** 空白が1シーズンでも8シーズンでも同じである。過去にトップリーグにいた Elo が残っていても**持ち越さず捨てる**。

理由は2つある。第一に、`SEASON_REGRESSION` を空白シーズン数だけ適用すると偏差が `R^N` に縮み（R=0.65 なら N=8 で 3.2%）、実質 1500 = トップリーグ平均になる。**二部に8シーズンいたクラブを平均と評価することになり、過大評価の方向に倒れる。** 第二に、その期間のロスターは総入れ替えになっている。

**空白シーズン数の閾値を設けない。** 11シーズンで空白を挟んだ復帰は7件（空白1年が4件、2年・5年・8年が各1件）しかなく、閾値を推定する検出力がない。閾値を置けば「実装しながら決めた値」が1つ増えるだけである。

**B.ONE / B2 の Elo は存在しない。** 取得対象はトップリーグ（PREMIER と前身の B1）のみであり（要件 5.4）、下位リーグの試合を取り込まないため Elo も計算されない。旧版が書いていた「B.ONE 時代の Elo があればリーグ間オフセットを推定して平行移動する」という分岐は**永久に発火しないため削除した**。

2026-27 で該当するのは `718` 神戸（B1 在籍は 2017-18 のみ、以後8シーズン不在）と `716` 信州（2シーズン不在）である。**B.PREMIER の構成は昇降格の結果ではない**ため、この状態が実際に発生する（`verification/RESULTS.md`）。

**チーム別ホームアドバンテージ**: 素朴な推定は過学習する。1チームあたりのホーム試合は年約30試合で、ホーム勝率の標準誤差は約9ポイント、見かけの差の大半はノイズである。階層モデル（部分プーリング）にする。

```
HA_team = HA_league + shrink * (HA_team_observed - HA_league)
shrink  = n_home / (n_home + k)     # k は経験ベイズで推定。典型 50〜100
```

### 2.6 当季集計の縮約

```
winrate_shrunk = (w + k * prior) / (n + k)
```

k = 10、`prior` は前季勝率をシーズン間回帰させた値。「重みを下げる」という記述ではなく、この式を使う。

### 2.7 根拠のグループ集約

SHAP 値は個別特徴ではなく、以下のグループに合算して表示する。

| `group_key` | 含む特徴量 | 表示名 |
|---|---|---|
| `TEAM_STRENGTH` | Elo、勝率、得失点差、SOS、ORtg/DRtg、Four Factors | チーム力 |
| `SCHEDULE` | 連戦、前戦結果、休養、連続アウェイ、移動距離 | 日程・疲労 |
| `PLAYER` | 欠場、出場時間喪失、出場時間集中度、ロスター構成 | 選手 |
| `VENUE` | メイン会場、収容人数、動員 | 会場 |

個別特徴のまま表示すると、相互相関0.6〜0.9のためSHAP値が分散し、ほぼ同じ条件の2試合で「チーム力の差」と「直近5試合の勝率」が入れ替わって表示される。

**表示ルール**

- 数値（`contribution`）を画面に出さない。方向と相対的な強さ（バーの長さ、3〜4段階）で示す
- `%` は勝率専用に予約する。SHAP値はログオッズ空間であり確率への加算ではない
- `favors` で有利な側を日本語で明示する
- 生の特徴量名（`elo_diff` 等）を画面に出さない
- **選手個人の能力・資質への評価を含む表現を使わない**（可: 「出場時間の減少」/ 不可: 「不調」「衰え」）
- 動員が上位に来ても因果として表示しない（強さの代理変数であるため）

表示例:

| group | label_ja | value_text | favors |
|---|---|---|---|
| TEAM_STRENGTH | チーム力の差 | ＋82ポイント | HOME |
| SCHEDULE | アウェイの休養 | 中0日（ホームは中2日） | HOME |
| PLAYER | アウェイの主力欠場 | 平均28分の選手が1名 | HOME |
| VENUE | 開催会場 | 代替アリーナ | AWAY |

冒頭に一行の要約を置く: 「ホームのチーム力が上回っていること、アウェイが2連戦の2戦目で疲労していることが、この予測の主な理由です。」

`explainer` は推論ループの外で1回だけ構築する。

---

## 3. API 仕様

ベースパス `/api/v1`。すべて JSON。

**主要導線（今日の予測・試合一覧）は API を経由しない。** バッチが書き出した静的JSON（`/data/today.json` 等）を Pages が配信する。API は動的クエリと書き込みに限定する。

### 3.1 共通

```jsonc
{ "data": { ... }, "meta": { "generatedAt": "...", "modelVersion": "...", "stale": false } }
{ "error": { "code": "NOT_FOUND", "message": "..." } }
```

| コード | HTTP |
|---|---|
| `BAD_REQUEST` | 400 |
| `UNAUTHORIZED` | 401 |
| `NOT_FOUND` | 404 |
| `ALREADY_FINAL` | 409 |
| `RATE_LIMITED` | 429 |
| `INTERNAL` | 500 |

### 3.2 入力検証

**すべてのパラメータを境界で検証し、不一致は D1 にもキャッシュにも触れず 400 で返す。**

| パラメータ | 制約 |
|---|---|
| `date` | `^\d{4}-\d{2}-\d{2}$` かつ実在日付かつ**シーズン範囲内** |
| `slug` | `^[a-z0-9-]{1,40}$` |
| `gameId` | 公式試合IDの形式 |
| `limit` | 既定20、最大100。範囲外はクランプ |

範囲外の日付を弾かないと、URL 空間が無限になりクローラの総当たりで無料枠が枯渇する。

**D1 クエリは必ず `env.DB.prepare(sql).bind(...)` を使う。** テンプレートリテラルや文字列連結でSQLを組まない。

### 3.3 公開エンドポイント

| メソッド | パス | 用途 |
|---|---|---|
| GET | `/games?date=` | 指定日の試合（シーズン範囲内のみ） |
| GET | `/games/:gameId` | 試合詳細（試合前/試合後で内容が変わる） |
| GET | `/results?date=` | 終了した試合と予測の対比 |
| GET | `/teams` `/teams/:slug` | チーム一覧・詳細 |
| GET | `/accuracy` | `accuracy_summary` から読む |
| GET | `/health` | 稼働状態と当日カウンタ |

#### GET /games/:gameId（試合前）

```jsonc
{
  "data": {
    "game": {
      "gameId": "...", "tipoffAt": "...", "league": "PREMIER",
      "status": "SCHEDULED",
      "home": { "clubId": "...", "slug": "...", "name": "...", "shortName": "..." },
      "away": { ... },
      "venue": { "name": "...", "isPrimary": true }
    },
    "prediction": {
      "homeWinProb": 0.68,
      "predHomeScore": 84, "predAwayScore": 78,
      "isProvisional": false, "isFinal": false, "isEarlySeason": false,
      "modelVersion": "winner-v1.0.0",
      "summary": "ホームのチーム力が上回っていること、アウェイが2連戦の2戦目で疲労していることが、この予測の主な理由です。",
      "reasons": [
        { "group": "TEAM_STRENGTH", "label": "チーム力の差",
          "value": "＋82ポイント", "favors": "HOME", "strength": 3 }
      ]
    },
    "evaluation": null,
    "playerPredictions": [
      {
        "playerId": "...", "name": "...", "position": "PG", "clubId": "...",
        "availProb": 0.95,
        "summary": { "min": 31.2, "pts": 18.4, "reb": 3.1, "ast": 6.1 },
        "error":   { "min": 5.8,  "pts": 4.8,  "reb": 2.1, "ast": 1.5 },
        "box": {
          "fg":  { "m": 6.4, "a": 13.1, "pct": 0.489 },
          "fg2": { "m": 4.1, "a":  7.8, "pct": 0.526 },
          "fg3": { "m": 2.3, "a":  5.3, "pct": 0.434 },
          "ft":  { "m": 3.3, "a":  3.9, "pct": 0.846 },
          "oreb": 0.6, "dreb": 2.5,
          "ast": 6.1, "tov": 2.2, "stl": 1.1, "blk": 0.3,
          "pf": 2.4, "fd": 3.1,
          "efgPct": 0.577, "tsPct": 0.601
        }
      }
    ],
    "recentForm": { "home": { "last5": ["W","W","L","W","W"], "avgMargin": 6.2, "elo": 1582 },
                    "away": { ... } },
    "modelAccuracy": { "version": "winner-v1.0.0", "accuracy": 0.682, "brier": 0.204, "n": 312 }
  }
}
```

**レスポンス形状は試合前後で同一にする。** `game` は日程・会場・結果といった事実のみを持ち、`prediction` は `game` の内側ではなく `data` 直下に置く。試合前後で変わるのは各フィールドの中身であって、キーの位置ではない。

| キー | 試合前 | 試合後 |
|---|---|---|
| `data.game` | `status: "SCHEDULED"`、スコアなし | `status: "FINISHED"`、`homeScore` / `awayScore` あり |
| `data.prediction` | `isFinal: false` | `isFinal: true` |
| `data.evaluation` | **`null`** | 判定・誤差・`bucketContext` |
| `data.playerPredictions` | 予測値 | 予測値 + `actual`（`plusMinus` は実績のみ） |
| `data.recentForm` / `data.modelAccuracy` | あり | あり |

キーの位置が試合の状態で動くと、クライアントが状態ごとに別のパスを持つことになり、**片方だけ壊れる不具合が出る**。

`availProb < 0.5` の選手は `playerPredictions` に含めない。`strength` は 1〜4 の段階値で、SHAP の生値は返さない。

**`pct` は試投数の予測値が閾値以上のときのみ返す。** 閾値未満は `null` とし、クライアントは分数のみを表示する。

| 項目 | 閾値 |
|---|---|
| FG% | `fg.a >= 4` |
| 2FG% / 3FG% | それぞれの `a >= 3` |
| FT% | `ft.a >= 3` |
| EFG% / TS% | `fg.a >= 4` |

**`plusMinus` の予測は返さない。** 試合後のレスポンスでは実績値のみを `actual` 側に含める。

`pts` と `reb` はサーバ側で導出して `summary` に入れる（クライアントで計算させると実装ごとにずれる）。`box` には導出元のカウントをそのまま入れ、クライアントは表示するだけにする。

#### GET /games/:gameId（試合後）

```jsonc
{
  "data": {
    "game": { ..., "status": "FINISHED", "homeScore": 88, "awayScore": 81 },
    "prediction": { "homeWinProb": 0.68, "predHomeScore": 84, "predAwayScore": 78,
                    "isProvisional": false, "isFinal": true, "isEarlySeason": false,
                    "modelVersion": "winner-v1.0.0",
                    "summary": "...", "reasons": [ ... ] },
    "evaluation": {
      "isCorrect": true,
      "scoreError": 3,
      "bucketContext": { "bucket": "60-70%", "n": 42, "correct": 29, "rate": 0.690 }
    },
    "playerPredictions": [ ... ],
    "recentForm": { ... },
    "modelAccuracy": { ... }
  }
}
```

`bucketContext` は「68%と予想した試合は42試合中29試合が的中」という表示の根拠になる。**外れた試合でもこれを返す。**

#### GET /accuracy

`accuracy_summary` を単一テーブルから読む（読み取り数十行）。

```jsonc
{
  "data": {
    "overall": { "accuracy": 0.682, "brier": 0.204, "n": 312, "baselineAccuracy": 0.601 },
    "bySeason": [ ... ],
    "byModel":  [ ... ],
    "calibration": [ { "bucket": "60-70%", "predicted": 0.65, "actual": 0.63, "n": 88 } ],
    "byProvisional": { "provisional": { "accuracy": 0.66, "n": 120 },
                       "confirmed":   { "accuracy": 0.70, "n": 192 } }
  }
}
```

#### GET /health

```jsonc
{
  "data": {
    "jobs": [ { "job": "daily_ingest", "lastSuccessAt": "...", "lastRunStatus": "SUCCESS" } ],
    "stale": false,
    "activeModels": ["winner-v1.0.0"],
    "quota": { "workersRequestsToday": 1240, "d1RowsReadToday": 98000,
               "d1RowsWrittenToday": 3200, "cacheHitRate": 0.91 },
    "degraded": false
  }
}
```

**`error_message` を返さない。** 詳細は返さず、状態のみを公開する。

### 3.4 内部エンドポイント

`Authorization: Bearer` を要求する。トークンは用途で分離する。

| メソッド | パス | トークン | 用途 |
|---|---|---|---|
| **POST** | **`/internal/masters`** | `INGEST_TOKEN` | マスタの投入（`seasons` / `clubs` / `club_source_ids`） |
| POST | `/internal/games` `/internal/stats` `/internal/entries` `/internal/ratings` | `INGEST_TOKEN` | ファクトの取り込み |
| POST | `/internal/predictions` | `INGEST_TOKEN` | 予測の追記（親子をまとめて受ける） |
| POST | `/internal/finalize` | `FINALIZE_TOKEN`（破壊的操作のため分離） | freeze |
| POST | `/internal/evaluate` `/internal/summary` `/internal/log` | `INGEST_TOKEN` | 照合・集計・ログ |
| **POST** | **`/internal/models`** | `INGEST_TOKEN` | 学習済みモデルの登録 |
| **GET** | **`/internal/models/active`** | `INGEST_TOKEN` | 有効モデル一覧（メタのみ） |
| **GET** | **`/internal/models/:version/artifact`** | `INGEST_TOKEN` | artifact 本体を1本ずつ取得 |
| **GET** | **`/internal/games/ingested`** | `INGEST_TOKEN` | backfill の再開判定 |
| **GET** | **`/internal/predictions/pending`** | `INGEST_TOKEN` | 照合対象の確定予測 |
| **GET** | **`/internal/metrics/active`** | `INGEST_TOKEN` | 現行モデルの識別子と記録済み評価値。**`/internal/models` とは別のルータにする**（同じルータを2箇所にマウントすると `/internal/metrics/active` がモデル一覧の `/active` に当たる） |

#### `POST /internal/masters`

`seed_master`（工程2）が使う唯一の書き込み口である。D1 への書き込みは Workers 経由に一本化されているため（絶対ルール3）、マスタにも口が必要になる。

```jsonc
// POST /internal/masters
{
  "seasons": [
    { "id": "2026-27-PREMIER", "label": "2026-27", "league": "PREMIER",
      "startDate": "2026-09-22", "endDate": "2027-05-30" }
  ],
  "clubs": [
    { "id": "703", "slug": "utsunomiya-brex", "name": "宇都宮ブレックス" }
  ],
  "clubSourceIds": [
    { "sourceId": "703", "clubId": "703", "validFrom": "2016-09-01", "note": null }
  ]
}
```

**テーブル名を引数に取る汎用エンドポイントにしない。** 「任意のテーブルへ書ける口」を作ると、Zod 検証・認可・`is_final` 保護という関門の意味が失われる。**名前付きの配列**にし、配列ごとに個別の Zod スキーマを持つ。3つの配列はいずれも省略可とし、与えられたものだけを処理する。

**1リクエストで全件が入る。** `seasons` 11行（5列 → `floor(100/5)=20` 行/文 → 1文）、`clubs` 30行（2文）、`club_source_ids` 30行（2文）で**計5文**。50クエリ上限に対して余裕がある。分割は不要だが、上限の算出は 3.4 の式に従う。

**冪等にする。** `INSERT ... ON CONFLICT DO UPDATE` とし、何度実行しても同じ結果になること。`seed_master` は運用手順（8.1）で2回以上流れうる。

**`club_seasons` と会場マスタはこの口では受けない。** どちらも backfill が試合データから作る（1.2 / 4.4）。ここで受けられるようにすると、事前に列挙できないはずのものを手で入れる経路が残る。

#### `POST /internal/games` は、試合データから導かれるマスタも受ける

**backfill が試合データから作るマスタ（`players` / `club_seasons` / `venues` /
`venue_source_keys`）を D1 へ書く口が必要である。** D1 への書き込みは Workers 経由に
一本化されており（絶対ルール3）、これらに口がないと backfill が実装できない。
v1.16 まで、この口が設計に書かれていなかった。

**専用のエンドポイントを増やさず、`POST /internal/games` の本文に任意の配列として足す。**
理由は2つある。

1. **FK の順序と原子性。** `player_game_stats` は `players` を、`games` は `venues` を
   参照する。別リクエストに分けると、途中で失敗したときに「試合はあるが会場がない」
   状態が残る。同一 `batch()` に **`venues` → `venue_source_keys` → `players` →
   `club_seasons` → `games` → `team_games`** の順で入れれば、FK 順を守ったまま原子性が保てる
2. **出所が同じ。** これらはすべて同じ試合レスポンスから抜き出した値である
   （`StadiumCD` / `StadiumNameJ` / `PlayerID` / `PlayerNameJ` / `TeamNameJ`）。
   別の口にすると、同じ1回の取得結果が2つのリクエストに分かれる

**テーブル名を引数に取る汎用の口にはしない**（`POST /internal/masters` と同じ理由）。
名前付きの配列にし、配列ごとに Zod スキーマを持つ。行数上限は各テーブルの
`max_rows_per_request` で個別に検査する。

`venue_revisions`（名称履歴）と `player_seasons` はこの口では受けない。前者は
シーズンをまたいだ集計が必要で1試合の取り込みでは決められず（U-10 の解決に従い
別の工程で作る）、後者は登録区分とポジションが未決（C03）で推測で埋めないためである。

#### 内部 GET を置く理由と射程

バッチは**入力データ**としては D1 を読まない（`batch/snapshot/*.parquet` のみ）。しかし運用上、D1 の現在値が要る場面が4つある — backfill の再開判定、照合対象の取得、モデル artifact の読み出し、現行モデルの識別。これらを `/internal/*` の GET に集約することで、**D1 REST API の直叩きを作らない**という方針を保ったまま実装できる。

| 区分 | 読み取り元 |
|---|---|
| 特徴量生成・学習・推論の入力データ | **スナップショットのみ** |
| 上記4つの運用上の読み取り | `/internal/*` の GET |
| D1 REST API の直叩き | **全面禁止** |

返却は `{ "data": ..., "meta": ... }`（3.1）に揃え、`Cache-Control: no-store` を付ける。いずれも1リクエストあたり1〜2クエリで、50クエリ制限にも読取枠にも影響しない。

```jsonc
// GET /internal/games/ingested?seasonId=2016-17-B1
{ "data": { "seasonId": "2016-17-B1", "count": 540,
            "gameIds": ["...", "..."] } }

// GET /internal/predictions/pending?limit=200
//   games.status が FINISHED / CANCELLED / POSTPONED で、
//   prediction_results が未登録の is_final = 1 の予測
{ "data": { "count": 12, "predictions": [
    { "predictionId": "...", "gameId": "...", "seasonId": "...",
      "modelVersion": "winner-v1.0.0", "homeWinProb": 0.68,
      "predHomeScore": 84, "predAwayScore": 78, "wasProvisional": false }
  ] } }

// GET /internal/models/active?league=PREMIER
//   artifact_text は含めない（33本 × 最大1.5MB になりレスポンスに載らない）
{ "data": { "models": [
    { "version": "winner-v1.0.0", "modelType": "WINNER", "target": "",
      "algo": "lightgbm", "params": { }, "featureList": ["elo_diff"],
      "winProbSource": "WINNER", "marginSigma": null,
      "artifactSha256": "...", "artifactBytes": 412345, "calibrator": null }
  ] } }

// GET /internal/models/:version/artifact
{ "data": { "version": "winner-v1.0.0",
            "artifactText": "tree\nversion=v4\n...", "artifactSha256": "..." } }

// GET /internal/metrics/active?modelType=WINNER&target=&league=PREMIER
{ "data": { "version": "winner-v1.0.0", "evalWindow": "2024-25..2025-26",
            "cvBrier": 0.2041, "cvEce": 0.031, "trainRows": 6120 } }
```

**`/internal/metrics/active` が返す値を採用判定の比較に使わない。** これは「学習当時のウィンドウで測った値」であり、新旧で評価対象が違えば比較にならない（4.6）。採用判定に使う現行モデルの Brier は、**この API で識別子を得てから artifact を取得し、新しい評価ウィンドウで再評価した値**である。API の返す `cvBrier` はログと突き合わせ用に限る。

**`artifact_text` を一覧に含めない。** 有効モデルは最大33本あり、1本あたり最大1.5MB であるため、一覧に本体を載せるとレスポンスが数十MBになる。一覧（メタ）と本体（1本ずつ）を分け、日次推論は必要な分だけ取得する。取得後は `artifactSha256` を照合する。

**`/internal/predictions` の必須ガード**

1. Bearer を**定数時間比較**する（両者を SHA-256 ハッシュ化してから比較する。Workers の `timingSafeEqual` は長さ不一致で例外を投げる）
2. `Bearer ` プレフィックスを正規表現で厳格に検証する
3. Zod で型と**値域**を検証する
4. **`games.tipoff_at <= now` の試合は 409 `ALREADY_FINAL` で拒否する**
5. 旧行の非活性化と新行の挿入を**単一 `batch()`** で実行する
6. UPDATE には必ず `WHERE is_final = 0` を付ける
7. 日次の書き込み回数上限を設け、超過時は 429

**日次上限は `predictions` テーブルへの挿入行数で数える（1日2,000行）。** 子テーブル（`player_predictions` / `prediction_team_targets` / `prediction_reasons` / `prediction_model_bundle`）は親に付随して増減するため**別枠とし、独立した上限は設けない**。子まで同じカウンタで数えると、`player_predictions` だけで約2,200行/日（1.5）に達し、正常な運用が上限に当たって止まる。この上限の目的は「トークンが漏れたときの被害を1日分に抑える」ことであり、親の本数を抑えれば子も連動して抑えられる。

#### バッチサイズ（テーブルごとに算出する）

D1 Free には**1 Worker 呼び出しあたり50クエリ**という上限がある（Paid は1,000）。旧版の「1リクエスト最大500件」は、**書き込み対象14テーブルのうち12**でこの上限を超えていた。

1文に詰められる行数は `floor(100 / 列数)`（バインドパラメータ上限100）で決まるため、

```
max_rows_per_request = floor(100 / 列数) × 40
```

とする（係数40は、50クエリのうち10を非活性化・ログ・整合性確認などの余裕として残すため）。

**列数は DDL の全列数で数える。** `DEFAULT` を持つ列を「INSERT 文に含めない前提」で除くと、実装が明示指定に変わった瞬間に上限を超える。上限は最も厳しい側で固定し、実装の書き方に依存させない。

**書き込み対象テーブル（バッチが `/internal/*` 経由で INSERT する）**

| テーブル | 全列数 | 1文の行数 | **1リクエスト上限** | 旧設計500行での必要文数 |
|---|---|---|---|---|
| `player_predictions` | 31 | 3 | **120** | **167（上限の3.3倍）** |
| `player_game_stats` | 24 | 4 | **160** | 125 |
| `games` | 24 | 4 | **160** | 125 |
| `team_game_stats` | 22 | 4 | **160** | 125 |
| `model_versions` | 25 | 4 | **160** | —（1行ずつ登録） |
| `predictions` | 19 | 5 | **200** | 100 |
| `prediction_team_targets` | 17 | 5 | **200** | 100 |
| `prediction_results` | 14 | 7 | **280** | 72 |
| `team_games` | 10 | 10 | **400** | 50 |
| `accuracy_summary` | 9 | 11 | **440** | 46 |
| `team_ratings` | 8 | 12 | **480** | 42 |
| `prediction_reasons` | 8 | 12 | **480** | 42 |
| `game_entries` | 6 | 16 | **640** | 32（上限内） |
| `prediction_model_bundle` | 4 | 25 | **1,000** | 20（上限内） |

**マスタ・運用テーブル**（`seed_master` と `ingestion_logs`。件数が小さく制約にならないが、定数は同じ式で持つ）

| テーブル | 全列数 | 1文の行数 | 1リクエスト上限 |
|---|---|---|---|
| `venue_source_keys` | 2 | 50 | 2,000 |
| `clubs` / `players` / `seasons` / `club_source_ids` | 5 | 20 | 800 |
| `venue_revisions` | 5 | 20 | 800 |
| `venues` | 7 | 14 | 560 |
| `club_seasons` / `player_seasons` | 8 | 12 | 480 |
| `ingestion_logs` | 9 | 11 | 440 |

上限値は `api/src/config/batch-limits.ts` に定数として置き、**列数から機械的に算出する関数と、その結果が50クエリ以内であることを検証するテスト**（`test_batch_size_within_query_limit`、A-10）を置く。列を1つ増やしたときに上限を静かに超えることを防ぐ。

**列数は DDL を唯一の出典とする。** 上の表は `db/migrations/*.sql` から数えた値であり、手で書き換えない。CI で DDL と定数の一致を検査する（`test_batch_limits_match_schema`）。実際、v1.3 の表は `player_predictions` を26列・`games` と `team_game_stats` を20列としていたが、DDL 上はそれぞれ31・23・22列で、`games` と `team_game_stats` の上限は 200 ではなく **160** が正しかった。

> **`batch()` 内の各文が50クエリ制限にどう計上されるかは、Cloudflare の公式ドキュメントに記載がない。** 1文＝1クエリという最も厳しい前提で設計し、Phase 0（P0-13）で実アカウントでの実測を行う。実測の結果 `batch()` 全体が1クエリと数えられるのであれば上限を緩められるが、**実測前に緩めない**。

**CPU 時間は制約ではない。** 実測では500行の JSON（141KB）のパース 0.56ms、検証 0.28ms、`batch()` 組立 0.50ms で合計 1.34ms。Zod で3〜4ms と見込まれるが、Workers Free の CPU 10ms に対して余裕がある。**バッチサイズは CPU ではなくクエリ数で決める。**

**トークンの2キー方式**: 認証は `INGEST_TOKEN` と `INGEST_TOKEN_NEXT` のいずれかに一致すれば通す。無停止で回転できる。

### 3.5 キャッシュとレート制限

| パス | Cache-Control |
|---|---|
| `/games?date=`（過去日） | `public, max-age=60, s-maxage=3600, stale-while-revalidate=86400` |
| `/games/:id`（確定済み） | 同上 |
| `/games/:id`（未実施） | `public, max-age=60, s-maxage=300, stale-while-revalidate=3600` |
| `/accuracy` | `public, max-age=60, s-maxage=3600, stale-while-revalidate=86400` |
| `/teams*` | `public, max-age=60, s-maxage=3600` |
| `/health` | `no-store` |

**`max-age` を長く取らない。** 一度 `max-age=86400` を返すとそのクライアントは24時間必ず古い値を見る。エッジをパージしても届かない。

**「パージする」に依存しない。** Workers の Cache API の `delete()` はそのコロにしか効かず、`*.pages.dev` ではゾーンパージも使えない。**キャッシュキーにデータ世代（最終取り込み時刻）を含める**ことで、新しい世代が出れば自動的に別キーになり、パージ自体が不要になる。

キャッシュキーは**許可リストにないクエリパラメータを除去して正規化する**。`?fbclid=` のようなパラメータで無限にフラグメント化すると、キャッシュを迂回して D1 に直撃する（キャッシュバスティング）。

#### レート制限

Cloudflare WAF のレートリミット（無料プラン）は次の制約を持つ。**旧版の「60req/分を超えたら1分ブロック」は無料枠では設定できない。**

| 項目 | 無料プランの制限 |
|---|---|
| ルール数 | **1** |
| カウント期間 | **10秒のみ**（1分・5分などは有料） |
| ブロック時間 | **10秒のみ** |
| カウント対象 | **IP のみ**（ヘッダやクッキー単位は有料） |
| 照合フィールド | **Path と Verified Bot のみ** |

したがって設定は次のとおりとする。

```
式:       (http.request.uri.path contains "/api/v1/") and (not cf.bot_management.verified_bot)
しきい値: 10秒あたり 30 リクエスト
動作:     ブロック 10秒
```

**10秒ブロックは総量規制としては弱い。** 攻撃者は10秒待てば再開できる。これは「誤った実装のクライアントによる暴走」と「素朴なクローラの総当たり」を止めるための措置であり、意図的な攻撃には効かない。真の防御は次の2つで、WAF はその補助と位置づける。

1. **主要導線が静的配信で Workers を通らない**（枠が枯渇しても当日の予測は表示され続ける）
2. **URL 空間の有限化**（シーズン範囲外の日付は D1 到達前に404）

Workers 側にも Cache API を使った簡易カウンタを置き、429 + `Retry-After` を返す。これは WAF より柔軟な期間を取れるが、Cache API はコロ単位であるため厳密な総量規制にはならない。**それを承知のうえで、階層の1つとして持つ。**

**CORS はレート制限の代替ではない。** ブラウザ内の制限であり、サーバからのアクセスには効かない。しかもフロントは静的出力でサーバサイド fetch するため `Origin` すら付かない。目的が「他サイトからの埋め込み抑止」であることを明記する。

### 3.6 セキュリティヘッダ

`web/public/_headers` に設定する。

```
/*
  X-Content-Type-Options: nosniff
  Referrer-Policy: strict-origin-when-cross-origin
  Permissions-Policy: geolocation=(), camera=(), microphone=()
  Content-Security-Policy-Report-Only: default-src 'self'; script-src 'self' 'sha256-<テーマ適用スクリプトのハッシュ>'; style-src 'self' 'unsafe-inline'; img-src 'self' data:
```

テーマ適用の inline script は**最初から `sha256-` ハッシュ指定で許可する形で書く**。後から CSP を入れるとこのスクリプトが真っ先に壊れ、「CSPを入れたらテーマがちらつくのでCSPをやめる」という後退が起きる。Report-Only で数週間様子を見てから強制に切り替える。

`dangerouslySetInnerHTML` を使わない。

---

## 4. バッチ処理詳細

### 4.1 ワークフロー定義

```yaml
# .github/workflows/daily-ingest.yml
name: daily-ingest
on:
  schedule:
    - cron: '0 21 * * *'     # 06:00 JST
  workflow_dispatch:
permissions:
  contents: write            # 静的JSONとスナップショットをコミットするため
                             # 他のワークフローは contents: read のまま
concurrency:
  group: d1-write            # D1 へ書き込むジョブの同時実行を禁止する
  cancel-in-progress: false  # 走行中のジョブは殺さず、後続を待たせる
jobs:
  ingest:
    runs-on: ubuntu-latest
    timeout-minutes: 30
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.12', cache: 'pip' }
      - run: pip install -r batch/requirements.txt
      - run: python -m batch.jobs.daily_ingest
        env:
          INGEST_TOKEN: ${{ secrets.INGEST_TOKEN }}
          API_BASE_URL: ${{ vars.API_BASE_URL }}
```

| ワークフロー | cron (UTC) | JST | タイムアウト | `permissions` | `concurrency` |
|---|---|---|---|---|---|
| `ci` | push / PR | — | 10分 | `contents: read` | `ci-${{ github.ref }}` |
| `daily-ingest` | `0 21 * * *` | 06:00 | 30分 | **`contents: write`** | `d1-write` |
| `gameday-update` | `0 2,4,7 * * *`（各スロットで `tipoff > now+30分` のみ対象） | 11:00 / 13:00 / 16:00 | 20分 | **`contents: write`** | `d1-write` |
| `parser-canary` | `0 22 * * *` | 07:00 | 10分 | `contents: read` | `canary` |
| `monthly-train` | `0 19 1 * *` | **毎月2日 04:00**（1日19:00 UTC は JST では翌日04:00） | **120分** | `contents: read` | `d1-write` |
| `backfill` | 手動のみ | — | 300分 | `contents: read` | `d1-write` |

**`contents: write` を与えるのは `daily-ingest` と `gameday-update` の2つだけ。** この2つは静的JSON（`web/public/data/`）とスナップショット（`batch/snapshot/`）をリポジトリへコミットするため書き込み権限が要る。`monthly-train` はモデルを D1 に登録するだけでコミットしないため `read` のままでよい。残りも同様。**既定を `read` に置き、必要なものだけ個別に緩める**という順序を崩さない。

**`concurrency: d1-write` で D1 へ書き込むジョブの同時実行を禁止する。** `cancel-in-progress: false` とし、走行中のジョブを殺さず後続を待たせる。

この設定がないと、cron 遅延で `daily-ingest`（21:00 UTC・遅延して02:00に開始）と `gameday-update`（02:00 UTC）が重なった場合に、**両方が同じ試合に対して再推論を行い、`revision` の採番が競合する**。D1 にはリクエストを跨ぐトランザクションがないため、「最大 revision を読む → +1 して挿入する」という2段階が Read-Modify-Write 競合を起こし、`UNIQUE (game_id, model_version, revision)` 違反か、より悪い場合は2本の `is_active = 1` が同時に立つ（部分ユニークインデックスがあるため後者は防がれ、片方が例外で落ちる）。**走行中を殺してはならない**のは、殺されたジョブが `batch()` の途中で止まり `ingestion_logs` が `RUNNING` のまま残るため。

`finalize` は Workers の Cron Trigger（毎時）で `/internal/finalize` を叩く。GitHub Actions ではなく Workers に置くのは、外部アクセスに依存しない純粋な D1 操作であり、スクレイピングの失敗に巻き込まれてはならないため。

**`finalize` は `predictions` と `player_predictions` の `is_final` を、単一 `batch()` で子 → 親の順に 0 → 1 にする。** `prediction_reasons` / `prediction_team_targets` / `prediction_model_bundle` は `is_final` 列を持たず、親参照トリガによって同時に凍結されるため UPDATE しない（1.8）。

`finalize` は `d1-write` グループには入らない（毎時実行を待たせると freeze が遅れるため）。freeze は他の書き込みと衝突しない（対象行が `is_final = 0 → 1` の遷移のみで、`gameday_update` は `tipoff_at > now + 30分` の試合しか触らない）。

**cron は UTC。** GitHub Actions の cron は数十分〜数時間遅延しうるため、遅延しても壊れない設計（tipoff ガード）を前提とする。`gameday-update` のスロットは JST 11:00 / 13:00 / 16:00 で、B.LEAGUE の主な開始時刻（14:05 / 15:05 / 17:05 / 19:05）の2〜3時間前を捕捉する。

### 4.2 daily_ingest

```python
def run():
    log = start_log("daily_ingest")
    status = "SUCCESS"

    # 0. robots.txt / 利用規約の変更検知
    if robots_changed() or terms_changed():
        finish_log(log, "ABORTED", "robots or terms changed")
        sys.exit(1)

    # 1. スクレイピング区間（ここだけ AbortedByRemote を捕捉する）
    try:
        games = scrape_finished_games(yesterday_jst())
        post_games(games)
        for g in games:                       # 3秒間隔・値域検証
            post_stats(scrape_boxscore(g.id))
    except AbortedByRemote:
        status = "PARTIAL"                    # 内部処理は継続する
    except ParseErrorStreak:
        status = "PARTIAL"

    # 2. スナップショット更新（D1 へ書いたのと同じデータから書き出す）
    write_snapshot()                          # batch/snapshot/*.parquet + MANIFEST.json

    # 3. 内部処理（外部アクセスの失敗に巻き込まれない）
    detect_score_revisions()                  # result_revision を進め再評価キューへ
    evaluate_finished_games()                 # GET /internal/predictions/pending → 照合（VOID を除外）
    rebuild_accuracy_summary()
    recompute_ratings(from_date=affected_min_date())   # 入力はスナップショット
    write_snapshot(tables=["team_ratings"])   # 特徴量が読むのはこちら。MANIFEST も再生成する
    post_ratings()                            # 同じ値を /internal/ratings で D1 にも送る

    # 4. 推論（未開始試合のみ）
    ds     = load_snapshot()                  # 入力データとして D1 は読まない
    models = load_active_models()             # GET /internal/models/active → :version/artifact
    for g in get_upcoming_games(days=7):      # tipoff_at > now のみ
        feats = build_features(g.id, as_of=g.tipoff_at, ds=ds)
        p     = predict(models, feats)        # 勝率・margin・total
        try:
            # チーム目標を予想スコアへ整合化してから選手側へ渡す（2.4 前段）
            target = {
                side: reconcile_team_targets(predict_team_rates(models, feats, side),
                                             pred_score=p.score[side])
                for side in ("home", "away")
            }
            box = {side: reconcile(predict_players(models, feats, side), target[side])
                   for side in ("home", "away")}
        except InfeasibleTargetError as e:
            log_event("infeasible_target", game_id=g.id, kind=type(e).__name__)
            target, box = None, None          # チーム予測のみ保存し、個人は破棄する
        post_prediction(g, p, target, box, data_as_of=ds.max_finished_at)

    # 5. 静的JSON の書き出し
    write_static_json()
    commit_and_push(["web/public/data/", "batch/snapshot/"])

    finish_log(log, status)
    if status == "PARTIAL":
        sys.exit(1)                           # Actions の失敗通知に乗せる
```

**`AbortedByRemote` の捕捉範囲をスクレイピング区間に限定する。** 旧版は try が処理全体を覆っていたため、前日結果が取れないだけで freeze・照合・Elo・推論まですべてスキップされ、その日の予測更新が止まっていた。

**`InfeasibleTargetError` の捕捉範囲は1試合に限定する。** チーム目標が到達不能だった試合は、チーム予測（勝率・予想スコア）のみを保存して個人スタッツを破棄し、次の試合へ進む。ジョブ全体を止めない。ただし例外オブジェクトはログに入れず、型名と自前メッセージに限定する（7.3）。発生が常態化する場合は `CLAUDE.md` の「止まるべき条件」に該当するため、実装を進めずに報告する。

**Elo の書き出し順序を守る。** `recompute_ratings()` → `team_ratings.parquet` → `/internal/ratings` の順に固定する。特徴量生成が読むのはスナップショット側であり、D1 側は公開APIの表示用の複製である。

**`PARTIAL` を exit 1 で終える。** exit 0 だと Actions の失敗通知に乗らず、誰も気づかないまま数日放置される。

### 4.3 スクレイピングクライアント

実装は `batch/scraper/client.py`。Python標準ライブラリの `urllib` を使い、
HTTPレスポンスはメモリ上で最大5 MiBまで読む。新しい実行時依存は追加しない。

- 3秒 + 0〜1秒の間隔、逐次取得、30秒タイムアウト。リダイレクト・自動再試行はしない。
- 共通の状態ファイルとロックで、同じUTC日の合計3,000リクエストと429/503後の停止を保つ。
  再起動や別クライアントも同じファイルを使う。別ホスト・別CI実行間の状態共有は運用側の責務。
- **ロック待ちは60秒を上限とし、超えたら `StateError` で失敗する（fail closed）。**
  正当な保持は1リクエスト分（転送タイムアウト30秒＋間隔の最大4秒）であり、それを超える
  待機は保持側の異常を意味する。無制限に待つと、停止したプロセスが以後のすべての取得
  ジョブを無言で止める。待機は実時刻で数える（注入する時計はリクエスト間隔の模擬用）。
- **状態の保存は `os.replace` の原子性だけに依り、`fsync` は行わない。** 同一ホストの
  他プロセスに対する可視性と一貫性は `os.replace` で足りる。`fsync` が守るのは電源喪失に
  対する耐久性だけで、この予算には要らない。一方でディスクが詰まると**シグナルでも中断
  できない待ち**に入る（CI で SIGKILL も job timeout も効かない停止を実際に起こした）。
- 件数にはrobots/利用規約、失敗したHTTP試行も含める。取得前に枠を予約する。
- 識別名と連絡先URLを含むUAを必須とする。取得先は公式サイトのHTTPSに限定する。
- robots/利用規約のSHA256を運営者が設定する。未設定・不一致なら本データは取得しない。
  初回の基準候補は専用の確認コマンドでハッシュだけ表示できるが、自動承認はしない。
- robotsは改行をLFへ正規化、利用規約は本文HTML全体をUTF-8文字列としてハッシュ化する。
  HTML全体の変更にも停止する保守的な方式。抽出範囲の最適化は別途変更する。
- 本文はディスクに保存せず、例外にURL・応答本文・元の通信例外を含めない。

使い方、戻り値、未対応範囲は [スクレイパ・パーサ](scraper-parser.md) に記載する。

**Open-Meteo は使用しない**（天候は見送り項目）。仮に外部APIを使う場合も、公式サイト向けの3秒間隔を無条件に適用しない。

### 4.4 パーサと値域検証

**入力は HTML の表ではなく、`<script>` 内に埋め込まれた JSON である**（Phase 0 で確認。基本設計 2.1）。

実装は `batch/parser/`。`ParseError` は必須項目・構造の不一致、
`ValidationError` は値域・恒等式の違反を示す。通信・DB書き込みは行わない。

日程はJSON内のHTML断片、試合詳細は `<script>` の `_contexts_s3id.data` を解析する。
JavaScriptは実行しない。`PeriodCategory=18` で絞り、選手行、チーム記録行
（Category=2）、公式チーム合計行（Category=3）を区別する。

**チーム値はCategory=3の公式合計を採用する。** TeamIDがnullならHOME/AWAYの
親コンテキストから解決し、明示IDが親と異なれば失敗する。合計行の重複・欠落も失敗。
得点・シュートの成功数/試投数は、全選手の値が揃う場合に選手合計と照合する。
リバウンド・ターンオーバー等は選手に付かないチーム記録があるため、選手の単純合計を
公式合計と等しいとは要求しない。Category=2を公式合計に加算しない。

空値はNoneとし、欠損を0に置換しない。旧年度の `PLUSMINUS` キー欠落は許容する。
他の対応表の必須キー欠落はParseError、キーが存在して空ならNoneとして保持する。
選手の複数ポジション・登録区分のDB正規化、公式エントリーの確定条件は未決のため、
今回のパーサからDB用の値を推定しない。

#### フィールド対応表（`parser/fields.py`）

**この表にある列だけを抜き出す。** JSON 全体を保存しない（審判名とプレイバイプレイが含まれるため。要件 5.3）。

| DB の列 | サイトのフィールド | 備考 |
|---|---|---|
| `player_id` | `PlayerID` | 空値なら非選手行。Category=2と3を区別 |
| `club_id` | `TeamID` | `club_source_ids` で `club_id` に解決する |
| `started` | `StartingFlg` | |
| `minutes` | `PlayTime` | **`"MM:SS"` 形式**。分の実数へ変換する |
| `fg2m` / `fg2a` | `PT2M` / `PT2A` | |
| `fg3m` / `fg3a` | `PT3M` / `PT3A` | |
| `ftm` / `fta` | `FTM` / `FTA` | |
| `oreb` / `dreb` | `RB_OFF` / `RB_DEF` | `RB_TOT` は検算に使い、保存しない |
| `ast` / `tov` / `stl` / `blk` | `AS` / `TO` / `ST` / `BS` | |
| `pf` | `FOUL` | |
| **`fd`** | **`FOULON`** | 被ファウル数 |
| `plus_minus` | `PLUSMINUS` | 実績のみ。旧年度でキー欠落ならNULL |
| `pts` | `Point` | 恒等式の検証に使う |
| `attendance`（`games`） | `Attendance` | 試合単位。例: 5530 |
| `club_seasons.name` | `TeamNameJ` | **当時の名称**が入る。`703` は 2016-17 で `栃木ブレックス`（現在は宇都宮ブレックス） |
| `competition`（`games`） | 試合一覧の `event` | `2` → `REGULAR` / `3` → `PLAYOFF`。ボックススコア側にはない |
| `venues.id` | **`StadiumCD`** | 公式の会場ID。**2016-17 は整数 `3`、2025-26 は文字列 `"169"`**。文字列へ正規化する |
| `venues.name` / `venue_revisions.name` | `StadiumNameJ` | その試合時点の名称 |

**`club_seasons` の名称は試合データから作る。** 公式サイトに年度別のクラブ一覧ページは存在せず
（`/club/` に年度指定がなく、`/club_detail/` は初回レスポンスが空）、過去シーズンの正式名称を
マスタ系ページから取る経路がない。一方でボックススコアの `TeamNameJ` は**その試合の時点の
名称**であるため、これを `club_seasons.name` の出典とする。短縮表記（`club_seasons.short_name`）
は試合一覧ページのクラブ選択肢から取る。どちらも取り込みで必ず通るレスポンスであり、追加の
リクエストは発生しない（`verification/RESULTS.md`）。

**取得しないフィールド**: 審判名（`RefereeNameJ*` ほか。特徴量 #30 は見送り）、プレイバイプレイ（`ActionCD*` `PlayText` `X` `Y`）、シュートチャート座標、`EFF` / `EFG` / `TS` / `USG` / `AST_TO`（導出値なので保存しない）。

`POSS` / `OFFRTG` / `DEFRTG` / `NETRTG` はサイト側にも存在するが、値の入り方が未確認である。**自前計算を正とし**、サイトの値は P0-7（係数 0.44 の妥当性）の突き合わせ材料として使う。

#### 日程ページの2つの形（2026-09-22 の実測）

日程の試合行は、区画の見出しによって**日付の出どころが変わる**。

| 区画 | 見出し | 行の `info-arena` の span | 日付の出どころ |
|---|---|---|---|
| 通常のリーグ戦 | `2016.09.22(木)`（日付） | `[第1節, 県 \| 会場, 18:55]` の3つ | **見出し** |
| ステージ（CS ほか） | `B.LEAGUE CHAMPIONSHIP 2016-17`（ステージ名） | `[クォーターファイナル, 県 \| 会場, 05/13 (土), 16:05]` の**4つ** | **行（時刻の直前の span）** |

**見出しだけを日付の出どころにすると、チャンピオンシップが丸ごと落ちる。** 実測では
CS 15試合すべてが欠落した（見出しが日付でないため区画ごと飛ばされた）。行の span が
4つある場合は、時刻の直前を日付として読む。

**暦年は月から決める。** 行の日付は `MM/DD` で年を持たないため、9〜12月はシーズン開始年、
1〜8月は翌年とする。結果は既存の範囲検査（開始年または翌年）を通す。

**`index` が `null` なら、試合があってもそれが最終ページである。** 2016-17 の
チャンピオンシップは 15試合 / `index=null` の単一ページで、`index` の前進を必須に
していた実装は**CSを1件も取り込めなかった**。空の `topics` と `index=null` の組だけを
終端とみなす実装は誤りである。

#### 取り込み前に必ず行う5つの正規化

Phase 0 の実地確認で見つかった、放置すると静かに壊れる箇所。

| # | 内容 |
|---|---|
| 0 | **試合一覧を `event` で絞る。** 取り込むのは `2`（リーグ戦 → `REGULAR`）と `3`（チャンピオンシップ → `PLAYOFF`）だけ。`4` 残留プレーオフ / `5` オールスターゲーム / `11` 入替戦 / `20` アーリーカップは取り込まない。**ラベルではなく値で分岐する**（`2` のラベルは 2026-27 で「B1リーグ」から「B.PREMIER」に変わっている）。詳細は 1.3 |
| 1 | **`PeriodCategory` で絞る。** 1〜4 がクォーター別、15 / 16 が前後半、**18 が試合通算**。18 以外を取り込むと行数が7倍になり、集計がすべて狂う |
| 2 | **チーム集計行を選手行から分離する。** 1カテゴリ27件のうち数件は `PlayerID` が空のチーム集計行で、`TeamID` も空の行が含まれる。混ぜると `player_game_stats` と `team_game_stats` の両方が汚染される |
| 3 | **空文字と 0 を区別する。** 数値フィールドが `0` ではなく `""` で入っていることがある。`int("")` は例外になり、`0` と誤って扱うと「記録なし」が「0回」になる。値域検証の**前に** None へ正規化する |
| 5 | **`StadiumCD` を文字列へ正規化する。** 2016-17 は整数 `3`、2025-26 は文字列 `"169"` で、型がシーズンで違う。正規化しないと同じ会場が `3` と `"3"` の2行になり、`venues` が分裂して収容人数と移動距離が欠損する |

数値パースは全角数字、カンマ、`"-"`（未出場）、**`"MM:SS"` 形式**、`"DNP"`、**空文字**を考慮する。

`ParseError` が連続3件でジョブを中止し `PARTIAL` で記録する。修正時は**サンプルを取得して合成 fixture に落としてからテストを書く**。本番アクセスで試行錯誤しない。

#### fixture の扱い

fixture は**構造のみを模し、値をダミーに置換した合成データ**とする（要件 4.5.2）。埋め込み JSON を読むようになっても方針は変わらない。

- 実サイトから取得した本文を、生のままコミットしない（HTML でも JSON でも同じ）
- 選手名・チーム名・ID はダミーに置換する
- 審判名とプレイバイプレイは fixture にも**含めない**（抽出対象外であることをテストで固定する）
- `.gitignore` の `**/fixtures/**/*.html` に加え、`**/fixtures/**/*.raw.json` も対象にする
- CI で fixtures に実サイト由来の文字列が混入していないことを検査する（A-12）

### 4.5 学習ジョブ

```python
def monthly_train():
    run_leakage_tests()                  # 失敗したらここで停止
    verify_snapshot_manifest()           # 行数と SHA256 の照合。不一致なら学習を中止

    df = load_training_data()            # batch/snapshot/*.parquet のみを読む。D1 は読まない
    df = apply_sample_weights(df)        # 時間減衰 + 観客制限期間の調整
    X, y, w = build_matrix(df)

    # 入れ子の時系列分割
    metrics, best_iters = walk_forward_validate(
        X, y, w, by="season_id", max_folds=5)

    # 最終モデルは best_iteration の中央値で固定学習
    model = train_lightgbm(X, y, w, params=PARAMS,
                           num_boost_round=median(best_iters))

    # 較正は out-of-fold で判断・fit
    if needs_calibration(metrics.oof_ece):
        calibrator = fit_platt(metrics.oof_pred, metrics.oof_y)
    else:
        calibrator = None

    if not passes_criteria(metrics, model):
        log("基準未達のため新モデルを有効化しない", metrics)
        return

    artifact = model.save_model()         # テキスト形式
    if len(artifact.encode()) > 1_572_864:            # 1.5 MiB
        log("artifact がサイズ上限を超えたため登録しない", size=len(artifact))
        sys.exit(1)                                   # PARTIAL 扱い。現行モデルを継続
    # POST /internal/models（Workers 経由。D1 を直接叩かない）
    # Workers 側でも同じ 1.5MB 判定を行い、DBトリガと合わせて三重にする
    version = register_model(artifact, calibrator, metrics)
    activate_model(version)               # uq_model_active が整合を保証


COUNT_STATS = ('fg2a','fg3a','fta','oreb','dreb','ast','tov','stl','blk','pf','fd')
PCT_STATS   = (('fg2_pct','fg2m','fg2a'), ('fg3_pct','fg3m','fg3a'),
               ('ft_pct','ftm','fta'))

def train_player_models():
    """選手モデル。カウント11 + 成功率3 + 出場2 = 16本。学習時間の管理が要る。"""
    df = load_player_training_data()      # 約20万行。スナップショットから
    X = build_player_matrix(df)

    train_and_register('PLAYER_AVAIL', X, y=df.played,  folds=3)
    train_and_register('PLAYER_MIN',   X[df.played], y=df.minutes, folds=3)

    for stat in COUNT_STATS:              # 11項目
        # per-minute レートで学習する。カウントで学習すると出場時間の分散に支配され、
        # 出場時間の予測誤差がそのまま全項目に伝播する
        y = df[stat] / df.minutes
        train_and_register('PLAYER_RATE', X[df.minutes > 0], y=y,
                           target=stat, folds=3)

    for pct, made, att in PCT_STATS:      # 3項目
        # 率は per-minute にしない（時間当たりの量ではない）。
        # 目的変数はシュリンクした成功率、重みは試投数。
        k, prior = SHRINK_K[pct], league_mean(made, att)
        y = (df[made] + k * prior) / (df[att] + k)
        train_and_register('PLAYER_RATE', X[df[att] > 0], y=y,
                           target=pct, weight=df[att], folds=3,
                           params={**PARAMS_REG, "objective": "regression"})


def train_team_rate_models():
    """整合化の目標値。選手側と同じ「試投数 + 成功率」の構造で予測する。

    ここで学習するのは素の水準であり、推論時に reconcile_team_targets() を通して
    予想スコア（Margin/Total 由来）へ整合させてから prediction_team_targets に
    保存する（2.4 前段）。学習段階で予想スコアに合わせ込まない。
    """
    for stat in COUNT_STATS:
        train_and_register('TEAM_RATE', Xt, y=team_df[stat], target=stat, folds=5)
    for pct, made, att in PCT_STATS:
        train_and_register('TEAM_RATE', Xt, y=team_df[made] / team_df[att],
                           target=pct, weight=team_df[att], folds=5)
```

**`TEAM_RATE` を「成功数」で学習しない。** チーム目標が `成功数 > 試投数` を満たしうる状態だと、整合化は原理的に成立しない（2.4）。試投数と成功率で学習すれば構造的に保証される。

**成功率モデルの出力は `clip(0.01, 0.99)` に収める。** ロジット変換の定義域を確保するため。

**`artifact_text` のサイズを登録前に検査する。** 1.5MB を超えたら登録せず、現行モデルを継続使用して `exit 1` で終える。`num_boost_round` の上限を 1,200 とし、early stopping が効かずに上限まで回るケースを防ぐ。

**選手モデルの fold 数は3に制限する**（チームモデルは5）。選手16本 + チーム14本の学習が必要で、タイムアウト120分に収めるための措置である。

学習時間が枠に収まらない場合の対処は、この順で行う。

1. 木をさらに小さくする（`num_leaves` 4、`max_depth` 2）
2. 学習行数を直近5シーズンに制限する
3. 低レート項目（`stl` `blk`）を「その選手の直近平均」で代替し、モデルを持たない

3 を選ぶ場合、画面には「この項目は直近平均を表示しています」と明示する。予測と称して平均を出すことはしない。

実測は Phase 0（P0-10）で確認する。

**検証セットの分離**

```
fold i:  train = seasons[:i-1]
         valid = seasons[i-1]     ← early stopping 用
         test  = seasons[i]       ← 一切触れない
```

旧版は `train_lightgbm(X, y)` に検証セットがなく、early stopping が成立していなかった（学習データを渡せば止まらず、評価シーズンを渡せばリーク）。

### 4.6 採用基準

```python
MIN_EVAL_N   = 500      # これ未満では一切比較しない
MIN_EFFECT   = 0.003    # 最小実質差（Brier）
ECE_FLOOR_K  = 1.5      # ノイズフロア95%点に対する倍率

def passes_criteria(m, model) -> bool:
    # 0. サンプル数の下限。満たさなければ比較そのものを行わない
    if m.n < MIN_EVAL_N:
        log("n < 500 のため比較せず現行モデルを継続", n=m.n)
        return False

    # GET /internal/metrics/active で現行モデルの識別子を得て、その artifact を
    # GET /internal/models/:version/artifact で取得し、新しいウィンドウで再評価する。
    # API が返す cv_brier（学習当時の値）をそのまま比較に使わない。
    current = current_active_brier_on_same_window()
    return (
        # 1. 同一の評価ウィンドウで再評価した Brier が良い
        m.brier < current
        # 2. 有意 かつ 実質的
        and m.brier_diff_ci_upper < 0          # ブートストラップCIが0を跨がない
        and (current - m.brier) >= MIN_EFFECT  # 点推定の差が 0.003 以上
        # 3. ベースラインを上回る
        and m.brier < m.baseline_elo_brier
        # 4. 較正（n 依存のノイズフロア。固定値 0.05 を使わない）
        and m.ece < ece_noise_floor(m.pred_probs, m.n, q=0.95) * ECE_FLOOR_K
        # 5. 特徴量の健全性
        and max(m.feature_null_rates.values()) <= 0.30
    )
```

**Accuracy を採用ゲートに入れない。** ホーム勝率60%の下では良い確率モデルでも Accuracy はベースラインとほぼ並ぶため、Brier で明確に優れているモデルが誤って棄却される。Accuracy は公開指標としてのみ使う。

**`n < 500` では比較を行わない。** 実測では、理想モデル（真の勝率を知る上限）とベースラインの Accuracy 差の標準偏差が n=200 で 0.041 あり、旧基準の `+0.03` はノイズと区別できなかった。ECE も同様で、完全較正モデルの ECE 95%点は n=200 で 0.1163、n=500 で 0.0734、n=1,200 で 0.0479 と n に強く依存する。**固定閾値 0.05 は n=200 では良いモデルを落とし、n=1,200 ではほぼ常に素通りする。**

**`ece_noise_floor()` はモデル自身の予測確率分布からブートストラップで毎回推定する。** 定数表を持たない。実装と検証は `verification/01_ece_noise_floor.py` を流用する。

**月次再学習の検出力は低いが、それは受け入れる。** 真の差 0.03 を検出できる確率は n=200 で 12.7%、n=1,200 でも 54.8% にとどまる。上記のゲートは「ノイズで誤って差し替える」ことを防ぐ側に倒してあり、その代償として「実際の改善を見逃す」。見逃した改善は次回以降の月次で拾う。**検出力を上げるためにゲートを緩めてはならない。**

**比較は同一の評価ウィンドウで行う。** `model_versions.cv_brier`（学習当時の値）を直接比較しない。新旧で検証対象のシーズンが違えば比較になっていない。

**欠損率チェックが必要な理由**: 新特徴量を追加した際、元データが未投入で大半が None になっても、Brier が偶然わずかに良ければ従来の基準では採用されてしまう。実際には新特徴量が機能していないどころか既存特徴量の重みが劣化しており、数値がすべて基準内なので誰も気づかない。

### 4.7 LightGBM パラメータ

```python
PARAMS = {
    "objective": "binary",
    "metric": ["binary_logloss"],
    "learning_rate": 0.03,
    "num_leaves": 7,           # 8000行に対し15は過大
    "max_depth": 3,
    "min_data_in_leaf": 100,
    "feature_fraction": 0.7,
    "bagging_fraction": 0.8,
    "bagging_freq": 1,
    "lambda_l2": 1.0,
    "num_boost_round": 1200,   # 上限。artifact サイズが 1.5MB を超えないための制約でもある
    "early_stopping_round": 100,
    "verbosity": -1,
    # 再現性。これを欠くと Brier が ±0.003 ぶれ、採用判定が乱数になる
    "seed": 42,
    "bagging_seed": 42,
    "feature_fraction_seed": 42,
    "deterministic": True,
    "force_row_wise": True,
    "num_threads": 4,
}
```

探索範囲: `num_leaves ∈ {4, 7, 15}`、`max_depth ∈ {2, 3, 5}`、`min_data_in_leaf ∈ {40, 100, 200}`、`learning_rate ∈ {0.02, 0.05}`。

**精度が出ないときに木を深くしない。** この問題の信号量（Elo差だけで Brier 0.215 程度、上限でも 0.20 前後）に対し、深さ5・葉15は5次の交互作用を許す設定で過大である。まず特徴量を見直す。

**`num_leaves` を 15 にする変更は、artifact サイズの再測定とセットでのみ許可する。** 書式からの推定では `num_leaves=15` / 木1,500本で 2.56MB となり、D1 の1行上限（2,000,000バイト）を超える。サイズガードが登録を拒否するため事故にはならないが、**「探索範囲に 15 があるから」という理由で無自覚に踏むことのないよう、ここに明記する**。

### 4.8 backfill

再開可能にする。

```python
def backfill(season_id: str):
    # GET /internal/games/ingested?seasonId=... （Workers 経由。D1 を直接叩かない）
    # これは「入力データ」ではなく運用上の読み取りであり、絶対ルール3の射程外（3.4）
    done = fetch_ingested_game_ids(season_id)
    for g in schedule_of(season_id):
        if g.id in done:
            continue                            # スキップ
        ...
```

1シーズンあたりの所要時間は Phase 0（P0-4）で実測する。1試合あたり2〜4ページなら1シーズン50〜100分、10シーズンで実質10日かかる。**1日1シーズンに限る**（D1 書込10万行/日の枠もある）。

再開可能にしないと、55分経過時点で 429 を食らった際に翌日また全ページを取り直すことになり、相手サイトへの負荷を二重にかける。

---

## 5. 画面詳細

### 5.1 コンポーネント構成

```
components/
├── prediction/
│   ├── GameCard.tsx / GameCardCompact.tsx
│   ├── ProbabilityBar.tsx / ScoreLine.tsx
│   ├── ReasonList.tsx / PlayerStatTable.tsx
│   ├── StatusBadge.tsx / StaleBanner.tsx
│   └── ResultComparison.tsx      試合後の対比
├── charts/
│   ├── AccuracyTrend.tsx / CalibrationChart.tsx   インラインSVG
└── ui/
    ├── Card.tsx / Tabs.tsx / ThemeToggle.tsx / EmptyState.tsx / Footer.tsx
```

### 5.2 マークアップ規約

意味構造を持たせる。旧版のモックは見出し要素が一つもなく、タブが `span`、装飾目的で `<s>` `<i>` `<em>` を使っていた。

| 要素 | 用途 |
|---|---|
| `<article>` | 試合1件 |
| `<h2>` | 日付見出し、対戦名（視覚的に隠してよい） |
| `<h3>` | 「なぜこの予測になったか」「個人スタッツ予測」 |
| `<nav>` + `<a>` | タブ。最低高さ44px、`aria-current="page"` |
| `<button aria-pressed>` | テーマ切替 |
| `<span>` + class | 装飾。`<s>` `<i>` `<em>` を装飾目的で使わない |

確率ブロックは `role="img"` と一文の `aria-label` を持ち、内部の数値とバーは `aria-hidden="true"` にする。両方に読み上げ対象があると「68 32 ホーム68パーセント…」と重複する。

```
aria-label="ホーム <チーム名> の勝率68パーセント、アウェイ <チーム名> の勝率32パーセント。予想スコア 84対78"
```

`:focus-visible` のアウトライン色をトークン化する（`--accent` の上に `--accent` のリングが乗ると消える）。

### 5.3 主要コンポーネント

#### ProbabilityBar

- `--accent` で塗り、残りを `--track`
- **両チームの勝率を必ず表示**する。圧縮カードでも省略しない
- 45〜55% は「ほぼ互角」と併記する

#### GameCardCompact

```
ホームB 55% — 45% アウェイB
ほぼ互角 ・ 17:05
```

片側のみの表示は要件8.3違反。55%と81%が同じ見た目になると、このツールで最も伝えるべき差が失われる。

#### ReasonList

- 要因グループ単位（4〜5件）
- 「ラベル / 値 / 有利な側」の3要素
- 数値の代わりに3〜4段階のバーで強さを示す
- 冒頭に一行の要約文

#### PlayerStatTable

**段階開示とする。** 20項目 × 最大10人を一度に出すとモバイルでは読めない。

初期表示（1行1選手、4項目）

```
選手A  PG        31.2分  18.4  3.1  6.1   ▾
選手B  C         28.5分  14.2  9.8  1.4   ▾
```

行をタップして展開（フルボックススコア）

```
選手A  PG                                  ▴
  出場時間   31.2分  ±5.8
  得点       18.4    ±4.8
  FG         6.4 / 13.1  (48.9%)
    2P       4.1 /  7.8  (52.6%)
    3P       2.3 /  5.3  (43.4%)
  FT         3.3 /  3.9  (84.6%)
  リバウンド  3.1  ±2.1   (OR 0.6 / DR 2.5)
  アシスト    6.1  ±1.5
  ターンオーバー 2.2
  スティール  1.1
  ブロック    0.3
  ファウル    2.4
  被ファウル  3.1
  EFG% 57.7  ・  TS% 60.1
```

規約

- 初期表示は `min` / `pts` / `reb` / `ast` の4項目のみ
- 誤差（`±`）は主要4項目にのみ併記する。全項目に出すと画面が埋まる
- **率は分数と併記する。** 単独で `48.9%` と出さない
- **試投数が閾値未満なら率を出さず分数のみ**（`1.2 / 2.4` のように）
- 展開状態は `localStorage` に保存しない（毎回たたんだ状態で開く）
- 展開の開閉はアニメーション付きの `<details>` 相当にし、キーボードで操作できるようにする
- **`＋/－` は試合前には表示しない。** 試合後の結果画面でのみ実績を出す
- ST / BS は誤差が平均値と同水準であることを、詳細画面の注記で1行説明する

#### StatusBadge / StaleBanner

| 状態 | 文言 | トークン |
|---|---|---|
| 暫定 | 暫定 | `--text-3` の枠線 |
| 確定 | 確定 | `--accent-bg` |
| 序盤 | 序盤のため精度が安定しません | `--warn-bg` / `--warn` |
| 更新遅延 | （全幅バナー） | `--warn-bg` / `--warn` |

#### ResultComparison（試合後）

```
実際のスコア   88 – 81（ホーム勝利）
予測          ホーム 68% / 予想スコア 84 – 78
判定          予測どおりでした（得点差の誤差 3点）
位置づけ      68%と予想した試合は、これまで42試合中29試合（69.0%）が的中しています
```

外れた場合も同じ構成で、判定の文言だけが変わる。赤などの強い否定色を使わない。

### 5.4 テーマ実装

```css
:root {
  --bg:#F6F7F9; --surface:#FFFFFF; --border:#E5E8ED;
  --text:#0F1622; --text-2:#5A6577; --text-3:#667184; --text-4:#8E99AB;
  --accent:#416A0E; --accent-bg:rgba(65,106,14,.10); --track:#E8EBF0;
  --warn:#7A4A12; --warn-bg:#FEF3E7;
}
:root[data-theme="dark"] {
  --bg:#0B0E13; --surface:#131820; --border:#212A36;
  --text:#E6EAF2; --text-2:#93A0B5; --text-3:#7C8798; --text-4:#59657A;
  --accent:#A3E635; --accent-bg:rgba(163,230,53,.12); --track:#232C3A;
  --warn:#E0A458; --warn-bg:rgba(224,164,88,.12);
}
```

```html
<script>
  try { var t = localStorage.getItem('theme');
        if (t) document.documentElement.setAttribute('data-theme', t); } catch (e) {}
</script>
```

- 既定はライト。OS設定への追従は行わない
- `localStorage` へのアクセスは必ず try/catch で囲む
- この inline script のハッシュを CSP に登録する（3.6）

### 5.5 数値表示

すべての数値に `font-variant-numeric: tabular-nums` を適用する。

| 値 | 表記 |
|---|---|
| 勝率 | 整数の百分率（`68%`） |
| 予想スコア | **整数**（`84 – 78`）。MAE 8〜10点に対し小数第1位は精度の誤認を招く |
| 個人スタッツ（カウント） | 小数第1位（`18.4`）。期待値なので整数にしない |
| 個人スタッツ（成功/試投） | `6.4 / 13.1` の分数。率は括弧で併記 |
| 率（FG%等） | 小数第1位の百分率（`48.9%`）。**試投数が閾値未満なら表示しない** |
| 出場時間 | 小数第1位＋単位（`31.2分`） |
| 的中率 | 小数第1位＋**母数**（`68.2%（312試合）`） |
| Brier | `0.204`（小数点前のゼロを省略しない）。的中率ページの折りたたみ内のみ |

### 5.6 データ取得

```ts
// app/page.tsx — 静的出力。ISR を使わない
export const dynamic = 'force-static';

export default function Page() {
  // ビルド時に埋め込まず、クライアントで静的JSONを読む
  // （バッチが更新しても再ビルド不要にするため）
}
```

```ts
const res = await fetch('/data/today.json', { cache: 'no-store' });
```

`/data/today.json` は Pages の静的アセットであり、Workers も D1 も消費しない。取得失敗時は `EmptyState` のエラー表示にフォールバックする。

#### Next.js 16 での `params`

**`params` と `searchParams` は Promise である。** 同期アクセスは型エラーになる。

```ts
// app/schedule/[date]/page.tsx
export async function generateStaticParams() {
  return listDatesInRecentSeasons(5).map((date) => ({ date }));  // 直近5シーズンのみ
}

export default async function Page({
  params,
}: {
  params: Promise<{ date: string }>;       // Promise。同期で受け取らない
}) {
  const { date } = await params;
  if (!isWithinRecentSeasons(date, 5)) notFound();
  ...
}
```

#### 静的生成の範囲

`generateStaticParams` は**直近3シーズンの範囲のみ**を生成し、範囲外は `notFound()` を返す。Pages の1デプロイあたりファイル数上限は 20,000 で、**1ルートにつき5ファイル**が出る（`index.html` + RSC ペイロード4本。工程11a で実測）。直近3シーズンで約15,100ファイル（76%）、5シーズンでは約25,200ファイルで超過する。

**CI で `out/` のファイル数を数え、18,000 を超えたら fail させる**（A-14）。

```bash
find web/out -type f | wc -l   # 18000 以下であること
```

1ルートあたりのファイル数は Next.js のバージョンで変わるため、この検査を省略しない。実測は Phase 0（P0-14）で行う。

5シーズンより古い日付・試合は静的生成せず、Workers API からクライアント fetch で表示する。

---

## 6. テスト設計

### 6.1 リーク検証（最重要）

**検証の向きは「DBを撹乱する」。**

```python
def test_target_game_mutation_does_not_change_features(db, game):
    """対象試合の結果を撹乱しても特徴量が変わらないこと。これが本命。"""
    f1 = build_features(game.id, as_of=game.tipoff_at, ds=export(db))
    mutate_game_result(db, game.id, home_score=200, away_score=0, attendance=99999)
    mutate_team_stats(db, game.id, pts=200)
    f2 = build_features(game.id, as_of=game.tipoff_at, ds=export(db))
    assert keys(f1) == keys(f2)
    assert all(approx_equal(f1[k], f2[k]) for k in f1)


def test_future_game_mutation_does_not_change_features(db, game):
    """as_of 以降の試合を撹乱しても変わらないこと。"""
    f1 = build_features(game.id, as_of=game.tipoff_at, ds=export(db))
    mutate_games_after(db, game.tipoff_at)
    f2 = build_features(game.id, as_of=game.tipoff_at, ds=export(db))
    assert all(approx_equal(f1[k], f2[k]) for k in f1)


def test_as_of_is_actually_applied(db, game):
    """as_of を前にずらすと、結果依存の特徴量が変化すること（陽性確認）。"""
    f_now  = build_features(game.id, as_of=game.tipoff_at, ds=export(db))
    f_past = build_features(game.id, as_of=game.tipoff_at - timedelta(days=30),
                            ds=export(db))
    changed = [k for k in f_now if not approx_equal(f_now[k], f_past.get(k))]
    assert any(k.startswith(("elo_", "winrate_", "margin_")) for k in changed)


def test_backtest_uses_production_data_as_of(db):
    """保存済み feature_snapshot と再構築結果が一致すること。"""
    for p in sample_final_predictions(db, n=50):
        rebuilt = build_features(p.game_id, as_of=p.as_of,
                                 ds=export_at(db, p.data_as_of))
        assert approx_equal_dict(json.loads(p.feature_snapshot), rebuilt)


def test_features_exclude_unfinished_games(db, game): ...
def test_features_exclude_in_progress_game(db, game): ...


def test_accuracy_within_plausible_band(metrics):
    assert metrics.baseline_accuracy < metrics.accuracy < 0.85


def test_sql_spy_captures_bound_parameters():
    """sql_spy がバインド値も記録することの自己テスト。"""
```

**旧版のテストは論理が逆だった。** `as_of` を7日後にずらして「値が変わらないこと」を assert していたが、`as_of` を後ろにずらせば参照できる過去試合が増えるため、正しい実装なら値は変わる。あのテストは正しい実装を落とし、`as_of` を一切見ない実装だけを通していた。

**ミューテーション試験**: 意図的にリークさせた実装（`elo_home` に当該試合の得点差を混入）を用意し、リークテストが**落ちること**を CI で確認する（テストのテスト）。

`sql_spy` はクエリ文字列だけでなくバインドパラメータも記録する。プレースホルダを使えば `game_id` はクエリ文字列に現れず、文字列一致では検出できない。

### 6.2 freeze の検証

```python
def test_finalize_at_tipoff_boundary():        # tipoff-1秒 / ちょうど / +1秒
def test_internal_predictions_rejects_after_tipoff():   # 409、1行も書き込まれない
def test_internal_predictions_rejects_when_is_final():
def test_gameday_update_skips_started_games():
def test_reinference_appends_not_replaces():   # 2行になり旧行が is_active=0
def test_cron_delay_simulation():              # tipoff+5分に実行しても確定予測が不変
def test_update_final_prediction_raises():     # トリガによる拒否

def test_finalize_freezes_parent_and_children_atomically():
    """finalize 後、predictions と player_predictions の is_final がともに 1 で、
    prediction_reasons / prediction_team_targets / prediction_model_bundle への
    UPDATE / DELETE が親参照トリガに拒否されること。"""

def test_finalize_statement_order_is_child_then_parent():
    """batch() の文順が 子 → 親 であること。親を先に立てると子への
    書き込みがトリガに拒否されるため、順序が結果を変える（1.8）。"""
```

### 6.3 冪等性

```python
def test_daily_ingest_twice_produces_identical_db():
    """ingestion_logs を除く全テーブルの行数と内容ハッシュが一致すること。"""
def test_daily_ingest_resumes_after_injected_failure():
def test_evaluate_is_idempotent():             # 3回実行して1行のまま
def test_finalize_is_idempotent():             # 2回目が is_final=1 行を UPDATE しない
def test_upsert_game_preserves_id():           # 日程変更で id が変わらない
def test_recompute_ratings_full_rebuild():     # 遡及投入後の洗い替え
def test_series_game_no_stable_under_reingest():
def test_entries_are_replaced_not_merged():    # 推定行が残らない
```

### 6.4 境界値・異常系

```python
def test_tov_rate_with_null_possessions():     # None を返し、関数内で0埋めしない
def test_oreb_rate_zero_denominator():
def test_efg_zero_fga():
def test_attendance_rate_null_capacity():
def test_postponed_game_does_not_duplicate():
def test_cancelled_game_excluded_from_accuracy():   # outcome='VOID'
def test_rest_days_clipped_across_season_boundary():
def test_winrate_l5_does_not_cross_season_boundary():
def test_elo_season_regression_applied_once():      # 洗い替え2回で二重適用しない
def test_new_club_shows_early_season_badge():       # team_ratings に行がなくても
def test_all_starters_out_probability_clamped():    # [0.05, 0.95] にクランプ
def test_player_avail_below_threshold_excluded():   # availProb < 0.5 は非表示
def test_win_probs_sum_to_one_after_calibration():
def test_displayed_percentages_sum_to_100():        # 丸め後
def test_win_prob_and_score_agree():                # 符号の一致
def test_parse_minutes_mmss() / test_parse_dnp() / test_parse_fullwidth_digits()
def test_parse_box_score_identities()               # pts = 2fgm×2 + 3fgm×3 + ftm
def test_null_score_game_raises_not_silently_zero()
def test_finished_at_estimated_flag_set_when_unavailable()  # tipoff_at + 2h / flag=1
def test_finished_at_null_for_unplayed_games()              # SCHEDULED は NULL
def test_spectator_restricted_null_when_attendance_missing()
def test_spectator_restricted_forced_for_2020_21_and_2021_22()
def test_allstar_game_not_ingested()                        # event=5 が1行も入らない
def test_preseason_and_playin_not_ingested()                # event=4 / 11 / 20 も同じ
def test_competition_check_rejects_unknown_value()          # CHECK 制約
def test_team_games_competition_matches_games()             # 親子で区分が一致する
def test_features_include_both_competitions()               # 規約7（2.1）の陽性確認
def test_returning_club_treated_as_promoted()               # 前季にトップリーグの試合がない
                                                            # クラブは古い Elo を持ち越さない
def test_stadium_cd_normalized_to_string()                  # 整数 3 と文字列 "3" が同一会場
def test_venue_registered_from_game_data()                  # 未知の StadiumCD で venues に登録
def test_venue_source_keys_resolves_official_code()         # 公式ID → venue_id
def test_attendance_rate_null_capacity()                    # capacity が NULL でも落ちない
def test_spectator_restricted_null_when_capacity_missing()  # 通常のホームアドバンテージを使う
def test_seed_master_is_idempotent()                        # 2回流して同一（ON CONFLICT）
def test_club_source_ids_resolve_every_top_tier_team()      # 30件すべてが club_id に解決する
def test_club_slug_format_and_uniqueness()                  # ^[a-z0-9-]{1,40}$ かつ重複なし
def test_season_range_contains_every_game_date()            # 範囲が実試合日を必ず含む
def test_masters_endpoint_rejects_unknown_array()           # 汎用テーブル指定を受け付けない
def test_elo_uses_home_advantage_when_restriction_unknown() # NULL は 0 にしない
```

### 6.4.1 個人スタッツの整合性（必須）

```python
def test_team_targets_are_feasible(game):
    """チーム目標が制約と恒等式を満たすこと。整合化の前提条件。"""
    for t in team_targets(game):                       # home / away の2行
        assert t.fg2a >= 0 and t.fg3a >= 0 and t.fta >= 0
        assert 0 <= t.fg2_pct <= 1 and 0 <= t.fg3_pct <= 1 and 0 <= t.ft_pct <= 1
        # 成功数 <= 試投数 は「率 × 試投数」という持ち方から構造的に成立する
        assert t.fg2_pct * t.fg2a <= t.fg2a
        assert t.fg3_pct * t.fg3a <= t.fg3a
        assert t.ft_pct  * t.fta  <= t.fta


def test_team_targets_match_predicted_score(game):
    """チーム目標の導出得点が予想スコアと一致すること（2.4 前段の帰結）。

    reconcile_team_targets() を通していなければ、ここで落ちる。
    """
    for t in team_targets(game):                       # side ごとに独立に検証する
        pts = t.fg2_pct * t.fg2a * 2 + t.fg3_pct * t.fg3a * 3 + t.ft_pct * t.fta
        assert abs(pts - team_pred(game)[t.side]) < 0.5


def test_team_reconcile_raises_on_infeasible():
    """到達不能な予想スコアに対して無言でクリップせず InfeasibleTargetError を
    投げること（pred_score が (0, 2*A2 + 3*A3 + Af) の外にある場合）。"""


def test_team_targets_attempts_unchanged_by_reconcile():
    """前段の整合化が試投数を動かさないこと。動かすのは成功率3項目だけ。"""


def test_player_predictions_reconcile_to_team(game):
    """選手予測の合計がチーム予測と一致すること。

    検証は「1チームあたり」で行う。総出場時間は 5人 x 40分 = 200分であり、
    1試合分（両チーム）を合計すると 400分になる。side をまたいで合計しない。
    """
    for side in ("home", "away"):
        ps = player_predictions(game, side=side)
        assert abs(sum(p.pts for p in ps) - team_pred(game)[side]) < 0.5
        assert abs(sum(p.minutes for p in ps) - 200.0) < 0.5    # 1チームあたり
        for s in ('fg2a', 'fg3a', 'fta', 'oreb', 'dreb', 'ast', 'tov',
                  'stl', 'blk', 'pf', 'fd'):
            assert rel_error(sum(getattr(p, s) for p in ps),
                             team_target(game, side)[s]) < 0.02
        # 成功数はチーム目標の成功数に一致する（ロジットシフトの帰結）
        for pct, att in (('fg2_pct','fg2a'), ('fg3_pct','fg3a'), ('ft_pct','fta')):
            made = sum(getattr(p, pct) * getattr(p, att) for p in ps)
            tgt  = team_target(game, side)
            assert rel_error(made, tgt[pct] * tgt[att]) < 0.02


def test_player_prediction_identities(game):
    """恒等式と大小関係が予測値でも成立すること。"""
    for p in player_predictions(game):
        assert approx(p.pts, p.fg2m * 2 + p.fg3m * 3 + p.ftm)
        assert approx(p.fgm, p.fg2m + p.fg3m)
        assert approx(p.fga, p.fg2a + p.fg3a)
        assert approx(p.reb, p.oreb + p.dreb)
        # 成功数は率×試投数の導出値なので、構造的に必ず成立する
        assert p.fg2m <= p.fg2a and p.fg3m <= p.fg3a and p.ftm <= p.fta
        assert all(0 <= getattr(p, k) <= 1
                   for k in ('fg2_pct', 'fg3_pct', 'ft_pct'))
        assert all(getattr(p, s) >= 0 for s in COUNT_STATS)


@pytest.mark.parametrize("seed", range(1500))
def test_reconciliation_converges(seed):
    """1,500ケースのランダム入力で3回反復後の誤差が許容内に収まること。

    実測の基準値: 項目誤差の99%点 0.27%、最大 1.87%、得点誤差 0.00000。
    """
    players, target = random_case(seed)
    m, x, pct, pts = reconcile(players, target, iters=3)
    assert abs(pts.sum() - target_points(target)) < 0.5
    for s in ATTEMPTS + COUNTS:
        assert rel_error(x[s].sum(), target[s]) < 0.02


def test_reconciliation_no_clipping():
    """整合化後の成功率が [0,1] を出ないこと。クリップに頼っていないことの確認。"""


def test_reconciliation_raises_on_infeasible_target():
    """実行不能な目標に対して無言でクリップせず InfeasibleTargetError を投げること。"""


def test_infeasible_target_discards_players_but_keeps_team_prediction(game):
    """InfeasibleTargetError の試合で、チーム予測は保存され個人スタッツが
    1行も保存されないこと。ジョブが止まらないこと（4.2）。"""


def test_no_plus_minus_prediction_column():
    """＋/－ の予測列・API フィールドが存在しないこと。"""


def test_percentage_hidden_below_attempt_threshold():
    """試投数が閾値未満のとき pct が null で返ること。"""


def test_count_rates_learned_per_minute():
    """カウント11項目の目的変数が per-minute であること。"""


def test_pct_models_not_per_minute():
    """成功率3項目が per-minute で学習されていないこと（率は時間当たりの量ではない）。"""


def test_overtime_minutes_not_assumed():
    """予測時点で延長を仮定せず、総出場時間が 200 分であること。"""
```

#### 凍結の網羅（子テーブルにも例外を設けない）

```python
def test_player_predictions_frozen_when_parent_final():
    """親が is_final=1 のとき player_predictions の UPDATE/DELETE が拒否されること。"""


def test_prediction_reasons_frozen_when_parent_final():
    """同上。根拠が後から差し替えられるなら不変性の主張が成立しない。"""


def test_team_targets_frozen_when_parent_final():
def test_model_bundle_frozen_when_parent_final():


def test_freeze_transition_is_allowed():
    """is_final 0→1 の遷移自体は通ること（トリガが freeze を妨げないこと）。"""
```

#### スナップショットと無料枠

```python
def test_snapshot_matches_d1(db):
    """スナップショットの行数と主キー集合が D1 と一致すること。"""


def test_snapshot_manifest_hashes():
    """MANIFEST.json の SHA256 が実ファイルと一致すること。"""


def test_training_reads_no_d1(monkeypatch):
    """学習パスで D1 クライアントが一度も呼ばれないこと。"""


def test_batch_size_within_query_limit():
    """各テーブルの max_rows_per_request が floor(100/列数)×40 以下で、
    1リクエストあたりの文数が 50 以下に収まること。"""


def test_batch_limits_match_schema():
    """batch-limits.ts の列数が db/migrations/*.sql の全列数と一致すること。

    列を1つ足したときに定数だけ古いまま残ることを防ぐ。v1.3 では表の列数が
    DDL と3テーブルでずれており、games / team_game_stats の上限が
    200（正しくは160）になっていた。"""


def test_artifact_size_guard():
    """1.5MB を超える artifact_text の登録が拒否されること。"""


def test_static_export_file_count():
    """out/ のファイル数が 18,000 以下であること。"""
```

極端な欠場（`minutes_lost` が学習域外）に対しては、モデルではなく**後処理で確率を `[0.05, 0.95]` にクランプする**。外挿域では較正も効かず、「ホーム 100% – アウェイ 0%」と表示して外すと信頼が一度で失われる。

### 6.5 回帰

```python
def test_feature_list_diff_is_reported():      # 新旧の差分と欠損率をログ出力
def test_golden_predictions_within_tolerance():# 代表100試合、|p_new - p_old| > 0.15 が10件超で失敗
def test_brier_compared_on_identical_window():
def test_calibration_error_definition():       # ECE 等頻度10ビンの定義を固定
def test_only_one_active_model_per_type():     # uq_model_active
def test_rollback_restores_previous_model_version():
def test_deterministic_training():             # 同一入力・同一seedで予測が完全一致
```

### 6.6 パーサ

`tests/fixtures/` には**構造のみを模し、値をダミーに置換した合成データ**を置く（埋め込みJSONを含む。4.4）。実サイトの文言・記事・画像参照を残さない（要件4.5.2）。審判名とプレイバイプレイは fixture にも含めない。CI で実サイト由来文字列の混入を検査する。

fixture はスナップショットであるため、定義上サイト構造の変更を検知できない。検知は `parser-canary`（日次、実サイトの日程ページ1枚）が担う。

### 6.7 API・画面

| 対象 | 内容 |
|---|---|
| API | Zod 契約、Bearer なしで401、tipoff 経過後に409、キャッシュヘッダ、入力検証で400。**テストは Workers ランタイム（`@cloudflare/vitest-pool-workers`）で走らせる** — `crypto.subtle.timingSafeEqual` と D1 の `batch()` は Workers の API であり Node 上では検証できない。スキーマは `readD1Migrations('../db/migrations')` で読み、テスト側に DDL を書き写さない |
| トークン | コントラスト比 4.5:1 以上の**単体テスト**（目視では見落とす） |
| E2E | `test_theme_persists_across_reload`、`test_no_horizontal_scroll_at_375px`、`test_disclaimer_reachable_from_every_page` |

画面スナップショットは1画面×2テーマに絞る。3画面×2テーマは、トークンを1つ変えるたびに6枚落ちる維持コストが個人開発に見合わない。

### 6.8 テストデータ

`db/seeds/test/` に架空8チーム × 2シーズン（約300試合）の決定論的シードを置く。実在選手名を固定しない。pytest のスキーマは `db/migrations/*.sql` をそのまま in-memory SQLite に適用する（二重管理するとテストが本番を反映しなくなる）。`test_migrations_apply_cleanly` でマイグレーション自体も検証する。

---

## 7. セキュリティ詳細

### 7.1 public リポジトリでの禁止事項

- Cloudflare API トークン、`INGEST_TOKEN` / `FINALIZE_TOKEN` の値
- `.env` の実ファイル（`.env.example` のみ可）
- **取得した生HTML**（要件4.5.2）
- 学習済みモデルバイナリ（D1 に格納する）

`.gitignore`:

```
.env
.env.local
.dev.vars
*.db
*.sqlite
batch/cache/
data/raw/
**/fixtures/**/*.html
*.html.gz
models/*.pkl
```

`D1_DATABASE_ID` は機密ではない（操作には API トークンが別途必要）ため `wrangler.toml` に直書きしてよい。`d1_databases` の `database_id` は必須フィールドで環境変数注入が効かないため、旧版の「直書きしない」という制約は実現不能だった。

### 7.2 認証

```ts
const constantTimeEq = async (a: string, b: string) => {
  const enc = new TextEncoder();
  const [ha, hb] = await Promise.all([
    crypto.subtle.digest("SHA-256", enc.encode(a)),
    crypto.subtle.digest("SHA-256", enc.encode(b)),
  ]);
  return crypto.subtle.timingSafeEqual(ha, hb);   // 長さが揃うのでハッシュ経由
};

const auth = (secretKeys: string[]) => async (c, next) => {
  const m = /^Bearer\s+(\S+)$/.exec(c.req.header("Authorization") ?? "");
  if (!m) return c.json({ error: { code: "UNAUTHORIZED", message: "..." } }, 401);
  for (const k of secretKeys) {
    if (c.env[k] && await constantTimeEq(m[1], c.env[k])) return next();
  }
  return c.json({ error: { code: "UNAUTHORIZED", message: "..." } }, 401);
};

app.use("/internal/predictions", auth(["INGEST_TOKEN", "INGEST_TOKEN_NEXT"]));
app.use("/internal/finalize",    auth(["FINALIZE_TOKEN"]));
```

`.replace("Bearer ", "")` は `Bearer ` がない場合もヘッダ全体を返すため、スキーム検証にならない。

### 7.3 漏洩経路の遮断

| 経路 | 対策 |
|---|---|
| Actions のログ（public で全世界から閲覧可） | 例外オブジェクトをそのまま出力しない |
| `ingestion_logs.error_message` | 型名と自前メッセージのみ。URLはホストとパスのみでマスク |
| `/api/v1/health` | `error_message` を返さない |
| 依存パッケージ | `permissions: contents: read`、Dependabot、`==` 固定 |
| push | Secret scanning + Push protection（push 時点で止まる） |

---

## 8. 運用手順

### 8.1 初期構築

```bash
# 1. D1 作成（ロケーションヒントを apac に）
wrangler d1 create bpredict --location apac

# 2. マイグレーション
wrangler d1 migrations apply bpredict --local     # ローカル検証
wrangler d1 migrations apply bpredict --remote

# 3. API デプロイ（backfill より先。書き込み経路が /internal/* のため）
cd api && wrangler deploy

# 4. マスタ投入（冪等。ingestion_logs に job='seed_master' で記録）
#    事前に列挙できるものだけ。club_seasons と会場マスタは backfill が作る（1.2 / 4.4）
python -m batch.jobs.seed_master

# 5. 過去データ取り込み（1日1シーズン。再開可能）
python -m batch.jobs.backfill --season 2016-17-B1

# 6. 全期間のレーティング洗い替え
python -m batch.jobs.recompute_ratings --full

# 7. 初回学習
python -m batch.jobs.train --initial

# 8. Web デプロイ（Pages は Git 連携で自動）
```

**実装順序の都合で、API デプロイが backfill より先に来る。** 書き込み経路を `/internal/*` に一本化したため、API がないと backfill が実行できない。

### 8.2 ローカル開発

```bash
# ローカル D1 にテストシードを投入
wrangler d1 execute bpredict --local --file=db/seeds/dev/seed.sql

# API をローカル起動
cd api && npm run dev

# バッチをローカル API に向ける
API_BASE_URL=http://127.0.0.1:8787 INGEST_TOKEN=dev \
  python -m batch.jobs.daily_ingest --dry-run
```

本番 D1 に書きながら開発しない。`is_final` を含む本番データを壊すリスクを常時抱えることになる。

### 8.3 障害対応

| 症状 | 確認 | 対応 |
|---|---|---|
| 更新遅延バナーが出る | `/api/v1/health` | Actions のログ |
| `parser-canary` が fail | 対象URLのHTML | 合成 fixture を作りテストを書いてから修正 |
| パースエラー多発 | `ingestion_logs.error_type` | 同上 |
| 予測が出ない | `model_versions.is_active` | 有効モデルの存在確認 |
| 精度が急落 | `/accuracy` の推移、`degraded` フラグ | 直前のモデル変更を確認。ロールバック |
| Workers 429 | `/health` の `quota` | キャッシュTTL延長、WAFルール確認 |
| 中止試合でバッチ失敗 | `prediction_results.outcome` | `VOID` として記録されているか確認 |

### 8.4 モデルのロールバック

```sql
UPDATE model_versions SET is_active = 0
 WHERE model_type = 'WINNER' AND target = '' AND league = 'PREMIER';
UPDATE model_versions SET is_active = 1 WHERE version = 'winner-v1.0.0';
```

`uq_model_active` により、この順序でのみ成功する。確定済み予測は変更しない。

---

## 9. 実装順序

| 順 | 作業 | 完了条件 |
|---|---|---|
| 0 | **Phase 0 の検証**（P0-1〜P0-10）と未決事項 U-01 / U-05 / U-06 の確定 | 取得可否と名称が決まる |
| 0b | **実機検証**（P0-12 LightGBM サイズ / P0-13 `batch()` クエリ計上 / P0-14 Next.js ビルド / P0-15 Zod コスト） | 設計の前提が数値で裏付けられる。外れた項目は設計を修正してから先へ進む |
| 1 | D1 スキーマとマイグレーション（トリガ・CHECK を含む） | `migrations apply` が成功、`test_migrations_apply_cleanly` が通る |
| 2 | マスタ整備。`db/seeds/master/*.csv`（`seasons` 11行 / `clubs` 30行 / `club_source_ids` 30行）と `seed_master` の実装。**事前に列挙できるものに限定する** — `club_seasons` と会場マスタは含めない（1.2 / 4.4） | CSV を in-memory SQLite に適用して、旧B1と新リーグのクラブが `club_source_ids` で紐付くことをテストで確認できる。**D1 への投入は工程4b（`POST /internal/masters`）で完了した** — ローカル D1 に 11 / 30 / 30 行が入り、公式ID30件すべてが `club_id` に解決し、2回流しても行数が増えないことを実機で確認済み |
| 3 | CI の構築（`ci.yml`、`parser-canary.yml`、`dependabot.yml`、リポジトリ検査）。**api / web のジョブは置くが、該当ディレクトリができた時点で有効になる形にする** | push で `ruff` / `mypy` / `pytest` と fixtures 検査（A-12）が走る。全ワークフローに `permissions: contents: read`・`concurrency`・`timeout-minutes` があり、`next lint` がないことをテストで固定できている。**ESLint（`eslint .`）は最初の TypeScript が入る工程4で有効になる** |
| 4a | **Workers API の土台と `POST /internal/masters`。** Hono / Zod / vitest（Workers ランタイム）/ ESLint Flat Config / `wrangler.toml` / `batch-limits.ts` / Bearer 認証（2キー方式・定数時間比較） | Bearer なしで401、汎用テーブル指定で400、`test_batch_size_within_query_limit` と `test_batch_limits_match_schema` が通る。`POST /internal/masters` がローカル D1 にマスタを投入でき、2回流しても行数が増えない |
| 4b | 残りの `/internal/*`（`predictions` / `finalize` / `evaluate` / `summary` / `log` / `ratings` / `games` / `stats` / `entries` / `models` と GET 群）。**freeze の Cron Trigger（毎時）もここで置く** | tipoff 経過後に409、freeze が子 → 親の順で通る、`GET /internal/games/ingested`（工程6が使う）と `GET /internal/models/active`（工程9が使う）が応答する |
| 5 | スクレイパとパーサ（値域検証を含む） | 合成 fixture でテストが通る。**完了した** — `batch/scraper/`（HTTP・取得前確認・URL構築）と `batch/parser/`（日程・終了済みボックススコア）を**標準ライブラリのみ**で実装し、batch のテストは 371 件。取得前確認は robots / 利用規約のハッシュが未設定・不一致なら試合データを取得しない。日次3,000件と 429/503 の停止はプロセス再起動を跨いで保たれる。**実サイトの取得は `SCRAPER_USER_AGENT` / `SCRAPER_ROBOTS_SHA256` / `SCRAPER_TERMS_SHA256` を設定するまで行わない**（カナリアは警告を残してスキップする）。利用方法は [スクレイパ・パーサ](scraper-parser.md) |
| 6 | backfill による過去データ取り込み **＋ `club_seasons` と会場マスタの構築 ＋ スナップショット書き出し**（**進行中**: `batch/loader/` と `batch/jobs/backfill.py` を実装し、合成応答でテスト済み。`recompute_ratings` とスナップショット書き出しジョブ、`venue_revisions` の構築は未実装）。会場は `StadiumCD` を見て未知なら `venues` に登録してから試合を入れる。座標と収容人数は CSV から後入れする。`venue_revisions` の作り方は 1.2 で確定済み（U-10 解決）。**着手前に、運営者が `robots.txt` と利用規約を確認して `SCRAPER_ROBOTS_SHA256` / `SCRAPER_TERMS_SHA256` を設定する必要がある**（未設定では取得しない。docs/scraper-parser.md） | 全シーズンが DB に入り、`test_snapshot_matches_d1` が通る |
| 7 | 特徴量生成とリーク検証テスト（入力はスナップショット） | DB撹乱法のテストが通る。ミューテーション試験も通る。`test_training_reads_no_d1` が通る。**完了した** — `batch/features/`（`dataset` / `base` / `team_strength` / `schedule_ctx` / `player` / `builder`）と採用16キー、`db/seeds/test/`（架空8クラブ × 2シーズン / 224試合の決定論的シード）、`scripts/rebuild_snapshot.py`。batch のテストは 404 件で、リーク検証10件・スナップショット9件・特徴量10件を含む |
| 8 | 勝敗モデルの学習と評価（**経路A・Bの両方**）。**P0-11**（採用経路と σ の実測）と **P0-16**（ECE ノイズフロアを実データの予測分布で再計算）をここで消化する | Elo単体ロジスティック回帰を Brier で上回る。P0-11 で採用経路と `margin_sigma` が決まり、P0-16 で ECE ゲートの閾値が確定する |
| 9 | 推論と predictions 登録、静的JSON書き出し（**着手前に未決事項 U-09「静的JSON の全体像」を確定させる**） | 予測が JSON に出る |
| 10 | 公開API（動的クエリ） | `/games?date=` `/accuracy` が応答する |
| 11a | **デザインシステムと画面の骨組み。** トークン（6.1）とコントラストの CI 検証、テーマ切替、マークアップ規約（5.2）に沿った主要コンポーネント、各画面の骨組み。データは**合成データ**を使う | `/` が 375px で横スクロールなく表示され、ライト/ダークが切り替わって再訪時も保持される。トークンのコントラスト検証と `out/` のファイル数検査（18,000以下）が CI で通る |
| 11b | 画面を実データへ結線する（今日の予測・試合詳細・結果） | 予測と結果が表示される |
| 12 | Margin/Total モデル | 予想スコアが出る。勝率と矛盾しない |
| 12b | **TeamRate モデル**（整合化の目標値）と **`reconcile_team_targets()`**（予想スコアへの前段整合化） | `test_team_targets_are_feasible` と `test_team_targets_match_predicted_score` が通る |
| 12c | 個人モデル4段（Avail → Minutes → Rates → 整合化） | フルボックススコアが出る。`test_reconciliation_converges` と `test_player_predictions_reconcile_to_team` が通る |
| 13 | SHAP のグループ集約 | 根拠が4グループで表示される |
| 14 | 的中率ページ（較正の言い換えを含む） | `accuracy_summary` から表示される |
| 15 | テーマ・PWA・免責・プライバシーポリシー | 受け入れ基準 A-01〜A-18 をすべて満たす |

**工程11 を 11a と 11b に分け、11a を工程6〜10 より先に実施した**（運営者の指示。2026-09-22）。見た目を早く確認できることに価値があり、11a は取得・学習・推論に依存しない。ただし **11a で静的JSON のスキーマを決めない** — U-09 は未決であり、画面が読むファイル構成を推測で作ると、工程9で作る側がそれに縛られる。11a のデータは合成データに限り、実データへの結線は U-09 の確定と工程9・10 の完了を待つ。

工程0で取得できないと判明した項目は、該当する特徴量を無効化して先に進む。工程を止めない。

**工程3の時点ではリントする TypeScript が存在しない。** ESLint の Flat Config は最初の TypeScript が入る工程4（`api/`）で置き、工程11（`web/`）でもう1つ置く。CI 側は工程3で `api` / `web` のジョブを**書いておき**、`detect` ジョブが `package.json` の有無を出力して分岐させる。ディレクトリができた時点で自動的に有効になるため、**後から CI に足し忘れることがない**。

`hashFiles()` を job 単位の `if` に書く方法は使えない。checkout より前に評価されるためワークスペースが空で、常に偽になる。

**工程 12b を 12c より先に置く。** 整合化はチーム側の目標値がなければテストすらできず、目標値が制約を満たしていなければ原理的に成立しない。個人モデルから作ると、整合化の実装時に「目標が実行不能」という問題を個人モデル側の欠陥と誤認する。

旧版では工程3（取得可否の検証）がスキーマ確定より後にあり、取得できない項目が判明した時点で確定済み DDL を直すことになっていた。また backfill が API 構築より前にあり、書き込み経路の一本化と矛盾していた。

-- 実績の集計（詳細設計 1.9）。戦績・スタッツの閲覧（要件 F-10 / F-15）が読む。
--
-- **実行時に集計しない。** 1選手の通算を `player_game_stats` から引くと、索引
-- `idx_pgs_player` が効いても **その選手の行（最大613行）＋ `games` への JOIN** を
-- 読む。1ページあたり約1,200行で、**D1 の読取枠（500万行/日）は1日4,000ページ閲覧で
-- 尽きる**。畳んでおけば1ページ2クエリ・数十行になる。
--
-- **`accuracy_summary` と同じ型である**（`scope` / `scope_key` / 主キーに NULL を
-- 含めない）。新しい型を発明しない。
--
-- **洗い替えない。upsert だけで更新する**（基本設計 3.2）。約4,650行は1リクエスト
-- （160行）に収まらず、分割すると後のリクエストの DELETE が前のリクエストで入れた行を
-- 消す。upsert で足りる根拠は、取り込みが試合を `id` で upsert して**削除しない**
-- ことであり、「集計すると行が1つ減る」経路がない。
--
-- **トリガを置かない。** 予測（0008）と違い、この2表は事実の集計であって不変の
-- 主張をしていない。試合の結果が訂正されれば集計も変わるべきである。

-- 選手の実績。保存するのは合計で、1試合平均は API が導出する（導出値を冗長に
-- 持つと不整合の余地が生まれる）。
CREATE TABLE player_stat_summary (
  player_id TEXT NOT NULL REFERENCES players(id),
  scope     TEXT NOT NULL CHECK (scope IN ('SEASON','CAREER')),
  scope_key TEXT NOT NULL,
  -- **季の行はクラブ別に持つ。** 季中の移籍が2行で出る。CAREER では空文字。
  -- **NULL にしない** — SQLite は主キー列の NULL 重複を許す（0006 の
  -- accuracy_summary.model_version と同じ理由）
  club_id   TEXT NOT NULL DEFAULT '',
  games         INTEGER NOT NULL CHECK (games >= 0),
  games_started INTEGER NOT NULL DEFAULT 0 CHECK (games_started >= 0),
  minutes REAL,
  fg2m INTEGER, fg2a INTEGER,
  fg3m INTEGER, fg3a INTEGER,
  ftm  INTEGER, fta  INTEGER,
  oreb INTEGER, dreb INTEGER,
  ast INTEGER, tov INTEGER, stl INTEGER, blk INTEGER,
  pf INTEGER, fd INTEGER,
  pts INTEGER,
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (player_id, scope, scope_key, club_id),
  -- CAREER に季を、SEASON に空を入れさせない。主キーの意味を守る
  CHECK ((scope = 'CAREER' AND scope_key = '' AND club_id = '')
      OR (scope = 'SEASON' AND scope_key <> '' AND club_id <> ''))
);

-- クラブページの当季の選手一覧をこの索引で引く（約16行）
CREATE INDEX idx_pss_club ON player_stat_summary(scope, scope_key, club_id);

-- クラブの実績。**母数を2つ持つ**（games と stat_games）。
-- `team_game_stats` が欠ける試合が実在するため（取り込みが games と stats を
-- 続けて投げ、その間で失敗すると試合行だけが残る）、1つに畳むと母数が嘘になる。
CREATE TABLE team_stat_summary (
  club_id   TEXT NOT NULL REFERENCES clubs(id),
  scope     TEXT NOT NULL CHECK (scope IN ('SEASON','CAREER')),
  scope_key TEXT NOT NULL,
  -- 勝敗・得点の母数（team_games で result IS NOT NULL の試合数）
  games INTEGER NOT NULL CHECK (games >= 0),
  wins  INTEGER NOT NULL CHECK (wins >= 0),
  -- `losses` の列を持たない（games - wins で導出できる冗長列）
  points_for     INTEGER,
  points_against INTEGER,
  -- ボックススコアの母数（team_game_stats に行がある試合数）
  stat_games INTEGER NOT NULL DEFAULT 0 CHECK (stat_games >= 0),
  fg2m INTEGER, fg2a INTEGER,
  fg3m INTEGER, fg3a INTEGER,
  ftm  INTEGER, fta  INTEGER,
  oreb INTEGER, dreb INTEGER,
  ast INTEGER, tov INTEGER, stl INTEGER, blk INTEGER,
  pf INTEGER, fd INTEGER,
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (club_id, scope, scope_key),
  CHECK ((scope = 'CAREER' AND scope_key = '')
      OR (scope = 'SEASON' AND scope_key <> '')),
  CHECK (wins <= games)
);

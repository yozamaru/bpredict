// 静的JSON と公開API からの取得（基本設計 2.5 / 詳細設計 3.7）。
//
// **主要導線は Workers も D1 も経由させない**（要件 4.2）。当日の一覧と詳細は
// Pages の静的アセット（`/data/*.json`）を読み、過去と集計だけ API に行く。
//
// **ビルド時に埋め込まない**（基本設計 5.6）。バッチが静的JSON を更新しても
// 再ビルドを要らなくするためで、したがって取得はクライアントで行う。
//
// **同一オリジンである**（詳細設計 3.5）。`/data/...` も `/api/v1/...` も相対パスで
// 叩けるため、環境ごとの分岐を持たない。

/** 取得に失敗した。**画面はエラーの空状態を出す**（要件 8.5）。 */
export class DataError extends Error {}

/**
 * JSON を取る。**`{ data, meta }` の `data` を返す**（詳細設計 3.1）。
 *
 * **`cache: 'no-store'` を付ける**（基本設計 5.6）。静的JSON は1日4回書き換わり、
 * ブラウザキャッシュに残ると古い予測が出続ける。
 */
async function load<T>(path: string): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, { cache: 'no-store' });
  } catch {
    // **例外の本文を持ち回らない**（絶対ルール4。URL が混ざる）
    throw new DataError('取得に失敗した');
  }
  if (!response.ok) {
    // ステータスは数値1つであり、伏せる理由がない（詳細設計 4.4 と同じ判断）
    throw new DataError(`応答が ${response.status}`);
  }
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    throw new DataError('JSON として読めない');
  }
  if (typeof body !== 'object' || body === null || !('data' in body)) {
    throw new DataError('応答に data がない');
  }
  return (body as { data: T }).data;
}

/** 当日の一覧。**`data.gameDate` が「何日ぶんか」を持つ**（詳細設計 3.7）。 */
export const fetchToday = () => load<GamesByDate>('/data/today.json');

/** 窓の中の日付。`today.json` と同じ形である。 */
export const fetchSchedule = (date: string) =>
  load<GamesByDate>(`/data/schedule/${date}.json`);

/** 窓の外の日付は公開APIへ行く（基本設計 2.5）。 */
export const fetchGamesByDate = (date: string) =>
  load<GamesByDate>(`/api/v1/games?date=${date}`);

/** 当日の試合詳細。 */
export const fetchGameDetail = (gameId: string) =>
  load<GameDetail>(`/data/games/${gameId}.json`);

/** 窓の外の試合は公開APIへ行く。 */
export const fetchGameFromApi = (gameId: string) =>
  load<GameDetail>(`/api/v1/games/${gameId}`);

export const fetchMeta = () => load<Meta>('/data/meta.json');

export const fetchResults = (date: string) =>
  load<ResultsByDate>(`/api/v1/results?date=${date}`);

export const fetchAccuracy = () => load<AccuracySummary>('/api/v1/accuracy');

export const fetchTeam = (slug: string) => load<Team>(`/api/v1/teams/${slug}`);

/**
 * 当季のクラブ一覧（要件 8.2 / 詳細設計 3.3）。**並びは API が返す順をそのまま使う**
 * （`clubs.slug` 昇順）。画面で並べ替えない — API と食い違ったときにどちらが正かが
 * 決まらなくなる。
 */
export const fetchTeams = () => load<TeamList>('/api/v1/teams');

/** 選手別（要件 F-15 / 詳細設計 3.3）。**集計値だけが返る。** */
export const fetchPlayer = (playerId: string) =>
  load<Player>(`/api/v1/players/${playerId}`);

// --- 契約ファイル（`contracts/public-shapes.json`）の形 ---
//
// **キーはあの契約が正である。** api 側と batch 側のテストが同じファイルを読んで
// いるため、ここを勝手に変えると画面だけが別の形を期待することになる。

export type ClubShape = {
  clubId: string;
  slug: string;
  /** **当季は `club_seasons` が埋まるまで null になる**（詳細設計 4.2 のステップ5） */
  name: string | null;
  shortName: string | null;
};

export type PredictionShape = {
  homeWinProb: number;
  predHomeScore: number;
  predAwayScore: number;
  isProvisional: boolean;
  isFinal: boolean;
  /** **推測で false を入れない。** 消化試合数の集計がないため null になる */
  isEarlySeason: boolean | null;
  modelVersion: string;
};

export type GameShape = {
  gameId: string;
  /** **UTC のまま来る**（CLAUDE.md 時刻の扱い）。JST への変換は画面が行う */
  tipoffAt: string | null;
  status: string;
  competition: string;
  home: ClubShape;
  away: ClubShape;
  /** **試合ごと省略しない。** 日程に載っているのに予測がない状態は実在する */
  prediction: PredictionShape | null;
  /**
   * 実際のスコア（v1.131）。**終了していなければ null。**
   *
   * **古い配信物にはキーが無い。** この画面を配った直後、次の `daily_ingest` が
   * 書くまでは `today.json` にこのキーが入っていない（`latestResultDate` と同じ。
   * 詳細設計 5.6）。したがって省略可で受ける。
   */
  homeScore?: number | null;
  awayScore?: number | null;
  /** 照合の結果。**終了してもすぐには付かない**（基本設計 4.1） */
  evaluation?: { isCorrect: boolean | null; scoreError: number | null } | null;
};

export type AccuracyShape = {
  accuracy: number;
  brier: number;
  n: number;
} | null;

export type GamesByDate = {
  gameDate: string;
  games: GameShape[];
  accuracy: AccuracyShape;
};

/** この予測に使った項目（詳細設計 2.7.2）。**寄与を持たない。** */
export type FactorShape = {
  group: string;
  label: string;
  value: string;
  /** **「有利な側」ではない。** 向きを持たない列は null */
  larger: string | null;
};

export type ReasonShape = {
  group: string;
  label: string;
  value: string;
  favors: string;
  strength: number;
};

export type GameDetail = {
  game: GameShape & {
    league: string | null;
    venue: { name: string | null; isPrimary: boolean } | null;
    homeScore: number | null;
    awayScore: number | null;
  };
  prediction: (PredictionShape & {
    reasons: ReasonShape[];
    /**
     * この予測に使った項目（詳細設計 2.7.2）。
     *
     * **古い配信物にはキーが無い。** この画面を配った直後、次の `daily_ingest` が
     * 書くまでは入っていない（`homeScore` と同じ。5.6）。省略可で受ける。
     */
    factors?: FactorShape[];
  }) | null;
  evaluation: {
    outcome: string;
    isCorrect: boolean | null;
    scoreError: number | null;
    bucketContext: { bucket: string; n: number; correct: number; rate: number } | null;
  } | null;
  playerPredictions: unknown[];
  /** その試合の実績（詳細設計 3.3）。**予測の有無に依存しない** */
  playerActuals: unknown[];
  recentForm: unknown;
  modelAccuracy: AccuracyShape;
};

export type ResultsByDate = {
  gameDate: string;
  results: {
    gameId: string;
    tipoffAt: string | null;
    home: ClubShape;
    away: ClubShape;
    homeScore: number | null;
    awayScore: number | null;
    prediction: PredictionShape | null;
    evaluation: {
      isCorrect: boolean | null;
      scoreError: number | null;
      bucketContext: { bucket: string; n: number; correct: number; rate: number } | null;
    } | null;
  }[];
};

export type Meta = {
  generatedAt: string;
  dataAsOf: string | null;
  /** **`status = 'SUCCESS'` の最新。** これから24時間で遅延と判定する（詳細設計 3.7） */
  lastSuccessAt: string | null;
  /**
   * **`/results`（引数なし）が既定で見る日**（詳細設計 3.7）。
   *
   * バッチが「照合した最も新しい試合日」を書く。**画面は時計を見ない** —
   * 静的配信は「いま」を知らず、時刻で変わる表示はキャッシュと噛み合わない。
   * 1試合も照合していない間は null（画面は空状態を出す。要件 8.5）。
   */
  latestResultDate: string | null;
  lastRunStatus: string;
  modelVersions: string[];
};

export type AccuracySummary = {
  // `scoreMae` は**1チームあたりの平均絶対誤差**（基本設計 5.2）。母数は `n` と同じで、
  // 集計側が母数の食い違いを見つけたときだけ null になる（詳細設計 4.12）
  overall: {
    accuracy: number; brier: number; n: number;
    baselineAccuracy: number | null; scoreMae: number | null;
  } | null;
  bySeason: {
    seasonId: string; accuracy: number; brier: number; n: number;
    scoreMae: number | null;
  }[];
  byModel: {
    modelVersion: string; accuracy: number; brier: number; n: number;
    scoreMae: number | null;
  }[];
  calibration: { bucket: string; predicted: number; actual: number | null; n: number }[];
  byProvisional: {
    provisional: { accuracy: number; n: number } | null;
    confirmed: { accuracy: number; n: number } | null;
  };
};

export type TeamList = {
  seasonId: string;
  teams: { clubId: string; slug: string; name: string | null; shortName: string | null }[];
};

export type Team = {
  club: { name: string | null; shortName: string | null; slug: string };
  record: { wins: number; losses: number } | null;
  last5: string[];
  avgMargin: number | null;
  elo: number | null;
  accuracy: { accuracy: number; n: number } | null;
  history: {
    gameId: string;
    gameDate: string;
    isHome: boolean;
    opponent: { name: string | null; shortName: string | null };
    ownWinProb: number;
    ownScore: number | null;
    opponentScore: number | null;
    isCorrect: boolean | null;
  }[];
  /** v1.118 で足した3つ（要件 F-10）。出典は集計テーブル（詳細設計 1.9）。 */
  seasons: TeamSeasonStat[];
  career: TeamSeasonStat | null;
  roster: RosterEntry[];
};

/**
 * 実績の集計（詳細設計 1.9 / 3.3）。**合計ではなく1試合平均が返る** — 導出は
 * API が行い、クライアントで計算しない（実装ごとにずれる）。
 */
export type Shots = { m: number | null; a: number | null; pct: number | null };

export type StatBox = {
  fg: Shots;
  fg2: Shots;
  fg3: Shots;
  ft: Shots;
  oreb: number | null;
  dreb: number | null;
  ast: number | null;
  tov: number | null;
  stl: number | null;
  blk: number | null;
  pf: number | null;
  fd: number | null;
};

export type PlayerStat = {
  games: number;
  gamesStarted: number;
  perGame: {
    minutes: number | null;
    pts: number | null;
    reb: number | null;
    ast: number | null;
  };
  box: StatBox;
};

/** クラブの集計。**母数が2つある**（`games` と `box.statGames`。詳細設計 1.9）。 */
export type TeamStat = {
  games: number;
  wins: number;
  losses: number;
  perGame: {
    pointsFor: number | null;
    pointsAgainst: number | null;
    margin: number | null;
  };
  box: StatBox & { statGames: number };
};

export type TeamSeasonStat = TeamStat & { seasonId: string | null; label: string | null };

export type RosterEntry = {
  playerId: string;
  name: string;
  number: string | null;
  position: string | null;
  games: number;
  perGame: PlayerStat['perGame'];
};

export type Player = {
  player: { playerId: string; name: string };
  current: {
    seasonId: string;
    clubSlug: string;
    clubName: string | null;
    number: string | null;
    position: string | null;
  } | null;
  career: PlayerStat | null;
  /** **季中に移籍した季は2行になる**（クラブ別に持つため。詳細設計 1.9）。 */
  seasons: (PlayerStat & {
    seasonId: string;
    label: string | null;
    club: { slug: string | null; name: string | null; shortName: string | null };
  })[];
};

/**
 * 1リクエストあたりの行数上限。
 *
 * D1 Free には **1 Worker 呼び出しあたり50クエリ**という上限がある。1文に詰められる
 * 行数はバインドパラメータ上限100で決まるため、上限はテーブルごとに算出する
 * （詳細設計 3.4）。
 *
 *     max_rows_per_request = floor(100 / 列数) × 40
 *
 * 係数40は、50クエリのうち10を非活性化・ログ・整合性確認の余裕として残すため。
 *
 * **列数は DDL の全列数で数える。** `DEFAULT` を持つ列を「INSERT 文に含めない前提」で
 * 除くと、実装が明示指定に変わった瞬間に上限を超える。上限は最も厳しい側で固定し、
 * 実装の書き方に依存させない。
 *
 * 列数の唯一の出典は `db/migrations/*.sql` である。ここの値と DDL の一致は
 * `batch/tests/test_batch_limits.py`（`test_batch_limits_match_schema`）が検査する。
 */

/** バインドパラメータの上限（D1）。1文に詰められる行数を決める。 */
export const MAX_BIND_PARAMS = 100;

/** 1 Worker 呼び出しあたりのクエリ上限（D1 Free）。 */
export const MAX_QUERIES_PER_REQUEST = 50;

/** 上限の算出に使う文数。50 のうち 10 を余裕として残す。 */
export const STATEMENTS_BUDGET = 40;

/** DDL の全列数。手で書き換えない（`db/migrations/*.sql` から数える）。 */
export const COLUMN_COUNTS = {
  clubs: 5,
  players: 5,
  venues: 7,
  seasons: 5,
  club_seasons: 8,
  club_source_ids: 5,
  player_seasons: 8,
  venue_revisions: 5,
  venue_source_keys: 2,
  games: 24,
  team_games: 10,
  team_game_stats: 22,
  player_game_stats: 24,
  game_entries: 6,
  team_ratings: 8,
  predictions: 19,
  prediction_team_targets: 17,
  player_predictions: 31,
  prediction_reasons: 8,
  model_versions: 25,
  prediction_model_bundle: 4,
  prediction_results: 14,
  accuracy_summary: 9,
  ingestion_logs: 9,
} as const;

export type TableName = keyof typeof COLUMN_COUNTS;

/** 1文に詰められる行数。 */
export function rowsPerStatement(table: TableName): number {
  return Math.floor(MAX_BIND_PARAMS / COLUMN_COUNTS[table]);
}

/** 1リクエストで受け付ける行数の上限。 */
export function maxRowsPerRequest(table: TableName): number {
  return rowsPerStatement(table) * STATEMENTS_BUDGET;
}

/** 指定件数を送るのに必要な文数。 */
export function statementsFor(table: TableName, rows: number): number {
  return Math.ceil(rows / rowsPerStatement(table));
}

/** 行を1文ぶんずつに切る。 */
export function chunkRows<T>(table: TableName, rows: readonly T[]): T[][] {
  const size = rowsPerStatement(table);
  const out: T[][] = [];
  for (let i = 0; i < rows.length; i += size) out.push(rows.slice(i, i + size));
  return out;
}

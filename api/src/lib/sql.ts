/**
 * 複数行 INSERT の組み立て。
 *
 * **D1 クエリは必ず `prepare().bind()` を使う**（CLAUDE.md 絶対ルール4）。
 * ここで文字列に埋めるのは列名と `?` の個数だけで、値は必ずバインドする。
 *
 * 1文の行数は `batch-limits.ts` が決める。列数は DDL の全列数で数える。
 */
import { chunkRows, type TableName } from '../config/batch-limits';

export type Row = readonly (string | number | null)[];

const marks = (cols: number, rows: number): string =>
  Array.from({ length: rows }, () => `(${Array.from({ length: cols }, () => '?').join(',')})`).join(
    ',',
  );

/**
 * `INSERT ... ON CONFLICT DO UPDATE` の文を、1文ぶんずつに切って返す。
 *
 * @param conflict 衝突判定に使う列（主キー）
 * @param update   衝突時に更新する列。空なら `DO NOTHING`
 * @param extra    `SET` に足す式（`updated_at = datetime('now')` など）
 */
export function upsertStatements(
  db: D1Database,
  table: TableName,
  columns: readonly string[],
  rows: readonly Row[],
  opts: { conflict: readonly string[]; update?: readonly string[]; extra?: readonly string[] },
): D1PreparedStatement[] {
  if (rows.length === 0) return [];
  const sets = [
    ...(opts.update ?? []).map((col) => `${col} = excluded.${col}`),
    ...(opts.extra ?? []),
  ];
  const action = sets.length > 0 ? `DO UPDATE SET ${sets.join(', ')}` : 'DO NOTHING';
  return chunkRows(table, rows).map((chunk) =>
    db
      .prepare(
        `INSERT INTO ${table} (${columns.join(', ')}) VALUES ${marks(columns.length, chunk.length)}
         ON CONFLICT(${opts.conflict.join(', ')}) ${action}`,
      )
      .bind(...chunk.flat()),
  );
}

/** 素の複数行 INSERT（衝突を想定しない洗い替え用）。 */
export function insertStatements(
  db: D1Database,
  table: TableName,
  columns: readonly string[],
  rows: readonly Row[],
): D1PreparedStatement[] {
  if (rows.length === 0) return [];
  return chunkRows(table, rows).map((chunk) =>
    db
      .prepare(
        `INSERT INTO ${table} (${columns.join(', ')}) VALUES ${marks(columns.length, chunk.length)}`,
      )
      .bind(...chunk.flat()),
  );
}

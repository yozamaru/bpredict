import { describe, expect, it } from 'vitest';

import {
  COLUMN_COUNTS,
  MAX_BIND_PARAMS,
  MAX_QUERIES_PER_REQUEST,
  chunkRows,
  maxRowsPerRequest,
  rowsPerStatement,
  statementsFor,
  type TableName,
} from '../src/config/batch-limits';

const tables = Object.keys(COLUMN_COUNTS) as TableName[];

describe('バッチサイズ（詳細設計 3.4）', () => {
  it.each(tables)('%s の上限が floor(100/列数)×40 と一致する', (table) => {
    const expected = Math.floor(MAX_BIND_PARAMS / COLUMN_COUNTS[table]) * 40;
    expect(maxRowsPerRequest(table)).toBe(expected);
  });

  it.each(tables)('%s を上限まで送っても50クエリを超えない', (table) => {
    // これが A-10 の「1リクエストあたりの D1 クエリ数が50を超えない」の核
    expect(statementsFor(table, maxRowsPerRequest(table))).toBeLessThanOrEqual(
      MAX_QUERIES_PER_REQUEST,
    );
  });

  it.each(tables)('%s の1文が バインドパラメータ上限を超えない', (table) => {
    expect(rowsPerStatement(table) * COLUMN_COUNTS[table]).toBeLessThanOrEqual(MAX_BIND_PARAMS);
  });

  it('設計に載っている代表値と一致する', () => {
    // 詳細設計 3.4 の表。列を増やしたときに静かに変わることを防ぐ
    expect(maxRowsPerRequest('player_predictions')).toBe(120);
    expect(maxRowsPerRequest('games')).toBe(160);
    expect(maxRowsPerRequest('team_games')).toBe(400);
    expect(maxRowsPerRequest('predictions')).toBe(200);
    expect(maxRowsPerRequest('venue_source_keys')).toBe(2000);
  });

  it('chunkRows が1文ぶんずつに切る', () => {
    const rows = Array.from({ length: 30 }, (_, i) => i);
    const chunks = chunkRows('clubs', rows);      // clubs は5列 → 20行/文
    expect(chunks.map((x) => x.length)).toEqual([20, 10]);
    expect(chunks.flat()).toEqual(rows);
  });

  it('空配列では文を作らない', () => {
    expect(chunkRows('clubs', [])).toEqual([]);
    expect(statementsFor('clubs', 0)).toBe(0);
  });
});

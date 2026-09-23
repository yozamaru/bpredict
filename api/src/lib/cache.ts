/**
 * 公開エンドポイントのキャッシュ方針（詳細設計 3.5）。
 *
 * **`max-age` を長く取らない。** 一度 `max-age=86400` を返すとそのクライアントは
 * 24時間必ず古い値を見る。エッジをパージしても届かない。
 *
 * **「パージする」に依存しない。** Workers の Cache API の `delete()` はそのコロにしか
 * 効かず、`*.workers.dev` ではゾーンパージも使えない。エッジ側の TTL（`s-maxage`）を
 * 短く保ち、`stale-while-revalidate` で体感を保つ。
 */
export const CACHE = {
  /** 過去日の試合一覧・確定済みの試合詳細・的中率 */
  settled: 'public, max-age=60, s-maxage=3600, stale-while-revalidate=86400',
  /** 未実施の試合詳細。予測が差し替わりうるのでエッジの TTL を短くする */
  pending: 'public, max-age=60, s-maxage=300, stale-while-revalidate=3600',
  /** チーム一覧・詳細 */
  teams: 'public, max-age=60, s-maxage=3600',
  /** 稼働状態。キャッシュさせない */
  none: 'no-store',
} as const;

export type CachePolicy = (typeof CACHE)[keyof typeof CACHE];

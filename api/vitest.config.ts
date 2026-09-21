import { cloudflareTest, readD1Migrations } from '@cloudflare/vitest-pool-workers';
import { defineConfig } from 'vitest/config';

// テストは Workers ランタイム（Miniflare）で走らせる。
// `crypto.subtle.timingSafeEqual` と D1 の `batch()` は Workers の API であり、
// Node 上では検証できない。
//
// **スキーマは db/migrations/*.sql を唯一の出典とする**（CLAUDE.md）。
// テスト側に DDL を書き写さない。
const migrations = await readD1Migrations('../db/migrations');

export default defineConfig({
  plugins: [
    cloudflareTest({
      wrangler: { configPath: './wrangler.toml' },
      miniflare: { bindings: { TEST_MIGRATIONS: migrations } },
    }),
  ],
});

// Workers ランタイムでテストを走らせるための型。
//
// `worker-configuration.d.ts` は `wrangler types` が生成する（手で直さない）。
// **シークレットをここで宣言しない** — `.dev.vars` がある環境では生成物側に
// `INGEST_TOKEN: string` が出るため、`?:` で重ねると宣言が衝突する。
// 認証は `c.env` を `Record<string, string | undefined>` として読むので、
// 型に現れていなくても動く。
/// <reference types="@cloudflare/vitest-pool-workers/types" />
// `?raw` インポートと `import.meta.glob` の型。**新しい依存は増えない**
// （vite は vitest の依存として既に入っている）。テストで実装のソースを
// 走査するために使う（`@types/node` を足さないための措置）。
/// <reference types="vite/client" />

declare namespace Cloudflare {
  interface Env {
    /** `db/migrations/*.sql` を `readD1Migrations` で読んだもの（vitest.config.ts が注入する）。 */
    TEST_MIGRATIONS: import('@cloudflare/vitest-pool-workers').D1Migration[];
  }
}

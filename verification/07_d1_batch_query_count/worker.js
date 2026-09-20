// P0-13  D1 の batch() 内の各文が「1呼び出し50クエリ（Free）」にどう計上されるかを測る。
//
// 設計は「1文＝1クエリ」という最も厳しい前提で書いてある。実測して初めて緩められる。
// 使い捨てのテーブル probe に INSERT するだけで、本番のテーブルには一切触れない。
//
// GET /probe            … 文数を増やしながら batch() を投げ、失敗し始める本数を返す
// GET /probe?n=60       … 文数を指定して1回だけ投げる
// GET /cpu?rows=500     … 同一リクエスト内で JSON パース+検証+batch組立の CPU を測る（P0-15 の補助）

const STEPS = [1, 10, 25, 40, 49, 50, 51, 60, 75, 100, 150, 200, 400, 800, 1000];

async function runBatch(db, n) {
  const stmts = [];
  for (let i = 0; i < n; i++) {
    stmts.push(db.prepare("INSERT INTO probe (k, v) VALUES (?, ?)").bind(`k${i}`, i));
  }
  const t0 = Date.now();
  await db.batch(stmts);
  return Date.now() - t0;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const json = (o) => new Response(JSON.stringify(o, null, 2),
      { headers: { "content-type": "application/json" } });

    if (url.pathname === "/probe") {
      await env.DB.prepare("DELETE FROM probe").run();

      const one = url.searchParams.get("n");
      if (one) {
        const n = Number(one);
        try {
          const ms = await runBatch(env.DB, n);
          const cnt = await env.DB.prepare("SELECT count(*) AS c FROM probe").first();
          return json({ statements: n, ok: true, ms, rowsInserted: cnt.c });
        } catch (e) {
          // 例外オブジェクトはそのまま返さない（設計の規約）。型名と短いメッセージのみ。
          return json({ statements: n, ok: false, error: e?.name ?? "Error",
                        message: String(e?.message ?? "").slice(0, 200) });
        }
      }

      const results = [];
      let firstFailure = null;
      for (const n of STEPS) {
        await env.DB.prepare("DELETE FROM probe").run();
        try {
          const ms = await runBatch(env.DB, n);
          results.push({ statements: n, ok: true, ms });
        } catch (e) {
          results.push({ statements: n, ok: false, error: e?.name ?? "Error",
                         message: String(e?.message ?? "").slice(0, 200) });
          if (firstFailure === null) firstFailure = n;
        }
      }
      const lastOk = results.filter((r) => r.ok).map((r) => r.statements).pop() ?? 0;
      return json({
        note: "1文＝1クエリなら 50 を超えたところで失敗するはず",
        firstFailureAt: firstFailure,
        maxSucceeded: lastOk,
        interpretation: firstFailure === null
          ? "測定範囲（最大1000文）では失敗しなかった → batch() 全体が1クエリと数えられている可能性が高い"
          : (firstFailure <= 51
            ? "1文＝1クエリ。設計の前提どおり。バッチサイズを緩めない"
            : `${firstFailure}文で失敗。50クエリ制限とは別の上限に当たっている可能性がある`),
        results,
      });
    }

    if (url.pathname === "/cpu") {
      const rows = Number(url.searchParams.get("rows") ?? 500);
      const COLS = ["game_id","player_id","club_id","game_date","started","minutes",
        "fg2m","fg2a","fg3m","fg3a","ftm","fta","oreb","dreb","ast","tov","stl","blk",
        "pf","fd","plus_minus","pts"];
      const payload = JSON.stringify({ rows: Array.from({ length: rows }, (_, i) => {
        const r = {};
        for (const c of COLS) r[c] = c.endsWith("_id") || c === "game_date"
          ? `x_${i}_${c}` : Math.round(Math.random() * 20);
        return r;
      })});
      const t0 = Date.now();
      const parsed = JSON.parse(payload);
      const per = Math.floor(100 / COLS.length);
      const stmts = [];
      for (let i = 0; i < parsed.rows.length; i += per) {
        const chunk = parsed.rows.slice(i, i + per);
        const ph = chunk.map(() => `(${COLS.map(() => "?").join(",")})`).join(",");
        const binds = [];
        for (const r of chunk) for (const c of COLS) binds.push(r[c]);
        stmts.push(env.DB.prepare(
          `INSERT INTO probe_stats (${COLS.join(",")}) VALUES ${ph}`).bind(...binds));
      }
      return json({ rows, bytes: payload.length, statements: stmts.length,
                    wallMs: Date.now() - t0,
                    note: "Workers の Date.now() は粗い。CPU 時間は Cloudflare のダッシュボードで確認する" });
    }

    return json({ usage: ["/probe", "/probe?n=60", "/cpu?rows=500"] });
  },
};

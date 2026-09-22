// 要件 4.2 の「初期JS 180KB以内（gzip後）」を CI で検査する。
// 上限を上げた経緯（Next 16 + React 19 のベースラインが 169KB）は要件 4.2 にある。
// 上げた分、増分の監視を自動化する。ライブラリを1つ足すと一気に超えるため。
import { readFileSync } from 'node:fs';
import { gzipSync } from 'node:zlib';

const JS_LIMIT_BYTES = 180 * 1024;
const CSS_LIMIT_BYTES = 20 * 1024;
const PAGE = new URL('../out/index.html', import.meta.url);

const html = readFileSync(PAGE, 'utf8');
const sources = [...new Set([...html.matchAll(/src="(\/_next\/[^"]+\.js)"/g)].map((m) => m[1]))];
if (sources.length === 0) {
  console.error('out/index.html から JS の参照が見つからない。ビルドしてから実行すること。');
  process.exit(1);
}

const gzipOf = (path) => gzipSync(readFileSync(new URL(`../out${path}`, import.meta.url))).length;

let js = 0;
for (const source of sources.sort()) {
  const bytes = gzipOf(source);
  js += bytes;
  console.log(`${String(bytes).padStart(7)}  ${source}`);
}

const styles = [...new Set([...html.matchAll(/href="(\/_next\/[^"]+\.css)"/g)].map((m) => m[1]))];
let css = 0;
for (const style of styles.sort()) {
  const bytes = gzipOf(style);
  css += bytes;
  console.log(`${String(bytes).padStart(7)}  ${style}`);
}

const kb = (value) => `${(value / 1024).toFixed(1)}KB`;
console.log(`\n初期JS  (gzip): ${kb(js)} / 上限 ${kb(JS_LIMIT_BYTES)}`);
console.log(`初期CSS (gzip): ${kb(css)} / 上限 ${kb(CSS_LIMIT_BYTES)}`);

let failed = false;
if (js > JS_LIMIT_BYTES) {
  console.error('\n初期JS が上限を超えている（要件 4.2）。');
  failed = true;
}
if (css > CSS_LIMIT_BYTES) {
  console.error('\n初期CSS が上限を超えている（ui-implementation スキルの性能予算）。');
  failed = true;
}
if (failed) {
  console.error(
    '依存を足したなら見直すこと。上限の変更は実測とセットでのみ行う' +
    '（verification/RESULTS.md に記録する）。',
  );
  process.exit(1);
}

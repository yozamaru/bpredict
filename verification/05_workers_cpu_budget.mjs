// Workers Free の CPU 10ms 予算に対する見積もり
// 注意: Node と workerd は同じ V8 だが、CPU 計上方法は異なる。桁の目安として扱う。
const COLS = ['game_id','player_id','club_id','game_date','started','minutes',
  'fg2m','fg2a','fg3m','fg3a','ftm','fta','oreb','dreb','ast','tov','stl','blk',
  'pf','fd','plus_minus','pts'];

function makeRows(n){
  const rows=[];
  for(let i=0;i<n;i++){
    const r={};
    for(const c of COLS){
      r[c] = (c.endsWith('_id')||c==='game_date') ? `x_${i}_${c}` : Math.round(Math.random()*40);
    }
    rows.push(r);
  }
  return rows;
}

// Zod 相当の検証（型 + 値域 + 恒等式）を手書きで再現
const RANGE = {minutes:[0,60], pts:[0,250], pf:[0,6], fg2m:[0,40], fg2a:[0,60],
  fg3m:[0,30], fg3a:[0,50], ftm:[0,40], fta:[0,50], oreb:[0,30], dreb:[0,40],
  ast:[0,30], tov:[0,20], stl:[0,15], blk:[0,15], fd:[0,20]};
function validate(rows){
  let ok=0;
  for(const r of rows){
    let good=true;
    for(const c of COLS){
      if(!(c in r)){good=false;break;}
      const v=r[c];
      if(c.endsWith('_id')||c==='game_date'){ if(typeof v!=='string'||v.length>64){good=false;break;} }
      else { if(typeof v!=='number'||!Number.isFinite(v)){good=false;break;}
             const rg=RANGE[c]; if(rg&&(v<rg[0]||v>rg[1])){good=false;break;} }
    }
    if(good && (r.fg2m>r.fg2a || r.fg3m>r.fg3a || r.ftm>r.fta)) good=false;
    if(good) ok++;
  }
  return ok;
}
function buildBatch(rows){
  const out=[];
  const per = Math.floor(100/COLS.length);
  for(let i=0;i<rows.length;i+=per){
    const chunk=rows.slice(i,i+per);
    const ph = chunk.map(()=>`(${COLS.map(()=>'?').join(',')})`).join(',');
    const binds=[];
    for(const r of chunk) for(const c of COLS) binds.push(r[c]);
    out.push({sql:`INSERT INTO player_game_stats (${COLS.join(',')}) VALUES ${ph}`, binds});
  }
  return out;
}

console.log("="*0);
console.log("="

.repeat(74));
console.log("V-15  /internal/* の CPU 見積もり（Workers Free = 1呼び出し 10ms）");
console.log("=".repeat(74));
console.log("処理: JSON.parse → 全行の型/値域/恒等式の検証 → D1 batch 文の組み立て");
console.log("注意: Node 実測。workerd とは CPU 計上が異なるため桁の目安として扱う。");
console.log("");
console.log("  行数  JSONサイズ   parse(ms)  validate(ms)   build(ms)   合計(ms)   10ms予算");
console.log("-".repeat(74));
for(const n of [50,100,200,500,1000]){
  const rows=makeRows(n);
  const json=JSON.stringify({rows});
  // ウォームアップ
  for(let i=0;i<20;i++){ const p=JSON.parse(json); validate(p.rows); buildBatch(p.rows); }
  const REP=200;
  let tp=0,tv=0,tb=0;
  for(let i=0;i<REP;i++){
    let s=process.hrtime.bigint(); const p=JSON.parse(json); tp+=Number(process.hrtime.bigint()-s);
    s=process.hrtime.bigint(); validate(p.rows);              tv+=Number(process.hrtime.bigint()-s);
    s=process.hrtime.bigint(); buildBatch(p.rows);            tb+=Number(process.hrtime.bigint()-s);
  }
  const ms=x=>x/REP/1e6;
  const tot=ms(tp)+ms(tv)+ms(tb);
  const verdict = tot>10 ? "超過" : (tot>5 ? "危険" : "余裕");
  console.log(`${String(n).padStart(6)} ${(json.length/1024).toFixed(0).padStart(8)}KB ${ms(tp).toFixed(3).padStart(11)} ${ms(tv).toFixed(3).padStart(13)} ${ms(tb).toFixed(3).padStart(11)} ${tot.toFixed(3).padStart(10)}   ${verdict}`);
}
console.log("");
console.log("【判断材料】");
console.log("  ・純粋な計算だけで 500行は 10ms 予算のかなりを消費する");
console.log("  ・実際にはこれに Zod のスキーマ解決、D1 バインド、レスポンス生成が加わる");
console.log("  ・workerd は Node より遅い場合があり、余裕はさらに小さい");

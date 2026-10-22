---
title: B.PREDICT API 仕様書
subtitle: 公開 API ／ 内部 API
version: ver. 1.0.0 beta
---

# 共通事項

**ベースパス:** `/api/v1`　　**形式:** JSON　　**文字コード:** UTF-8

サイトと API は **同一オリジン**に置いています（`bpredict.naomaru.app`）。
`/api/*` が Workers、それ以外が Pages です。CORS の設定を持たないのはこのためです。

## 応答の形

```jsonc
// 成功
{ "data": { ... }, "meta": { "generatedAt": "...", "modelVersion": "...", "stale": false } }

// 失敗
{ "error": { "code": "NOT_FOUND", "message": "..." } }
```

## エラーコード

| コード | HTTP | 意味 |
|---|---:|---|
| `BAD_REQUEST` | 400 | 入力が形式・値域を満たさない |
| `UNAUTHORIZED` | 401 | Bearer が無い、または一致しない |
| `NOT_FOUND` | 404 | 対象が無い。**シーズン範囲外の日付を含む** |
| `ALREADY_FINAL` | 409 | **開始時刻を過ぎた試合への書き込み** |
| `RATE_LIMITED` | 429 | レート制限 |
| `INTERNAL` | 500 | 内部エラー |

**エラー本文に内部 ID や取得元 URL を含めません。**

## 時刻

**すべて UTC の ISO 8601 で返します**（`2026-09-22T10:05:00Z`）。
JST への変換は画面が行います。

`gameDate` は **その試合の JST における暦日**です。`tipoffAt` が UTC で
同じ日とは限りません（19:05 JST 開始は UTC で 10:05 の同日ですが、
深夜の試合では日付がずれます）。

## 入力の検証

**すべてのパラメータを境界で検証し、不一致は D1 にもキャッシュにも触れず 400 で返します。**

| パラメータ | 制約 |
|---|---|
| `date` | `^\d{4}-\d{2}-\d{2}$` かつ実在日付かつ **シーズン範囲内** |
| `slug` | `^[a-z0-9-]{1,40}$` |
| `playerId` | 数字列 |
| `gameId` | 公式試合 ID の形式 |
| `limit` | 既定 20、最大 100（範囲外はクランプ） |

日付は 3 段で見ます。**(1) 形式と実在日付 (2) 粗い絶対範囲（2016〜2100）**
までは D1 に触れずに 400、**(3) シーズン範囲**だけが `seasons` に 1 クエリ投げて 404。

**順序を崩さないでください。** URL 空間を有限に保つ関門が D1 の後ろに下がります。

---

# 公開 API

認証は不要です。守っているのは書き込み側であって読み取り側ではありません。

| メソッド | パス | 用途 |
|---|---|---|
| GET | `/api/v1/games?date=` | 指定日の試合一覧 |
| GET | `/api/v1/games/:gameId` | 試合詳細（試合前 / 試合後） |
| GET | `/api/v1/results?date=` | 終了した試合と予測の対比 |
| GET | `/api/v1/teams` | クラブ一覧（画面の導線用） |
| GET | `/api/v1/teams/:slug` | クラブ別（履歴・戦績・当季の選手） |
| GET | `/api/v1/players/:playerId` | 選手別（集計値のみ） |
| GET | `/api/v1/accuracy` | 的中率 |

> **`/api/v1/health` は未実装です。** 当日のカウンタの出所が未確定のため
> 保留しています（詳細設計 3.3）。

## キャッシュ

| 対象 | `Cache-Control` |
|---|---|
| 確定済み（試合後・過去日） | `public, max-age=60, s-maxage=3600, stale-while-revalidate=86400` |
| 未実施 | `public, max-age=60, s-maxage=300, stale-while-revalidate=3600` |
| `/teams` | `public, max-age=60, s-maxage=3600` |

**`max-age` を長く取りません。** 一度 `max-age=86400` を返すとその利用者は
24 時間必ず古い値を見ることになり、エッジをパージしても届きません。

**キャッシュキーは許可リストに無いクエリパラメータを除いて正規化します**
（`?fbclid=` のような値でキャッシュを迂回されないため）。

## レート制限

Cloudflare WAF（無料プラン）で `/api/v1/*` に 1 ルール。
**10 秒あたり 30 リクエストを超えたら 10 秒ブロック**します。

無料プランではカウント期間もブロック時間も 10 秒しか選べません。
**意図的な攻撃には効きません。** 真の防御は次の 2 つで、WAF はその補助です。

1. 主要導線が静的配信で Workers を通らない
2. URL 空間の有限化（シーズン範囲外は D1 到達前に 404）

---

# 公開 API の応答

## GET /games?date=

**静的 JSON の `today.json` と同じ形です。** 専用のスキーマを作っていません。

```jsonc
{
  "data": {
    "gameDate": "2026-09-22",          // その一覧が何日ぶんか
    "games": [{
      "gameId": "506447",
      "tipoffAt": "2026-09-22T10:05:00Z",   // UTC。時刻未公表なら null
      "status": "SCHEDULED",
      "competition": "REGULAR",              // REGULAR | PLAYOFF
      "home": { "clubId": "...", "slug": "...", "name": "...", "shortName": "..." },
      "away": { ... },
      "prediction": {                        // 予測が無ければ null（キーは残す）
        "homeWinProb": 0.68,
        "predHomeScore": 84, "predAwayScore": 78,
        "isProvisional": false, "isFinal": false,
        "isEarlySeason": null,               // 推測で false を入れない
        "modelVersion": "winner-v1.2026.1009"
      },
      "homeScore": 88, "awayScore": 81,      // 終了していなければ null
      "evaluation": { "isCorrect": true, "scoreError": 3 }   // 未照合なら null
    }],
    "accuracy": { "accuracy": 0.682, "brier": 0.204, "n": 312 }   // 未照合なら null
  },
  "meta": { "generatedAt": "..." }
}
```

**`awayWinProb` を返しません**（`1 − homeWinProb` で導出できます）。
ただし**画面では必ず両チーム分を表示してください**。

**予測が無い試合を省略しません。** 日程に載っているのに予測が無い状態は実在します。

## GET /games/:gameId

**試合前と試合後でキーの位置が変わりません。** 変わるのは中身だけです。

| キー | 試合前 | 試合後 |
|---|---|---|
| `data.game` | `status: "SCHEDULED"`、スコアなし | `status: "FINISHED"`、スコアあり |
| `data.prediction` | `isFinal: false` | `isFinal: true` |
| `data.evaluation` | **`null`** | 判定・誤差・その確率帯の通算 |
| `data.playerPredictions` | 予測値 | **予測値のまま** |
| `data.playerActuals` | `[]` | **その試合の実際の記録** |

キーの位置が状態で動くと、クライアントが状態ごとに別のパスを持つことになり、
片方だけ壊れる不具合が出ます。

### 予測と実績を別の配列にしている理由

**母集団が違います。** 予測は出場確率 50% 以上の選手、実績は実際に出場した全員です。
1 つの表に混ぜると「予測を外した」と「候補に入っていなかった」が
**どちらも空欄として同じに見えます。**

決定的なのは **予測が 1 本も無い試合**で、混ぜる形では行が 1 つも作れません。

### 率の扱い

| | `pct` が `null` になる条件 |
|---|---|
| `playerPredictions` | **試投数の予測が閾値未満**（存在しない精度を主張しない） |
| `playerActuals` | **試投数が 0**。実績に閾値は設けない |

閾値は FG `fg.a >= 4`、2FG / 3FG は各 `a >= 3`、FT `ft.a >= 3`、EFG% / TS% は `fg.a >= 4`。

**`plusMinus` は実績にだけ入ります**（予測しない指標です）。

## GET /results?date=

`status = 'FINISHED'` かつ照合済みの試合だけを返します。
**`outcome = 'VOID'`（中止・延期）は返しません。**

```jsonc
"evaluation": {
  "isCorrect": true, "scoreError": 3,
  "bucketContext": { "bucket": "60-70%", "n": 42, "correct": 29, "rate": 0.690 }
}
```

`bucketContext` は「68% と予想した試合は 42 試合中 29 試合が的中」の根拠です。
**外れた試合でも同じ形で返します。**

> 出典は `accuracy_summary.hit_rate` です。`actual_rate`（ホームが勝った割合）
> ではありません。**50% 未満の帯では的中率と符号が逆になります。**

## GET /teams ／ /teams/:slug

`/teams` はクラブ名とリンクだけを返します。**成績を返しません**
（一覧に並べると順位表に見えるためです）。並びは `clubs.slug` の昇順です。

`/teams/:slug` は当季の成績・直近 5 試合・Elo・的中率・予測履歴に加えて、
**シーズン別と通算の戦績**、**当季の選手一覧**を返します。

- `ownWinProb` は **そのクラブから見た**勝率です（アウェイ側では `1 − p`）
- `losses` は列として持ちませんが、返します（クライアントに計算させません）
- `perGame` と `box` は **API が導出します**（集計テーブルは合計だけを持ちます）

## GET /players/:playerId

**集計値だけを返します。試合ごとの記録の一覧は返しません。**

**季中に移籍した季は 2 行になります**（クラブ別に持つため）。1 行に畳むと
「どのクラブでの成績か」が消えます。

集計が 1 行も無ければ 404 です（静的生成の範囲内でも、集計が走る前は空です）。

## GET /accuracy

`accuracy_summary` を単一テーブルから読みます（数十行）。

```jsonc
{
  "data": {
    "overall": { "accuracy": 0.691, "brier": 0.2099, "n": 55,
                 "baselineAccuracy": 0.491, "scoreMae": 10.3 },
    "bySeason": [...], "byModel": [...],
    "calibration": [{ "bucket": "60-70%", "predicted": 0.65, "actual": 0.63, "n": 88 }],
    "byProvisional": { "provisional": {...}, "confirmed": {...} }
  }
}
```

- `scoreMae` は **1 チームあたり**の平均絶対誤差です（得点差の MAE ではありません）
- 母数は `n` と同じです。**欠けた行があったときだけ `null`** になります
  （黙って除いて平均すると、母数の違う数字が並びます）
- **Log Loss は返しません**（学習時の目的関数であり、表示指標ではありません）
- 1 試合も照合していない間は `overall` が `null` です

---

# 内部 API

**`/api/v1/internal/*`。すべて `Authorization: Bearer <token>` が必要です。**

この API は **書き込みの関門**です。D1 REST API を直接叩く経路を作らないのは、
REST 直叩きを許すと Zod 検証・認可・tipoff ガード・`is_final` 保護が
**すべて迂回可能になる**ためです。

## 認証

| トークン | 対象 |
|---|---|
| `INGEST_TOKEN` / `INGEST_TOKEN_NEXT` | 書き込み全般・運用上の読み取り（**2 キー方式**） |
| `FINALIZE_TOKEN` | `POST /internal/finalize` のみ（破壊的操作を分離） |

**定数時間で比較します**（両者を SHA-256 にしてから）。
`Bearer ` の形も正規表現で厳格に見ます（`.replace("Bearer ", "")` では
スキームの検証になりません）。

### 無停止でのトークン回転

1. 新トークンを Workers Secret に `INGEST_TOKEN_NEXT` として追加
2. 認証は現行・次期のいずれかに一致すれば通る
3. GitHub Secrets を更新
4. 次のバッチ成功を確認
5. 旧トークンを削除

## 一覧

### 書き込み

| メソッド | パス | 対象 |
|---|---|---|
| POST | `/internal/masters` | `seasons` / `clubs` / `club_source_ids` |
| POST | `/internal/games` | `games` / `team_games` ＋ 試合から導かれるマスタ |
| POST | `/internal/stats` | `team_game_stats` / `player_game_stats` |
| POST | `/internal/entries` | `game_entries`（**全行を洗い替える**） |
| POST | `/internal/ratings` | `team_ratings`（期間 DELETE ＋ INSERT） |
| POST | `/internal/venue-revisions` | `venue_revisions`（同上） |
| POST | `/internal/rosters` | `players` / `player_seasons` |
| POST | `/internal/predictions` | 予測の追記（**1 リクエスト 1 試合**） |
| POST | `/internal/finalize` | freeze（`FINALIZE_TOKEN`） |
| POST | `/internal/evaluate` | `prediction_results` |
| POST | `/internal/summary` | `accuracy_summary`（**全スコープを洗い替える**） |
| POST | `/internal/stat-summary` | 実績の集計（**upsert のみ。洗い替えない**） |
| POST | `/internal/models` | 学習済みモデルの登録 |
| POST | `/internal/log` | `ingestion_logs` |

### 運用上の読み取り

| メソッド | パス | 用途 |
|---|---|---|
| GET | `/internal/games/ingested` | backfill の再開判定（**スタッツまで入っているか**） |
| GET | `/internal/predictions/pending` | 照合対象（確定済みで未評価） |
| GET | `/internal/results` | **照合済みの全件**（的中率の集計に要る。カーソルで辿る） |
| GET | `/internal/models/active` | 有効モデルのメタ（**artifact 本体は含まない**） |
| GET | `/internal/models/:version/artifact` | artifact を 1 本ずつ |
| GET | `/internal/metrics/active` | 現行モデルの識別子と記録済み評価値 |
| GET | `/internal/venues` | 座標を解決する会場の一覧 |

**これらは入力データの読み取りではありません。** 特徴量生成・学習・推論の入力は
`batch/snapshot/*.parquet` だけです。

> `GET /internal/metrics/active` が返す `cvBrier` を**採用判定の比較に使わないでください**。
> 学習当時のウィンドウで測った値であり、新旧で評価対象が違えば比較になりません。

## 書き込みのときに必ず効く関門

1. Bearer の定数時間比較
2. Zod による**型と値域**の検証
3. **`tipoff_at <= now` の試合への予測書き込みを 409 で拒否**
4. `is_final = 1` の行を UPDATE / DELETE しない（**DB トリガでも二重化**）
5. 非活性化と挿入を**単一 `batch()`** にまとめる（D1 にリクエストを跨ぐトランザクションは無い）
6. 日次の書き込み回数上限（`predictions` への挿入 1 日 2,000 行）

### freeze の順序

`is_final` を持つのは `predictions` と `player_predictions` の 2 つで、
**子 → 親の順に UPDATE します。この順序は必須です。**

```sql
-- 単一 batch() 内。この順序で固定する
UPDATE player_predictions SET is_final = 1 WHERE prediction_id = ? AND is_final = 0;
UPDATE predictions        SET is_final = 1 WHERE id = ?            AND is_final = 0;
```

親を先に確定させると、子の `0 → 1` が親参照トリガに拒否されて **freeze 自体が失敗します。**

残る 4 つの子テーブル（`prediction_reasons` / `prediction_factors` /
`prediction_team_targets` / `prediction_model_bundle`）は `is_final` 列を持たず、
**親参照トリガで同時に凍結されます。**

## 送られてこない列を NULL で上書きしない

同じエンドポイントを複数の経路が共有するため、`DO UPDATE SET col = excluded.col` は
**送られてこなかった列を NULL にします。**

| テーブル | 保護する列 | 送る経路 |
|---|---|---|
| `venues` | `prefecture` / `lat` / `lng` | `resolve_venue_geo` |
| `club_seasons` | `primary_venue_id` / `color_*` | `derive_primary_venues` |
| `players` | `height_cm` | （まだ無い） |
| **`games`** | **`venue_id`** | 試合後の `StadiumCD`、試合前の `ArenaCD` |

**判断の基準は「列を送らない経路があるか」ではなく「`None` を送る経路があるか」です。**
`games` は全列を送るため、明示的な保護が要ります（2026-10-09 に本番で 24 件が消えました）。

---

# バッチサイズの上限

**D1 Free は 1 Worker 呼び出しあたり 50 クエリ**で、これがこのプロダクトで
最初に当たる制約です。1 文に詰められる行数はバインドパラメータ上限 100 で決まります。

```
1 リクエスト上限 = floor(100 ÷ 列数) × 40
```

係数 40 は、残り 10 を非活性化・ログ・整合性確認の余裕として残すためです。

**列数は DDL の全列数で数えます。** `DEFAULT` を持つ列を除くと、実装が明示指定に
変わった瞬間に上限を超えます。**一律 500 件にしないでください**
（`player_predictions` は 31 列あり、500 件で 167 文が必要になります）。

| テーブル | 列数 | 1 文の行数 | **1 リクエスト上限** |
|---|---:|---:|---:|
| `games` | 25 | 4 | **160** |
| `team_games` | 10 | 10 | **400** |
| `team_game_stats` | 22 | 4 | **160** |
| `player_game_stats` | 24 | 4 | **160** |
| `game_entries` | 6 | 16 | **640** |
| `team_ratings` | 8 | 12 | **480** |
| `predictions` | 19 | 5 | **200** |
| `prediction_team_targets` | 17 | 5 | **200** |
| `player_predictions` | 31 | 3 | **120** |
| `prediction_reasons` | 8 | 12 | **480** |
| `prediction_factors` | 6 | 16 | **640** |
| `prediction_model_bundle` | 4 | 25 | **1000** |
| `prediction_results` | 14 | 7 | **280** |
| `accuracy_summary` | 11 | 9 | **360** |
| `player_stat_summary` | 23 | 4 | **160** |
| `team_stat_summary` | 23 | 4 | **160** |
| `model_versions` | 25 | 4 | **160** |
| `ingestion_logs` | 9 | 11 | **440** |
| `seasons` | 5 | 20 | **800** |
| `clubs` | 5 | 20 | **800** |
| `club_source_ids` | 5 | 20 | **800** |
| `club_seasons` | 8 | 12 | **480** |
| `players` | 5 | 20 | **800** |
| `player_seasons` | 8 | 12 | **480** |
| `venues` | 7 | 14 | **560** |
| `venue_source_keys` | 2 | 50 | **2000** |
| `venue_revisions` | 5 | 20 | **800** |

> 上限は `api/src/config/batch-limits.ts` が DDL から機械的に算出し、
> CI が 50 クエリ以内であることを検証します。

**`model_versions.artifact_text` は 1.5 MB 以下**です（D1 の 1 行上限は 2,000,000 バイト）。
超えたら登録を拒否し、現行モデルを継続します。

---

# 静的 JSON

**主要導線は Workers も D1 も経由しません。** バッチが書き出したファイルを
Pages が静的アセットとして配信します。

| ファイル | 中身 | 同じ形の API |
|---|---|---|
| `/data/meta.json` | 鮮度と運用状態 | — |
| `/data/today.json` | 当日の一覧 | `GET /games?date=` |
| `/data/schedule/<date>.json` | 翌日から 7 日 | 同上 |
| `/data/games/<id>.json` | **当日の試合だけ**の詳細 | `GET /games/:gameId` |
| `/data/games/index.json` | 予測を出した試合 ID の索引（**溜まる**） | — |

**専用のスキーマを作っていません。** クライアントは取得先だけを替えます。
キー構造は `contracts/public-shapes.json` に固定し、**api と batch の両方の
テストが同じファイルを読みます。**

## meta.json

```jsonc
{ "data": {
    "generatedAt": "2026-10-21T11:48:28Z",
    "dataAsOf": "2026-10-19T12:35:00Z",      // 推論時点の最新の試合終了時刻
    "lastSuccessAt": "2026-10-21T11:48:28Z", // SUCCESS の最新。24 時間で遅延と判定
    "latestResultDate": "2026-10-19",        // /results が既定で見る日
    "lastRunStatus": "SUCCESS",
    "modelVersions": [...] } }
```

**`stale` を真偽値で持ちません。** バッチは「今」を知らないため、
遅延の判定は**クライアントが `lastSuccessAt` と現在時刻で**行います。

## 古い配信物と新しい画面

**画面を配ってから次のバッチが走るまで、必ず「古い形の JSON ＋ 新しい画面」になります。**
静的 JSON は前回のバッチが書いたもので、画面を配っても中身は変わりません。
これは事故ではなく設計どおりの状態です。

したがって **新しいキーは省略可で受け、配列を回す前に `Array.isArray` で守ってください。**
契約ファイルでは塞がりません（あれは新しい形を両側で固定するもので、この組を見ません）。

---

© 2026 B.PREDICT

"""許可した項目だけを保持する解析結果。DB投入は別工程。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Counts:
    """ボックススコアのカウント値。**選手とチームで同じ形を使う。**

    **`fgm` / `fga` / `reb` を持たない。** `fg2m + fg3m` / `fg2a + fg3a` /
    `oreb + dreb` で導出できる冗長な値であり、持つと不整合の余地が生まれる
    （詳細設計 1.3）。`fd` は被ファウル数（`FOULON`）で、NBA の
    「テイクチャージ」に相当する FIBA 系の項目である。

    **すべて None を取りうる。** サイトは値が無いとき 0 ではなく空文字を返し、
    旧年度は項目そのものが欠ける（`plus_minus` など）。**0 で埋めない** —
    「記録なし」が「0回」になると平均の分母に入って集計が狂う。
    """

    pts: int | None
    fg2m: int | None
    fg2a: int | None
    fg3m: int | None
    fg3a: int | None
    ftm: int | None
    fta: int | None
    oreb: int | None
    dreb: int | None
    ast: int | None
    tov: int | None
    stl: int | None
    blk: int | None
    pf: int | None
    fd: int | None


@dataclass(frozen=True)
class GameRecord:
    """ボックススコアから読めた試合1件。

    `id` は**公式サイトの試合ID**で、これを主キーに upsert する — 自然キーに
    `game_date` を含めると、延期で日付が変わった瞬間に別レコードになり、
    旧行が `SCHEDULED` のまま残る（詳細設計 1.3）。

    **`finished_at` は列として持ち、推定かどうかを併せて持つ。** リーク判定の
    絞り込みは `tipoff_at` ではなくこの列で行うため（要件 6.3）、
    実測が取れないときは `tipoff_at + 2時間` を入れて
    `finished_at_is_estimated = 1` を立て、**区別できる状態にして渡す。**

    `venue_id` は公式の `StadiumCD`、`venue_name` はその試合時点の名称
    （`venue_revisions` の唯一の入力。詳細設計 1.2）。
    """

    id: str
    year: int
    competition: str
    game_date: str
    tipoff_at: str
    finished_at: str
    finished_at_is_estimated: int
    home_club_id: str
    away_club_id: str
    home_name: str
    away_name: str
    venue_id: str | None
    venue_name: str | None
    home_score: int
    away_score: int
    attendance: int | None
    status: str = "FINISHED"


@dataclass(frozen=True)
class PlayerStats:
    """選手1人の1試合分。

    **`club_id` は実績である。** 所属の判定に現在の所属を使わないため
    （特徴量の規約6。詳細設計 2.1）、ここに入るのはその試合に出たクラブで、
    移籍しても過去の記録が動かない。

    **非選手行はここに来ない。** `PlayerID` が空のチーム集計行と、ヘッドコーチ行
    （`Category=2`）は分離する（詳細設計 4.4 の正規化2）。
    """

    game_id: str
    player_id: str
    club_id: str
    name: str
    number: str | None
    started: int | None
    minutes: float | None
    plus_minus: int | None
    stats: Counts


@dataclass(frozen=True)
class TeamStats:
    """チーム1つの1試合分。**公式の合計行（`Category=3`）を採る。**

    選手の単純合計にしないのは、リバウンドなど**選手に付かないチーム記録**が
    あるためである（詳細設計 4.4）。

    `possessions` は取り込み時に算出し、**値域（50〜120）を外れたら None にする** —
    試合そのものは取り込む。中断・不成立の短い試合が実在し（2016-17 の `1330` は
    31分・26–18）、**値が信用できないことと試合がなかったことは別である**
    （詳細設計 1.3）。
    """

    game_id: str
    club_id: str
    is_home: int
    stats: Counts
    possessions: float | None


@dataclass(frozen=True)
class BoxScore:
    """1試合分の解析結果。**ここから先は DB 投入の工程が扱う。**

    `teams` は2件（ホームとアウェイ）。`players` は非選手行を除いた選手だけで、
    **得点とシュート数は `teams` の公式合計と照合済みである**（詳細設計 4.4）。
    照合の分母には `Category=2` の行も含める — 得点を持つ `Category=2` 行が
    実在し、外していたため 2016-17 で7試合が落ちていた。
    """

    game: GameRecord
    players: tuple[PlayerStats, ...]
    teams: tuple[TeamStats, ...]

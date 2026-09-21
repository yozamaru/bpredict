"""架空ID・架空名によるボックススコア生成。HTTP取得した本文ではない。"""

import copy
import json


def boxscore_data():
    game = {
        "ScheduleKey": "demo-game", "Year": 2025, "Event": "", "GameEndedFlg": True,
        "GameDateTime": "1759331100",  # 2025-10-01T15:05:00Z / JSTは翌日
        "HomeTeamID": 101, "AwayTeamID": "102",
        "HomeTeamNameJ": "架空ホーム", "AwayTeamNameJ": "架空アウェイ",
        "HomeTeamScore": 80, "AwayTeamScore": 79,
        "StadiumCD": 901, "StadiumNameJ": "架空アリーナ", "Attendance": "1,000",
    }
    sides = {}
    for side, tid in (("Home", "101"), ("Away", "102")):
        rows = []
        for idx in range(2):
            row = {
                "ScheduleKey": "demo-game", "TeamID": tid, "Category": 1,
                "PeriodCategory": 18, "PlayerID": f"demo-{side}-{idx}",
                "PlayerNameJ": f"架空{side}選手{idx}", "PlayerNo": str(idx),
                "StartingFlg": True, "PlayTime": "30:15", "PLUSMINUS": -2,
                "Point": 40, "PT2M": 10, "PT2A": 20, "PT3M": 5, "PT3A": 10,
                "FTM": 5, "FTA": 5, "RB_OFF": 2, "RB_DEF": 6, "RB_TOT": 8,
                "AS": 4, "TO": 4, "ST": 2, "BS": 1, "FOUL": 2, "FOULON": 2,
            }
            if side == "Away" and idx == 1:
                row["Point"], row["FTM"] = 39, 4
            rows.append(row)
        total = copy.deepcopy(rows[0])
        for key in ("Point", "PT2M", "PT2A", "PT3M", "PT3A", "FTM", "FTA",
                    "RB_OFF", "RB_DEF", "RB_TOT", "AS", "TO", "ST", "BS", "FOUL", "FOULON"):
            total[key] = sum(r[key] for r in rows)
        total.update({"PlayerID": "", "TeamID": None, "Category": 3,
                      "PlayTime": "200:00", "RB_OFF": 7, "RB_DEF": 16, "RB_TOT": 23})
        team = {k: 0 for k in total}
        team.update({"ScheduleKey": "demo-game", "TeamID": tid, "Category": 2,
                     "PeriodCategory": 18, "PlayerID": "", "RB_OFF": 3,
                     "RB_DEF": 4, "RB_TOT": 7})
        quarter = copy.deepcopy(rows[0])
        quarter["PeriodCategory"] = 1
        sides[f"{side}Boxscores"] = [quarter, *rows, team, total]
    return {"ScheduleKey": "demo-game", "Game": game, **sides, "unused": "破棄するデータ"}


def page(data=None):
    payload = json.dumps(boxscore_data() if data is None else data, ensure_ascii=False)
    return f"<html><script>\n_contexts_s3id.data = {payload};\n</script></html>"

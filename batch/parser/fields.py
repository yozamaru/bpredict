"""詳細設計4.4に対応する許可リスト。元JSONを返さない。"""

COUNT_FIELDS = {
    "pts": "Point",
    "fg2m": "PT2M", "fg2a": "PT2A",
    "fg3m": "PT3M", "fg3a": "PT3A",
    "ftm": "FTM", "fta": "FTA",
    "oreb": "RB_OFF", "dreb": "RB_DEF",
    "ast": "AS", "tov": "TO", "stl": "ST", "blk": "BS",
    "pf": "FOUL", "fd": "FOULON",
}

SCORING_FIELDS = ("pts", "fg2m", "fg2a", "fg3m", "fg3a", "ftm", "fta")

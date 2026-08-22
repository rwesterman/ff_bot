"""The league's weekly payout schedule.

Every week pays the high scorer. Most weeks also pay a side contest. Both pots are split evenly when
teams tie, so the recorded payouts always sum back to the pot exactly.

Contest weeks are the league's own numbering. `espn_week` is the ESPN scoring period that actually
holds the games, because the two drift: Thanksgiving fell in ESPN week 13 in 2025 and falls in week 12
in 2026.
"""

from dataclasses import dataclass


HIGH_SCORE_POT_CENTS = 2000
SIDE_CONTEST_POT_CENTS = 2000

# ESPN exposes conference only as a pro-team abbreviation, so the split is maintained here.
AFC_TEAMS = frozenset(
    {
        "BUF",
        "MIA",
        "NE",
        "NYJ",
        "BAL",
        "CIN",
        "CLE",
        "PIT",
        "HOU",
        "IND",
        "JAX",
        "TEN",
        "DEN",
        "KC",
        "LV",
        "LAC",
    }
)
NFC_TEAMS = frozenset(
    {
        "DAL",
        "NYG",
        "PHI",
        "WSH",
        "CHI",
        "DET",
        "GB",
        "MIN",
        "ATL",
        "CAR",
        "NO",
        "TB",
        "ARI",
        "LAR",
        "SF",
        "SEA",
    }
)


@dataclass(frozen=True, slots=True)
class Contest:
    week: int
    name: str
    rule: str
    resolver: str
    espn_week: int | None = None
    pot_cents: int = SIDE_CONTEST_POT_CENTS
    # Weeks the resolver must read. Cumulative contests need every week up to their own.
    lookback_to: int = 0

    def source_week(self) -> int:
        return self.espn_week if self.espn_week is not None else self.week

    def weeks_required(self) -> tuple[int, ...]:
        last = self.source_week()
        first = self.lookback_to or last
        return tuple(range(first, last + 1))


SCHEDULE: tuple[Contest, ...] = (
    Contest(
        week=1,
        name="Opening Week Bang",
        rule="Team with the highest-scoring starter in Week 1.",
        resolver="highest_scoring_starter",
    ),
    Contest(
        week=2,
        name="Ronnie's Failed Rule: AFC Edition",
        rule="Team with the highest combined score from AFC-team players in their starting lineup.",
        resolver="most_afc_points",
    ),
    Contest(
        week=3,
        name="Ronnie's Failed Rule: NFC Edition",
        rule="Team with the highest combined score from NFC-team players in their starting lineup.",
        resolver="most_nfc_points",
    ),
    Contest(
        week=4,
        name="Early Season Draft King",
        rule="Team with the RB/WR/TE who scores the most cumulative points through Week 4.",
        resolver="best_cumulative_skill_player",
        lookback_to=1,
    ),
    Contest(
        week=5,
        name="First Down Frenzy",
        rule="Team with the most combined first-down bonus points from their starting lineup.",
        resolver="most_first_down_points",
    ),
    Contest(
        week=6,
        name="Kicks out for Harambe",
        rule="Team with the highest total Kicker points.",
        resolver="highest_kicker_points",
    ),
    Contest(
        week=7,
        name="Big D Energy",
        rule="Team with the highest-scoring Defense/Special Teams.",
        resolver="highest_defense_points",
    ),
    Contest(
        week=8,
        name="Keeper King",
        rule="Team whose keeper scored the most cumulative points through Week 8.",
        resolver="best_keeper",
        lookback_to=1,
    ),
    Contest(
        week=9,
        name="Gronk Memorial Challenge",
        rule="Starting TE with receiving yards closest to 69 (over or under). Nice.",
        resolver="tight_end_closest_to_69",
    ),
    Contest(
        week=10,
        name="Ground Game",
        rule="Any starting player (any position) with the most rushing yards, leaguewide.",
        resolver="most_rushing_yards",
    ),
    Contest(
        week=11,
        name="Target Practice",
        rule="Any starting player with the most receptions, leaguewide. Tiebreaker: receiving yards.",
        resolver="most_receptions",
    ),
    Contest(
        week=12,
        name="Thanksgiving Feast",
        rule="Team with a rostered player (starter or bench) scoring the most across the Wed-Fri slate.",
        resolver="best_thanksgiving_player",
    ),
    Contest(
        week=13,
        name="Penalty Pope",
        rule=(
            "Team whose starting offensive players, including K, committed the most Unsportsmanlike "
            "Conduct / Taunting penalties this season, at +10 pts per penalty (house rule)."
        ),
        resolver="manual_penalties",
        lookback_to=1,
    ),
    Contest(
        week=14,
        name="Waiver Whisperer",
        rule="Best waiver pickup of the season, ranked by points scored while started since being added.",
        resolver="best_waiver_pickup",
        lookback_to=1,
    ),
)

BY_WEEK: dict[int, Contest] = {contest.week: contest for contest in SCHEDULE}


def contest_for_week(week: int) -> Contest | None:
    return BY_WEEK.get(week)

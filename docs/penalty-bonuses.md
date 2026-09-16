# Unsportsmanlike conduct bonuses

Set `PENALTY_CHANNEL_ID` to the numeric Discord channel ID for announcements. The bot needs View Channel and Send
Messages there. Polling starts when Discord is ready and repeats every ten minutes, independently of chat-history Q&A.
An unset/blank channel ID disables polling. Existing recorded bonuses still apply to scores.

Use `/penalties 1` (or another week from 1 to 18) to inspect the current league/season's recorded awards. Like the other
bot commands, this is a `/`-prefixed chat command. It forces a fresh ESPN penalty and fantasy-lineup lookup for that week,
records new eligible bonuses silently, then displays the updated ledger. Weeks beyond the current fantasy week are rejected.
New awards discovered this way are marked Silent and will not be announced by later scheduled polls, even after a restart.
Existing pending announcements from scheduled polls remain pending. The refresh works even without `PENALTY_CHANNEL_ID`.
The table lists fantasy team, player, penalty, bonus, and announcement status (Sent/Pending/Silent), with eight rows per message
and a total across all pages. Long names are shortened for readability; full names and descriptions remain in SQLite.
All recorded awards are included. An empty result means no eligible bonuses are logged after the refresh.
Feed or database failures produce an error rather than presenting an old table as freshly updated.

The public ESPN scoreboard and detailed play-by-play JSON feeds supply regular-season games and penalized athlete IDs.
These are undocumented public endpoints and may change. Failures are logged and retried, not treated as a clean week.
The first poll after each restart checks Weeks 1 through the current week; subsequent polls revisit the current and
previous weeks. This catches downtime and week rollover. Feed publication can lag the ten-minute polling interval.

Every recorded Unsportsmanlike Conduct or Taunting penalty earns ten points if its ESPN athlete ID matches an offensive
player (QB/RB/WR/TE/FB/K) in an active slot, including flex, in that week's ESPN fantasy box score. Bench, IR, free agents,
and defensive players are excluded. Declined and offsetting flags count, since the rule concerns being flagged.
Ambiguous/unattributed penalties are logged and skipped; the bot never guesses from a touchdown scorer's identity.
Multiple penalty clauses on a play are matched to penalized participants by ESPN short name; ambiguous names are skipped.

`penalty_bonuses` lives in `LEAGUE_DB` (default `data/league.db`), alongside weekly contest records. Chat/RAG and rules
indexes remain in `CHAT_HISTORY_DB`. Production sets `LEAGUE_DB=/data/league.db` on the existing persistent volume.
Rows include league/season, fantasy team ID/name, player ID/name, penalty name,
description, NFL game/play ID, occurrence, scoring week, ten points, and notification status. A unique event key prevents
duplicate points on repeated polls or restarts. Awards are recorded before announcements; failed sends remain pending.
Delivery is at least once: a process crash after Discord accepts a message but before SQLite records its ID can cause
a repeated announcement, but never a repeated bonus. Run only one bot process.

Current-week announcements use the requested wording. Catch-up announcements name the historical week explicitly.
The bot applies local bonuses to `/scores`, `/final`, close scores, trophies and projections. `/standings` adjusts ESPN's
head-to-head wins/losses/ties when a completed regular-season result flips, uses adjusted scores for top-half wins, and
adds bonuses to the points-for tiebreaker. Current-week bonuses do not change completed-week standings.
This does not write scoring adjustments back to ESPN. Do not also add the same points manually to ESPN scores.

Standings retain the repository's existing single-week regular-season matchup and top-half-win rules. Multiweek playoff
score displays add bonuses across the matchup's scoring weeks. Playoff results do not change regular-season standings.
Later feed retractions or commissioner lineup edits do not automatically revoke a persisted award; review such changes
in the ledger. Back up both databases; a backup of `chat_history.db` does not include league records.

When switching from shared storage, no records are migrated or deleted. The new ledger starts empty, so the first
enabled penalty poll rediscovers past awards and sends their announcements again. Rebuild Week 1 contest payouts with
`/weeklycontest settle 1`, then check `/weeklycontest 1` and `/weeklycontest totals`.

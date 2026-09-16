# Unsportsmanlike conduct bonuses

Set `PENALTY_CHANNEL_ID` to the numeric Discord channel ID for announcements. The bot needs View Channel and Send
Messages there. Polling starts when Discord is ready and repeats every ten minutes, independently of chat-history Q&A.
An unset/blank channel ID disables polling. Existing recorded bonuses still apply to scores.

Use `/penalties 1` (or another week from 1 to 18) to inspect the current league/season's recorded awards. Like the other
bot commands, this is a `/`-prefixed chat command. It reads SQLite only and does not trigger a poll or award points.
The table lists fantasy team, player, penalty, bonus, and announcement status (Sent/Pending), with eight rows per message
and a total across all pages. Long names are shortened for readability; full names and descriptions remain in SQLite.
Both sent and pending awards are included. An empty result means nothing has been logged, not that ESPN has no flags.

The public ESPN scoreboard and detailed play-by-play JSON feeds supply regular-season games and penalized athlete IDs.
These are undocumented public endpoints and may change. Failures are logged and retried, not treated as a clean week.
The first poll after each restart checks Weeks 1 through the current week; subsequent polls revisit the current and
previous weeks. This catches downtime and week rollover. Feed publication can lag the ten-minute polling interval.

Every recorded Unsportsmanlike Conduct or Taunting penalty earns ten points if its ESPN athlete ID matches an offensive
player (QB/RB/WR/TE/FB/K) in an active slot, including flex, in that week's ESPN fantasy box score. Bench, IR, free agents,
and defensive players are excluded. Declined and offsetting flags count, since the rule concerns being flagged.
Ambiguous/unattributed penalties are logged and skipped; the bot never guesses from a touchdown scorer's identity.
Multiple penalty clauses on a play are matched to penalized participants by ESPN short name; ambiguous names are skipped.

`penalty_bonuses` lives in `CHAT_HISTORY_DB` (default `data/chat_history.db`), alongside the existing tables. Production's
existing `/data` volume persists it. Rows include league/season, fantasy team ID/name, player ID/name, penalty name,
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
in the ledger. Keep database backups with the existing chat-history backup process.

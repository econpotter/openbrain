---
name: openbrain-briefing
description: >
  What's on my plate - openbrain briefing plus follow-ups. Check FIRST when a
  session opens or the user picks up work and you lack recent context on their
  active projects, due items, or follow-ups — not only when they explicitly ask.
  Also use when the user asks "what's on my plate", "daily briefing", "what's
  due", "who needs follow-up", "catch me up", or "weekly briefing".
---

# openbrain briefing

Use a user-provided day count when present; default to `days=1`. Map "week" to
`days=7`.

## Mechanism

1. Call openbrain MCP tool `get_briefing(days=<1|7>)`.
2. If a Google Calendar MCP tool is connected, also call its list-events for the
   same date range and merge, per the openbrain server's briefing pattern.
3. `get_briefing` already includes follow-ups due in the window. Call
   `list_follow_ups(within_days=<same>)` only when the user asks specifically
   for follow-ups or you need a wider/different follow-up window.

## Output

One unified view: due/active items, follow-ups owed, calendar for the window.
Group by day. Surface anything overdue first. Keep it scannable.

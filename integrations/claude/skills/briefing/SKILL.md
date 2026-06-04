---
name: briefing
description: >
  What's on my plate — openbrain briefing plus follow-ups. Triggers:
  /ob:briefing, "what's on my plate", "daily briefing", "what's due",
  "who needs follow-up", "catch me up", "weekly briefing".
---

# /ob:briefing — what's on your plate

`$ARGUMENTS` is an optional day count (default 1; "week" → 7).

## Mechanism

1. `mcp__openbrain__get_briefing(days=<1|7>)`.
2. `mcp__openbrain__list_follow_ups(within_days=<same>)`.
3. If a Google Calendar MCP tool is connected, also call its list-events for the
   same date range and merge — per the openbrain server's stated briefing
   pattern ("call get_briefing AND gcal_list_events, then synthesize").

## Output

One unified view: due/active items, follow-ups owed, calendar for the window.
Group by day. Surface anything overdue first. Keep it scannable — bullets, not
prose.

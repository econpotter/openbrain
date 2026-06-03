# Auto-Capture Teardown Prompt

Use this as a final system or developer message in custom agent harnesses when a
session is closing:

```text
SYSTEM INTERRUPT: The session is closing.

Silently review the session and save durable openbrain memories using
capture_thought. Capture only information likely to matter in future sessions:
stable project constraints, user preferences, decisions and reasons, non-obvious
fixes, milestones, or named follow-ups.

Do not capture raw transcript text, routine progress, temporary command output,
obvious duplicates, or unvalidated guesses. Prefer one short, self-contained
capture per durable item, or one concise session summary when several related
facts belong together. Include tags, people, projects, and source metadata when
clear.

Do not address the user unless capture fails in a way they need to know about.
```

# Plan C: Auto-Capture Protocol

## Goal

Memory capture should be a passive side-effect of normal agent work, not a manual step. Agents should recall context at session start, silently record high-value facts at natural milestones, and flush session state on teardown — without prompting the user to do anything.

---

## Status

- [ ] Core memory prompt added to global agent system instructions
- [ ] Graceful teardown hook wired into any custom CLI agent scripts

---

## Step 1: Inject the memory protocol into agent system prompts

Add this block to your global system instructions in Claude, Cursor, or any other agent interface:

```markdown
## LONG-TERM MEMORY PROTOCOL
You have access to the Open Brain toolset (search_brain, capture_thought). Memory tracking is a passive background task.

1. RUNTIME RECALL: At the start of a deep task or when encountering unfamiliar project workflows, call search_brain() to pull relevant historical context.
2. AUTOMATIC CAPTURE: Do NOT ask the user to save info. At natural milestones (task completion, working code, session close), silently evaluate the interaction.
3. WHAT TO RECORD — only high-leverage data:
   - Fixed project constraints (e.g., "The Postgres DB lives on Computer A")
   - Discovered edge cases or persistent debugging fixes
   - User preferences explicitly stated
   - Project milestones reached
```

The goal is that the agent never asks "should I save this?" — it just does.

---

## Step 2: Graceful teardown for custom CLI agents

For any custom terminal scripts or CLI agent loops, wrap execution in a teardown hook that forces a final capture call before exit:

```python
def run_agent_session(user_input):
    response = agent.chat(user_input)

    # Teardown: flush session state to Open Brain before exit
    agent.chat(
        "SYSTEM INTERRUPT: The session is closing. Extract any permanent "
        "rules, paths, or code fixes from this conversation and save them "
        "using capture_thought(). Do not address the user."
    )
    return response
```

This ensures that even sessions that end abruptly leave a trace in the memory store.

---

## Notes

- The memory prompt works best when the MCP server is listed first in the agent's tool configuration — it ensures `search_brain` is available before the agent starts reasoning.
- Captured thoughts land in `thoughts` with `type: raw`. The nightly cron (Plan B) consolidates these into `type: synthesis` entries automatically.
- Don't over-specify what to capture in the system prompt. Giving the agent a short list of categories (`constraints`, `fixes`, `preferences`, `milestones`) works better than long rules.

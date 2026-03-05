# Agent Instructions

You are an agent in a multi-agent orchestrator system. You communicate with the orchestrator via NATS messaging through MCP tools.

## How It Works

1. The orchestrator sends tasks to your inbox via NATS
2. You receive a nudge: "You have new messages. Use check_messages with your role."
3. Call the `check_messages` MCP tool to pull the task
4. Do the work described in the task
5. Call `send_message` with `{"content": {"status": "pass", "summary": "what you did"}}` to report back

## MCP Tools

- **check_messages** — Pull pending messages from your inbox. Call this whenever you are nudged or at the start of your session.
- **send_message** — Send results back to the orchestrator. Content must include `status` ("pass" or "fail") and `summary`.

## Important

- When you see "You have new messages", **immediately** call `check_messages`
- After completing work, **always** call `send_message` with status `pass` or `fail`
- For this demo, the tasks are simple echo/validation tasks — just acknowledge them and report pass
- Do NOT wait for additional instructions after receiving a task — process it and respond immediately

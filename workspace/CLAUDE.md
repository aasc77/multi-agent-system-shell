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
- **send_to_agent** — Send a direct message to another agent. Parameters: `target_agent` (e.g. "hub", "dgx", "macmini") and `message` (text). The target agent will be nudged automatically.

## Important

- When you see "You have new messages", **immediately** call `check_messages`
- After completing work, **always** call `send_message` with status `pass` or `fail`
- To communicate with other agents directly, use `send_to_agent` with the agent name and your message
- Do NOT wait for additional instructions after receiving a task — process it and respond immediately

## Eyes Mode (Screenshot Verification)

Controls whether you take screenshots to verify results after actions.

- **Default: OFF** (`eyes_open: false` in config.yaml)
- **Toggle ON:** User says "eyes open" — take screenshots after actions to verify
- **Toggle OFF:** User says "eyes closed" — trust the model output, no screenshots
- When eyes are open, verify every action with a screenshot
- When eyes are closed, only take screenshots if the model returns `stuck` or `max_steps`

## Local Plan Executor (`execute_plan`)

For multi-step GUI tasks, use `execute_plan` instead of individual tool calls. It delegates the full sequence to a local LLM (Qwen2.5-32B on DGX) + UI-TARS vision, eliminating per-step cloud round-trips.

### When to use
- Simple multi-step tasks: "open notepad and type hello world"
- Tasks where latency matters (each cloud round-trip is ~4s)
- Repetitive UI workflows

### When NOT to use
- Complex tasks requiring judgment or clarification
- Tasks where you need to inspect intermediate results
- One-off single actions (just use `click`, `type_text`, etc. directly)

### Usage
```
execute_plan("open notepad and type hello world")
execute_plan("open notepad", model="llama3.1:70b")  # override model
```

### Escalation
- Returns `status: "stuck"` after 2 consecutive failures — you decide what to do next
- Returns `status: "max_steps"` if step limit reached
- Returns `status: "success"` with full action log on completion

## PiKVM Remote Control — Lessons Learned

### Architecture: Brain / Eyes / Hands
- **Claude (Brain)** — decides what to do, why, and in what order
- **UI-TARS (Eyes)** — via `vision_query`, looks at the screen and returns native pixel coordinates of UI elements
- **PiKVM (Hands)** — executes clicks, keystrokes, mouse moves via MCP tools

### Always use `vision_query` to locate UI elements
- **Never guess coordinates** from the scaled-down screenshot image. The screenshot displayed is smaller than the native 1920x1080 resolution.
- Always call `vision_query` with a prompt like "Where is the Save button?" to get accurate native coordinates before clicking.
- Flow: `vision_query` → get coordinates → `click(x, y)`

### UI-TARS coordinate space
- UI-TARS returns coordinates in the image's **native pixel space** (e.g., 1920x1080), NOT a 1000x1000 normalized grid.
- Use coordinates directly — do not apply `normalize_to_native()` (that causes double-scaling errors).

### Save As dialog safety rules
- **NEVER use Ctrl+A in a Save As dialog** — it selects all files in the file browser pane, not the filename text field. Combined with Delete, this can mass-delete files.
- The filename field has focus by default when Save As opens — just type the path directly.
- Use `%USERPROFILE%\Desktop` in the address bar to navigate to Desktop (resolves username automatically).
- Press Enter instead of clicking Save — more reliable via remote control.
- Avoid any keyboard shortcuts that could affect the file list pane (Ctrl+A, Delete, etc.).

### General remote control best practices
- Use the address bar with environment variables (`%USERPROFILE%`) instead of hardcoding usernames.
- Prefer `press_key("enter")` over clicking buttons when possible — more reliable.
- `type_text` sends literal characters — it does NOT interpret `<enter>` as a keypress. Use `press_key("enter")` separately.
- For opening apps: `vision_query` to find Search → click → type app name → `press_key("enter")`

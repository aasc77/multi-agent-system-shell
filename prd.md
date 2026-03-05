# Multi-Agent System Shell (MAS) -- PRD

## Source Template

This project is based on the **orchestrator-template** (`~/Repositories/orchestrator-template/`).

## Problem

The orchestrator-template repo works but it's limited:
- Hardcoded agent counts (3 and 2)
- File-based mailboxes (JSON on local disk) -- single machine only
- Hardcoded state machines (RGR enums, BDD enums)
- tmux layout is hardcoded per project

Building a new multi-agent pipeline means forking an existing repo and rewriting the agent wiring, state machine, mailbox handlers, and tmux layout every time.

## Vision

A **generic shell** where you define agents, their communication flow, and the state machine in YAML -- then run one command and get a consolidated terminal with all agents visible, communicating over NATS, driven by a config-driven state machine.

**One command. N agents. Full visibility.**

## Core Requirements

### R1: tmux Session Layout

Everything runs inside **tmux** -- works on macOS and Linux. No iTerm2 dependency. On macOS you attach from iTerm2 or Terminal.app; on Linux you attach from any terminal.

When the user runs `./start.sh <project>`, a tmux session is created with two windows:

**Window 1: "control" -- orchestrator + NATS monitor (side-by-side panes):**
```
┌──────────────────┬──────────────────┐
│   orchestrator   │   nats-monitor   │
│                  │                  │
│  (state machine, │  (all messages   │
│   task progress, │   flowing in     │
│   commands)      │   real-time)     │
└──────────────────┴──────────────────┘
```

**Window 2: "agents" -- all agent panes in a tiled grid:**

Layout varies by agent count. tmux `tiled` layout auto-arranges. No explicit upper bound on agent count -- limited only by screen real estate.

**2 agents** (e.g., writer + executor):
```
┌──────────────────┬──────────────────┐
│     writer       │    executor      │
│                  │                  │
│  Claude Code     │  echo_agent.py   │
│                  │                  │
└──────────────────┴──────────────────┘
```

**3 agents** (e.g., RGR: qa + dev + refactor):
```
┌──────────────────┬──────────────────┐
│       qa         │      dev         │
│                  │                  │
│  Claude Code     │  Claude Code     │
│                  │                  │
├──────────────────┴──────────────────┤
│             refactor                │
│                                     │
│           Claude Code               │
│                                     │
└─────────────────────────────────────┘
```

**4 agents** (e.g., analyst + coder + reviewer + tester):
```
┌──────────────────┬──────────────────┐
│    analyst       │     coder        │
│                  │                  │
│  Claude Code     │  Claude Code     │
│                  │                  │
├──────────────────┼──────────────────┤
│    reviewer      │     tester       │
│                  │                  │
│  Claude Code     │  script agent    │
│                  │                  │
└──────────────────┴──────────────────┘
```

**5 agents** (e.g., planner + frontend + backend + qa + deployer):
```
┌────────────┬────────────┬────────────┐
│  planner   │  frontend  │  backend   │
│            │            │            │
│ Claude Code│ Claude Code│ Claude Code│
│            │            │            │
├────────────┴──────┬─────┴────────────┤
│        qa         │     deployer     │
│                   │                  │
│    Claude Code    │   script agent   │
│                   │                  │
└───────────────────┴──────────────────┘
```

**6 agents** (e.g., large pipeline with SSH remote agents):
```
┌────────────┬────────────┬────────────┐
│  analyst   │   writer   │  reviewer  │
│            │            │            │
│ Claude Code│ Claude Code│ Claude Code│
│  (local)   │  (local)   │ (ssh:dgx1) │
│            │            │            │
├────────────┼────────────┼────────────┤
│  executor  │   tester   │  deployer  │
│            │            │            │
│ script     │ Claude Code│  script    │
│  (local)   │ (ssh:dgx1) │  (local)   │
│            │            │            │
└────────────┴────────────┴────────────┘
```

**NATS monitor pane:** The right pane in the control window runs `nats sub "agents.>"`, which subscribes to all agent subjects and prints every NATS message in real-time. This is the primary visibility surface for debugging message flow. The `scripts/nats-monitor.sh` wrapper script is used (accepts an optional subject filter argument, defaults to `agents.>`).

Pane titles MUST be enabled (`pane-border-status top`) so each pane shows the agent name.

`start.sh` creates the tmux session. `stop.sh` kills it. Attach from any terminal with `tmux attach -t <session>`.

### R2: Config-Driven Agents (N agents from YAML)

Agents are defined in a project config file. No code changes needed to add/remove agents.

```yaml
agents:
  writer:
    runtime: claude_code        # or: script
    working_dir: /path/to/dir
    system_prompt: "You are..."
  executor:
    runtime: script
    command: "python3 agents/echo_agent.py --role executor"
  reviewer:
    runtime: claude_code
    ssh_host: dgx1.local       # remote agent via SSH
```

**Runtimes:**
- `claude_code` -- launches Claude Code CLI with a per-agent MCP config file
- `script` -- runs any command that speaks NATS directly

**MCP bridge per agent:** `start.sh` MUST generate a separate MCP config file for each `claude_code` agent at `projects/<name>/.mcp-configs/<agent_name>.json`. Each config spawns the MCP bridge (`mcp-bridge/index.js`) with environment variables that bake in the agent's identity:

```json
{
  "mcpServers": {
    "mas-bridge": {
      "command": "node",
      "args": ["<root>/mcp-bridge/index.js"],
      "env": {
        "AGENT_ROLE": "<agent_name>",
        "NATS_URL": "nats://localhost:4222",
        "WORKSPACE_DIR": "<working_dir or project_dir>"
      }
    }
  }
}
```

Claude Code is launched with `claude --mcp-config <path_to_config>`. The `AGENT_ROLE` env var tells the MCP bridge which NATS subjects to use -- `check_messages` pulls from `agents.<AGENT_ROLE>.inbox` and `send_message` publishes to `agents.<AGENT_ROLE>.outbox`. The agent does NOT pass its role as a parameter; the role is baked into the MCP bridge process.

**Collaboration model:** Agents don't need distinct roles. All agents can remain as "collaborators" -- same runtime, same capabilities, just working on different tasks or different parts of the same task. The state machine defines the flow, not the agent identity. You can have 4 identical Claude Code agents all collaborating on one codebase.

**Remote agents:** If `ssh_host` is set, the pane SSHs into that machine before launching the agent.

### R3: Communication Flow

There are two communication layers. NATS carries data. tmux carries notifications.

```
┌─────────────────────────────────────────────────────────┐
│                     NATS JetStream                      │
│                  (data layer -- messages)                │
│                                                         │
│  agents.writer.inbox    agents.writer.outbox             │
│  agents.executor.inbox  agents.executor.outbox           │
│                                                         │
└────────┬──────────────────────────────┬─────────────────┘
         │                              │
         ▼                              ▼
┌─────────────────┐            ┌─────────────────┐
│  Orchestrator   │            │     Agent       │
│                 │            │   (Claude Code)  │
│  - Publishes    │            │                  │
│    task to NATS │            │  - Calls         │
│    inbox        │            │    check_messages│
│                 │            │    MCP tool      │
│  - Then nudges  │            │                  │
│    via tmux     │            │  - MCP bridge    │
│    send-keys    │──tmux──────▶    pulls from    │
│    "check your  │  nudge     │    NATS inbox    │
│     messages"   │            │                  │
│                 │            │  - Does work     │
│  - Subscribes   │            │                  │
│    to NATS      │◀───NATS────│  - Calls         │
│    outbox for   │   outbox   │    send_message  │
│    responses    │            │    MCP tool      │
└─────────────────┘            └─────────────────┘
```

**Step by step:**
1. Orchestrator publishes task assignment to `agents.<role>.inbox` via NATS
2. Orchestrator nudges the agent's tmux pane via `tmux send-keys`: "You have new messages. Use check_messages with your role."
3. Claude Code sees the nudge text, calls the `check_messages` MCP tool
4. MCP bridge pulls the message from NATS JetStream inbox, returns it to Claude Code
5. Agent does the work (writes code, runs tests, etc.)
6. Agent calls `send_message` MCP tool with results
7. MCP bridge publishes to `agents.<role>.outbox` via NATS
8. Orchestrator picks up the outbox message, runs state machine transition, assigns next agent

**Why two layers:**
- **NATS** is the real message bus -- structured JSON payloads, persistent, cross-machine
- **tmux send-keys** is just a notification tap -- Claude Code has no push notification mechanism, so the orchestrator types a reminder into its pane to trigger a `check_messages` call
- Script agents don't need tmux nudges -- they subscribe to NATS directly and get messages pushed to them

**NATS details:**
- Subject convention: `agents.<role>.inbox` / `agents.<role>.outbox`
- Stream: `AGENTS` covering `agents.>`
- Port: 4222 (default)
- Auth: none (future: token-based)
- JetStream retention: limits policy, max 10,000 messages, max age 1 hour
- Max message size: 1MB (NATS default)
- Durable consumers so messages persist across restarts
- Works cross-machine (agent on Mac, agent on DGX-1, same NATS server)

**Two messaging interfaces:**
1. **MCP bridge** (for Claude Code agents) -- `send_message(content)` and `check_messages()` tools. Both are parameterless regarding identity -- the bridge knows its own role from the `AGENT_ROLE` env var. `send_message` publishes to `agents.<AGENT_ROLE>.outbox`. `check_messages` pulls from `agents.<AGENT_ROLE>.inbox`.
2. **Direct NATS** (for script agents) -- subscribe to inbox, publish to outbox

**Inbox message schema (orchestrator -> agent):**

```json
{
  "type": "task_assignment",
  "task_id": "demo-1",
  "title": "Echo test",
  "description": "What needs to be done",
  "message": "Extra instructions from orchestrator (empty string if none)"
}
```

**`all_done` message:**

When all tasks are `completed` or `stuck`, the orchestrator MUST publish an `all_done` message to every agent's inbox:

```json
{
  "type": "all_done",
  "summary": "All tasks processed: X completed, Y stuck"
}
```

**Agent behavior on `all_done`:**
- **Claude Code agents:** The MCP bridge returns the `all_done` message via `check_messages`. The agent MUST NOT send an outbox response. The agent stays alive (Claude Code session remains open) -- the user may want to interact with it directly. The orchestrator MUST NOT nudge agents after sending `all_done`.
- **Script agents:** MUST exit cleanly (exit code 0) upon receiving `all_done`.

**Outbox message schema (agent -> orchestrator):**

All messages on the outbox MUST follow this JSON schema so the orchestrator can map them to state machine triggers:

```json
{
  "type": "agent_complete",
  "status": "pass | fail",
  "summary": "What the agent did",
  "files_changed": ["src/foo.py", "tests/test_foo.py"],
  "error": "Optional error details if status is fail"
}
```

Required fields:
- `type` -- must be `"agent_complete"` (maps to the `agent_complete` trigger in the state machine)
- `status` -- `"pass"` or `"fail"` (matches the `status` condition on transitions)

OPTIONAL fields. Agents MAY omit them:
- `summary` -- human-readable description of work done
- `files_changed` -- list of files modified
- `error` -- error details when `status` is `"fail"`

The orchestrator maps these fields to state machine lookups:
- `type` -> `trigger` (e.g., `"agent_complete"` matches transitions with `trigger: agent_complete`)
- the agent's role -> `source_agent` (e.g., writer outbox matches transitions with `source_agent: writer`)
- `status` -> `status` condition (e.g., `"pass"` matches transitions with `status: pass`)

**Unrecognized messages:**

If an outbox message has a `type` value that does not match any transition's `trigger` field, OR if the message is missing required fields (`type`, `status`), the orchestrator MUST:
1. Log a warning with the full message payload: `"Unrecognized outbox message from <role>: <payload>"`
2. Discard the message (ACK it so it doesn't re-deliver)
3. NOT change state -- the state machine stays in its current state
4. NOT increment the task's attempt counter

This is distinct from a matched `type` with no matching transition (e.g., `agent_complete` from the wrong `source_agent` for the current state). In that case the orchestrator MUST also log a warning and discard, but the log message MUST indicate "no matching transition" rather than "unrecognized type."

**Orchestrator-generated triggers:**

Some triggers are fired by the orchestrator itself, not by agent messages:
- `task_assigned` -- fired when the orchestrator picks up a pending task

### R4: Config-Driven State Machine

Replaces the hardcoded `RGRState` enum and per-agent handler functions. States and transitions defined in YAML.

```yaml
state_machine:
  initial: idle
  states:
    idle: { description: "No active task" }
    waiting_writer: { agent: writer }
    waiting_executor: { agent: executor }
    blocked: { description: "Human intervention needed" }

  transitions:
    - from: idle
      to: waiting_writer
      trigger: task_assigned
      action: assign_to_agent
      action_args: { target_agent: writer }

    - from: waiting_writer
      to: waiting_executor
      trigger: agent_complete
      source_agent: writer
      status: pass
      action: assign_to_agent
      action_args: { target_agent: executor }

    - from: waiting_writer
      to: idle
      trigger: agent_complete
      source_agent: writer
      status: fail
      action: flag_human

    - from: waiting_executor
      to: idle
      trigger: agent_complete
      source_agent: executor
      status: pass

    - from: waiting_executor
      to: waiting_writer
      trigger: agent_complete
      source_agent: executor
      status: fail
      action: assign_to_agent
      action_args: { target_agent: writer, message: "Tests failed. Fix and re-send." }
```

When `status: fail` matches a transition, that transition fires. The retry logic in R5 (reset to initial, re-fire `task_assigned`) only applies when `status: fail` does NOT match any transition from the current state.

**Built-in actions:**
- `assign_to_agent` -- publish task to agent's NATS inbox, nudge agent's tmux pane
- `flag_human` -- set state to blocked, print alert to orchestrator console

**Startup validation:** The orchestrator MUST validate the state machine config on startup and exit with code 1 if any of these checks fail:
- `initial` MUST reference a state defined in `states`
- Every transition's `from` and `to` MUST reference a state defined in `states` (except `from: "*"`)
- Every `action` MUST be a recognized built-in action
- Every `source_agent` MUST reference an agent defined in the `agents` config
- Every `target_agent` in `action_args` MUST reference an agent defined in the `agents` config
- At least one transition MUST exist

### R5: Task Queue

JSON file with tasks. Orchestrator processes them sequentially.

```json
{
  "tasks": [
    {
      "id": "task-1",
      "title": "Do the thing",
      "description": "Details...",
      "status": "pending",
      "attempts": 0
    }
  ]
}
```

**Task statuses:** `pending` -> `in_progress` -> `completed` | `stuck`

**Task completion rule:** When a transition returns the state machine to the `initial` state (as defined by `state_machine.initial`) AND the transition has no `action` (or the action is not `assign_to_agent`), the orchestrator MUST mark the current task as `completed` and move to the next pending task. This is how the state machine signals "done" -- returning to initial without assigning another agent means the pipeline is finished for this task.

**Max attempts:** Configurable via `tasks.max_attempts_per_task` in config.

**Retry behavior on `status: fail`:**
1. The orchestrator increments the task's `attempts` counter.
2. If `attempts < max_attempts_per_task`: the orchestrator resets the state machine to `initial` (idle) and fires the `task_assigned` trigger again. This re-enters the state machine from the top, re-assigning the task to the first agent in the pipeline. The task stays `in_progress`.
3. If `attempts >= max_attempts_per_task`: the task is marked `stuck` and skipped.

**What "stuck" means:** The task exceeded its max retry count. The orchestrator MUST log a warning, mark the task `stuck`, and move to the next pending task. It MUST NOT exit or block.

**After all tasks:** When all tasks are `completed` or `stuck`, the orchestrator sends `all_done` messages to all agents and continues polling (stays alive for interactive commands). It logs a summary: `"All tasks processed: X completed, Y stuck"`.

### R6: tmux Communication (Orchestrator -> Agents)

The orchestrator communicates with agent panes via `tmux send-keys`. This is how the template works today and it carries over.

**How it works:**
1. `start.sh` creates a tmux session with named panes per agent
2. The orchestrator knows each agent's tmux target from the session/window/pane naming convention
3. When the orchestrator needs to nudge an agent (e.g., "you have new messages"), it runs `tmux send-keys -t <target> "nudge text" Enter`
4. When the user types `msg writer "fix the tests"`, the orchestrator types that text into the writer's tmux pane

**Nudge flow:**
```
Orchestrator publishes task to NATS (agents.writer.inbox)
  -> tmux send-keys to writer's pane: "You have new messages. Use check_messages with your role."
  -> Claude Code agent sees the nudge, calls check_messages MCP tool
  -> MCP bridge pulls from NATS inbox, returns the message
```

**Config:**
```yaml
tmux:
  session_name: demo
  nudge_prompt: "You have new messages. Use check_messages with your role."
  nudge_cooldown_seconds: 30
```

Nudge cooldown prevents stacking multiple nudges on an agent that's already working. The orchestrator tracks last-nudge timestamps per agent.

**Safe nudging:**

`tmux send-keys` sends text to whatever process owns stdin in the pane. If Claude Code is running a subprocess (tests, builds, git), the nudge text goes to that subprocess's stdin -- which could break a build, corrupt output, or be silently swallowed.

Mitigation: before nudging, the orchestrator checks what process is in the foreground:

```bash
tmux display-message -p -t <target> '#{pane_current_command}'
```

- If the foreground process is `claude` -> safe to nudge, send the text
- If it's anything else (`node`, `python`, `git`, `npm`, etc.) -> skip the nudge, retry on next cooldown cycle
- If the orchestrator has skipped nudges for a single agent more than `max_nudge_retries` times (default: 20, ~10 minutes at 30s cooldown), it escalates to `flag_human` and logs a warning: "Agent <name> appears stuck -- foreground process never returned to claude"

This is safe because:
- The NATS message is already persisted -- nothing is lost by skipping a nudge
- The cooldown timer resets, so the orchestrator will try again in N seconds
- Eventually Claude Code finishes the subprocess and returns to its prompt, and the next nudge lands correctly
- If it never returns, the max-retry escalation catches it

**Canonical tmux target format:** `<session_name>:agents.<pane_index>` where pane_index is the agent's position (0-based) in the agents window. Example: `demo:agents.0` for the first agent. The orchestrator builds a mapping from agent name to pane index at startup based on config order.

### R7: Interactive Orchestrator Console

Adapted from template. The orchestrator pane accepts typed commands:
- `status` -- current state, task, progress, NATS connection status
- `tasks` -- list all tasks with status markers
- `skip` -- skip current task (mark as `stuck`, assign next)
- `nudge <agent>` -- remind agent to check messages (types into agent's tmux pane)
- `msg <agent> <text>` -- type into agent's tmux pane. MUST perform the same `#{pane_current_command}` safe-nudge check as automated nudges (R6). If the agent's foreground process is not `claude`, the orchestrator MUST print a warning ("Agent <name> is busy -- message not sent") and NOT send the text.
- `pause` / `resume` -- pause/resume NATS message processing. Only stops processing new outbox messages. Does NOT interrupt in-progress agents. Orchestrator enters a "paused" flag; agents continue working independently.
- `log` -- show last 10 log entries
- `help` -- show all commands with available agent names

### R8: Scripts

| Script | Purpose |
|--------|---------|
| `start.sh <project>` | Create tmux session: control window (orchestrator + NATS monitor) + agents window (1 pane per agent) |
| `stop.sh <project>` | Kill tmux session, clean up generated MCP configs |
| `setup-nats.sh` | Install nats-server + nats CLI via brew, start with JetStream |
| `reset-tasks.sh <project>` | Reset all tasks to `pending`, attempts to 0 |
| `nats-monitor.sh [subject]` | Subscribe to NATS subjects and print messages in real-time. Defaults to `agents.>` |

**`start.sh` behavior:**
- Pre-flight: checks for tmux, python3, nats-server. MUST fail with clear error listing missing tools.
- If tmux session already exists: kills it first, then creates fresh (idempotent).
- If NATS not running: starts it automatically via `setup-nats.sh`.
- Exit code 0 on success, 1 on failure.

**`stop.sh` behavior:**
- Kills the tmux session. If session doesn't exist, prints "already stopped" and exits 0.
- Cleans up generated MCP config files in `projects/<name>/.mcp-configs/`.
- Does NOT stop NATS server by default. Pass `--kill-nats` to also stop NATS.

### R9: Configuration

**Two-level config with deep merge:**

Global defaults in `config.yaml`:
```yaml
llm:
  provider: ollama
  model: qwen3:8b
  base_url: http://localhost:11434
  temperature: 0.3
  disable_thinking: true

nats:
  url: nats://localhost:4222
  stream: AGENTS
  subjects_prefix: agents

tasks:
  max_attempts_per_task: 5

tmux:
  nudge_prompt: "You have new messages. Use check_messages with your role."
  nudge_cooldown_seconds: 30
  max_nudge_retries: 20
```

Project overrides in `projects/<name>/config.yaml`:
```yaml
project: demo
tmux:
  session_name: demo
agents:
  writer: { ... }
  executor: { ... }

state_machine:
  initial: idle
  states: { ... }
  transitions: [ ... ]
```

**Merge strategy:** Project config overrides global. Two levels deep -- e.g., project `tmux.session_name` overrides global `tmux.session_name` but inherits `tmux.nudge_prompt` if not specified.

**What goes where:**
- **Global**: LLM settings, NATS connection, task limits, tmux nudge defaults -- things that rarely change between projects
- **Project**: agents, state machine, tasks file, session name -- everything project-specific

### R10: LLM Client (Ollama)

Carried over from the template. The Ollama client is used by the orchestrator for **optional routing decisions** -- interpreting ambiguous agent messages or natural language commands typed into the orchestrator console.

The LLM is NOT in the critical path. All state machine transitions are determined by the structured JSON message schema (R3). The LLM is only used for:
- Interpreting unrecognized commands typed into the orchestrator console (future)
- Fallback when an agent message doesn't match any transition (logs a warning, flags human)

**Health check:** On startup, the orchestrator checks if Ollama is reachable. If not, it logs a warning and continues without LLM support -- this is non-fatal.

### R11: Logging

**Orchestrator logs:** Written to `orchestrator/orchestrator.log` and stdout. Format: `%(asctime)s [%(levelname)s] %(message)s`. MUST include state transitions, task assignments, NATS publish/subscribe events, and nudge attempts.

**Session report:** Per-project markdown file at `projects/<name>/session-report.md`. Timestamped entries for task assignments, completions, and blockers.

**Agent logs:** Agents log to their own stdout (visible in their tmux pane). No centralized agent log collection.

## Error Handling

### NATS unavailability
- **On startup:** If NATS is unreachable, the orchestrator MUST print an error with instructions (`Run: scripts/setup-nats.sh`) and exit with code 1. Fail fast, do not retry.
- **During runtime:** If NATS connection drops, `nats-py` auto-reconnects. If reconnection fails after the library's default timeout, the orchestrator MUST log an error and continue polling (durable consumers will catch up when reconnected).

### Agent crash/death
- **No heartbeat.** If an agent dies mid-task, the orchestrator has no way to detect it -- it waits for an outbox message that never comes.
- **Mitigation:** The `max_attempts_per_task` limit combined with the nudge retry escalation (`max_nudge_retries`) will eventually flag the task for human review.
- **Future:** Add a `system.health` NATS subject for heartbeats.

### SSH remote agent failures
- **Not detected.** If the SSH connection drops, the agent's tmux pane shows the disconnect. The orchestrator does not detect this -- same behavior as agent crash above.
- **Future:** Monitor `#{pane_dead}` tmux variable to detect dead panes.

## File Structure

```
Multi_Agent_System_Shell/
  config.yaml                         # Global defaults
  projects/
    <name>/
      config.yaml                     # Agents, state machine
      tasks.json                      # Task queue
  orchestrator/
    orchestrator.py                   # Main async loop
    state_machine.py                  # Generic state machine engine
    nats_client.py                    # NATS pub/sub wrapper
    llm_client.py                     # Ollama client (optional routing)
    requirements.txt
  mcp-bridge/
    index.js                          # MCP stdio server with NATS backend
    package.json
  agents/
    echo_agent.py                     # Example script agent
  scripts/
    start.sh
    stop.sh
    setup-nats.sh
    reset-tasks.sh
    nats-monitor.sh
  tests/
    test_state_machine.py
    test_nats_client.py
    test_integration.py
  docs/
  README.md
  CLAUDE.md
```

## Out of Scope (Future)

- Git integration (worktrees, branch_prefix, merge_and_assign, merge_to_default, merge conflict detection)
- Web dashboard for monitoring
- Agent hot-reload (add/remove agents without restart)
- Task dependencies (DAG instead of sequential)
- Multi-project orchestration
- Authentication/authorization between agents
- Agent heartbeat / liveness detection
- SSH connection monitoring
- LLM-powered natural language command interpreter

## Success Criteria

1. `./scripts/start.sh demo` creates tmux session with control window + agents window (1 pane per agent), all running
2. Echo agent round-trip: orchestrator assigns task via NATS -> nudges agent via `tmux send-keys` -> agent responds via NATS -> state transitions -> task completes
3. Safe nudging: orchestrator checks `#{pane_current_command}` before sending, skips if agent is running a subprocess
4. `nudge <agent>` and `msg <agent> <text>` from orchestrator console type into the correct agent's tmux pane (target format: `<session>:agents.<pane_index>`)
5. Adding a third agent requires only config changes, zero code changes
6. `./scripts/stop.sh demo` cleanly shuts everything down
7. All components have unit tests that pass
8. NATS unavailable on startup -> orchestrator exits with clear error message

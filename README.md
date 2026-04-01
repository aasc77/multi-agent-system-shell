# Multi-Agent System Shell (MAS)

A config-driven multi-agent orchestrator. Define agents, their communication flow, and the state machine in YAML -- then run one command and get a consolidated terminal with all agents visible, communicating over NATS, driven by a config-driven state machine.

**One command. N agents. Full visibility.**

## Architecture

`start.sh` automatically opens **two terminal windows** so you can arrange
control and agents side-by-side on your screen:

```
 Window 1 — control                     Window 2 — agents
┌──────────────────┬───────────────┐    ┌──────────────┬──────────────┐
│   orchestrator   │  nats-monitor │    │  dev (hub)   │  qa (macmini)│
│  (state machine  │ (live msgs)   │    │ (claude_code)│ (claude_code)│
│   + console)     │               │    ├──────────────┼──────────────┤
├──────────────────┘               │    │  dgx1        │  hassio      │
│   manager (monitor)              │    │ (claude_code)│ (claude_code)│
│  (autonomous oversight)          │    └──────────────┴──────────────┘
└──────────────────────────────────┘
            │                    │
            └────── NATS ────────┘
              JetStream pub/sub
```

Both windows share the same tmux session via grouped sessions, so each can
independently view a different tmux window.

**Supported terminals:**

| Platform | Terminal | Method |
|----------|----------|--------|
| macOS | iTerm2 | Two windows via osascript |
| Windows | Windows Terminal (WSL) | Two tabs via `wt.exe` |
| Linux | GNOME Terminal | Two windows via `gnome-terminal` |
| Linux | xterm (fallback) | Two windows via `xterm` |
| Any | Current terminal | Attaches control window, prints command for second |

### Key Concepts

- **N agents** defined in config (not hardcoded). Any runtime: `claude_code` or `script`
- **Local & remote agents**: SSH support for agents on other machines with auto-reconnect on connection loss
- **NATS JetStream** for messaging. Subject convention: `agents.<role>.inbox`
- **Config-driven state machine**: states + transitions in YAML, supports wildcards (`from: "*"`)
- **Built-in actions**: `assign_to_agent`, `merge_and_assign`, `merge_to_default`, `flag_human`
- **MCP bridge**: tools for Claude Code agents (`send_message`, `check_messages`, `send_to_agent`)
- **Manager agent**: autonomous monitor that watches task progress, agent health, and logs
- **Idle watchdog**: detects idle agents with pending tasks and alerts the manager
- **Inactivity announcer**: alerts when no agent has any NATS activity for a configurable threshold
- **Conversation mode**: streams agent-to-agent messages to home speakers via Piper TTS
- **Push notifications**: Pushover and Twilio SMS integration for external alerts
- **Pane labels**: configurable labels per agent for clear pane identification
- **Two-window iTerm layout**: control and agents in separate windows (macOS)

## Project Structure

```
multi-agent-system-shell/
├── orchestrator/          # Core orchestrator modules
│   ├── __main__.py        # Entry point (python3 -m orchestrator <project>)
│   ├── config.py          # YAML config loader (global + project merge)
│   ├── state_machine.py   # Config-driven state engine
│   ├── task_queue.py      # Task queue manager
│   ├── nats_client.py     # NATS JetStream wrapper
│   ├── router.py          # Message router + inbox relay
│   ├── tmux_comm.py       # tmux communication (nudge, clear, send)
│   ├── lifecycle.py       # Task lifecycle manager
│   ├── watchdog.py        # Idle agent detection + inactivity announcer
│   ├── console.py         # Interactive console commands
│   ├── llm_client.py      # Ollama LLM client
│   ├── logging_setup.py   # Logging configuration
│   └── session_report.py  # Session report generator
├── manager/
│   └── CLAUDE.md          # Manager agent instructions and monitoring workflow
├── agents/
│   └── echo_agent.py      # Example script agent (speaks NATS directly)
├── mcp-bridge/
│   ├── index.js           # MCP server (send_message, check_messages)
│   └── package.json
├── scripts/
│   ├── start.sh           # Launch two iTerm windows with all agents
│   ├── stop.sh            # Graceful shutdown
│   ├── setup-nats.sh      # Install and start NATS server
│   ├── reset-tasks.sh     # Reset task statuses to pending
│   ├── nats-monitor.sh    # Live NATS message monitor
│   ├── share-file.sh      # Distribute files to all agent workspaces
│   ├── tmux-paste-image.sh # Paste clipboard image into any agent pane
│   ├── ssh-reconnect.sh   # Auto-reconnect wrapper for remote SSH agents
│   ├── notify.sh          # macOS text-to-speech notification helper
│   ├── push-notify.py     # Pushover push notification script
│   ├── sms-notify.py      # Twilio SMS notification script
│   └── conversation-mode.py # Standalone conversation mode listener
├── projects/
│   └── demo/              # Example project (writer + executor)
│       ├── config.yaml    # Project config with agents + state machine
│       └── tasks.json     # Task definitions
├── tests/                 # Unit tests for all modules
├── config.yaml            # Global config (NATS, tmux, tasks)
└── prd.md                 # Product requirements document
```

## How It Works

1. Define your agents and state machine in `projects/<name>/config.yaml`
2. Run `./scripts/start.sh <name>`
3. The orchestrator reads the config, connects to NATS, and starts the state machine
4. Agents communicate via NATS JetStream (Claude Code agents use MCP bridge, script agents use nats-py directly)
5. The state machine drives transitions based on agent messages
6. Everything is visible in a tmux session

## Configuration

### Global Config (`config.yaml`)

```yaml
nats:
  url: nats://localhost:4222
  stream: AGENTS
  subjects_prefix: agents

tmux:
  nudge_prompt: "check_messages"       # text sent to agent pane on nudge
  nudge_cooldown_seconds: 30           # min seconds between nudges to same agent
  max_nudge_retries: 20                # consecutive skips before agent marked stuck
  monitor_nudge_prompt: "You have..."  # nudge text for monitor agents (e.g. manager)

tasks:
  max_attempts_per_task: 5             # retries before task marked stuck
```

### Project Config (`projects/<name>/config.yaml`)

```yaml
project: demo
tmux:
  session_name: demo

# Idle watchdog and inactivity announcer (optional)
watchdog:
  enabled: true
  check_interval: 60          # seconds between idle checks
  idle_cooldown: 300           # seconds before re-alerting same agent
  announce_on_speaker: false
  inactivity_announcer:
    enabled: true
    threshold_seconds: 300     # no-activity duration before first alert
    escalate_after: 3          # alerts before escalation
    announce_on_speaker: false

agents:
  manager:
    runtime: claude_code
    working_dir: .
    role: monitor              # places agent in the control window
    label: manager             # custom pane label
    system_prompt: "You are the manager agent."
  dev:
    runtime: claude_code
    working_dir: ./workspace
    label: dev
    system_prompt: "You are the dev agent."
  qa:
    runtime: claude_code
    ssh_host: user@192.168.1.44          # remote agent via SSH
    remote_working_dir: ~/mas-workspace
    remote_bridge_path: ~/mas-bridge/index.js
    remote_node_path: ~/local/bin/node   # custom Node.js path on remote
    label: qa
    system_prompt: "You are the QA agent."

state_machine:
  initial: idle
  states:
    idle:
      description: "No active task"
    waiting_dev:
      agent: dev
    waiting_qa:
      agent: qa
  transitions:
    - from: idle
      to: waiting_dev
      trigger: task_assigned
      action: assign_to_agent
      action_args:
        target_agent: dev
    - from: waiting_dev
      to: waiting_qa
      trigger: agent_complete
      source_agent: dev
      status: pass
      action: assign_to_agent
      action_args:
        target_agent: qa
    - from: waiting_qa
      to: idle
      trigger: agent_complete
      source_agent: qa
      status: pass
```

## Dependencies

- **Python 3.10+**: nats-py, pyyaml, requests
- **Node.js 18+**: @modelcontextprotocol/sdk, nats
- **System**: nats-server, nats CLI, tmux, ollama
- **Optional**: Claude Code (`claude`) for claude_code agent runtime

## Tests

```bash
cd /path/to/multi-agent-system-shell
python3 -m pytest tests/ -v
```

## Clipboard Image Paste

Paste a screenshot from your clipboard into any agent pane — local or remote. The image is delivered to the agent's `shared/` directory and the agent is told to read it.

**Setup:** `brew install pngpaste` (macOS only, run on the hub machine)

**Usage:**
1. Copy a screenshot to your clipboard (`Cmd+Shift+4`, etc.)
2. Switch to any agent pane in the `agents` tmux window
3. Press `prefix + V` (`Ctrl-B` then `Shift-V`)

The script auto-detects which agent you're in, SCPs the image to that agent's `shared/` directory (or copies locally for the hub), and tells the agent to look at it.

**How it works:**
- Local agents: image copied to `<working_dir>/shared/`
- Remote agents: image SCP'd to `<remote_working_dir>/shared/` on the remote host
- No setup needed on remote machines — everything runs from the hub

**Manual alternative:** Use the orchestrator console `img` command:
```bash
img /path/to/screenshot.png macmini
```

## Vision Inference (DGX)

Fara-7B runs on the DGX Spark via vLLM, with Magentic-UI for browser automation.

- **vLLM API:** `http://192.168.1.51:5000/v1/chat/completions`
- **Models:** `http://192.168.1.51:5000/v1/models`
- **Magentic-UI:** port 8080 on DGX
- **Quickstart:** [docs/QUICKSTART-DGX.md](docs/QUICKSTART-DGX.md)
- **Ops status:** `bash scripts/ops-status.sh dgx`

## License

MIT

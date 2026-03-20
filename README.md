# Multi-Agent System Shell (MAS)

This project aims to reduce communication friction between ai cli agents operating across multiple boxes/environments as i have experienced 

A config-driven multi-agent orchestrator. Define agents, their communication flow, and the state machine in YAML -- then run one command and get a consolidated terminal with all agents visible, communicating over NATS, driven by a config-driven state machine.

**One command. N agents. Full visibility.**

## Architecture

```
┌─────────────────────────────────────────────────┐
│                    tmux session                  │
│                                                  │
│  Window 1: control                               │
│  ┌──────────────────┬──────────────────┐         │
│  │   orchestrator   │   nats-monitor   │         │
│  │  (state machine) │  (live messages) │         │
│  └──────────────────┴──────────────────┘         │
│                                                  │
│  Window 2: agents (tiled grid, N panes)          │
│  ┌──────────────────┬──────────────────┐         │
│  │    agent-1       │    agent-2       │         │
│  │  (claude_code)   │  (script)        │         │
│  └──────────────────┴──────────────────┘         │
└─────────────────────────────────────────────────┘
            │                    │
            └────── NATS ────────┘
              JetStream pub/sub
```

### Key Concepts

- **N agents** defined in config (not hardcoded). Any runtime: `claude_code` or `script`
- **NATS JetStream** for messaging. Subject convention: `agents.<role>.inbox`
- **Config-driven state machine**: states + transitions in YAML, supports wildcards (`from: "*"`)
- **Built-in actions**: `assign_to_agent`, `merge_and_assign`, `merge_to_default`, `flag_human`
- **MCP bridge**: 2 generic tools (`send_message`, `check_messages`) for Claude Code agents
- **tmux layout**: dynamic pane arrangement based on agent count

## Project Structure

```
multi-agent-system-shell/
├── orchestrator/          # Core orchestrator modules
│   ├── config.py          # YAML config loader
│   ├── state_machine.py   # Config-driven state engine
│   ├── task_queue.py      # Task queue manager
│   ├── nats_client.py     # NATS JetStream wrapper
│   ├── router.py          # Message router
│   ├── tmux_comm.py       # tmux communication (nudge, clear, send)
│   ├── lifecycle.py       # Task lifecycle manager
│   ├── console.py         # Interactive console + LLM client
│   ├── llm_client.py      # Ollama LLM client
│   ├── logging_setup.py   # Logging configuration
│   └── session_report.py  # Session report generator
├── agents/
│   └── echo_agent.py      # Example script agent (speaks NATS directly)
├── mcp-bridge/
│   ├── index.js           # MCP server (send_message, check_messages)
│   └── package.json
├── scripts/
│   ├── start.sh           # Launch tmux session with all agents
│   ├── stop.sh            # Graceful shutdown
│   ├── setup-nats.sh      # Install and start NATS server
│   ├── reset-tasks.sh     # Reset task statuses to pending
│   └── nats-monitor.sh    # Live NATS message monitor
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
  nudge_prompt: "You have new messages. Use check_messages with your role."
  nudge_cooldown_seconds: 30

tasks:
  max_attempts_per_task: 5
```

### Project Config (`projects/<name>/config.yaml`)

```yaml
project: demo
tmux:
  session_name: demo

agents:
  writer:
    runtime: claude_code
    working_dir: ./workspace
    system_prompt: "You are a writer agent."
  executor:
    runtime: script
    command: "python3 agents/echo_agent.py --role executor"

state_machine:
  initial: idle
  states:
    idle:
      description: "No active task"
    waiting_writer:
      agent: writer
    waiting_executor:
      agent: executor
  transitions:
    - from: idle
      to: waiting_writer
      trigger: task_assigned
      action: assign_to_agent
      action_args:
        target_agent: writer
    - from: waiting_writer
      to: waiting_executor
      trigger: agent_complete
      source_agent: writer
      status: pass
      action: assign_to_agent
      action_args:
        target_agent: executor
    - from: waiting_executor
      to: idle
      trigger: agent_complete
      source_agent: executor
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

## License

MIT

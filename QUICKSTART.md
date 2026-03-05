# Quickstart Guide

Get the Multi-Agent System Shell running with the demo project in 5 minutes.

## 1. Prerequisites

```bash
brew install tmux nats-server python3 node ollama
npm install -g @anthropic-ai/claude-code   # for claude_code agents
ollama serve                                # leave running in background
```

## 2. Install Dependencies

```bash
cd /path/to/multi-agent-system-shell

# Python dependencies
pip3 install nats-py pyyaml requests

# MCP bridge dependencies
cd mcp-bridge && npm install && cd ..
```

## 3. Start NATS

```bash
cd /path/to/multi-agent-system-shell
./scripts/setup-nats.sh
```

This installs (if needed) and starts `nats-server` with JetStream enabled on port 4222.

## 4. Run the Demo

```bash
cd /path/to/multi-agent-system-shell
./scripts/start.sh demo
```

This creates a tmux session with:
- **Window 1 (control)**: orchestrator + NATS monitor side-by-side
- **Window 2 (agents)**: one pane per agent (writer + executor for the demo)

### tmux Navigation

| Key | Action |
|-----|--------|
| `Ctrl-b 1` | Switch to control window |
| `Ctrl-b 2` | Switch to agents window |
| `Ctrl-b o` | Cycle to next pane |
| `Ctrl-b q` | Show pane numbers |
| `Ctrl-b d` | Detach (session keeps running) |

## 5. Interactive Commands

Type these in the orchestrator pane:

| Command | Description |
|---------|-------------|
| `status` | Current task and progress |
| `tasks` | List all tasks with status |
| `nudge <agent>` | Manually nudge an agent |
| `msg <agent> TEXT` | Send text to an agent's pane |
| `skip` | Skip current stuck task |
| `pause` / `resume` | Pause/resume polling |
| `log` | Show last 10 log entries |
| `help` | Show all commands |

## 6. Stop

```bash
cd /path/to/multi-agent-system-shell
./scripts/stop.sh demo
```

## Creating Your Own Project

1. Create a new directory under `projects/`:

```bash
mkdir -p projects/my-project
```

2. Create `projects/my-project/config.yaml` with your agents and state machine (see `projects/demo/config.yaml` for reference).

3. Create `projects/my-project/tasks.json` with your task definitions:

```json
{
  "project": "my-project",
  "tasks": [
    {
      "id": "task-1",
      "title": "First task",
      "description": "What the agents should do",
      "acceptance_criteria": ["Criterion 1", "Criterion 2"],
      "status": "pending",
      "attempts": 0,
      "max_attempts": 5
    }
  ]
}
```

4. Launch: `./scripts/start.sh my-project`

## Utility Scripts

| Script | Purpose |
|--------|---------|
| `scripts/setup-nats.sh` | Install and start NATS server with JetStream |
| `scripts/start.sh <project>` | Launch tmux session with all agents |
| `scripts/stop.sh <project>` | Graceful shutdown |
| `scripts/reset-tasks.sh <project>` | Reset all task statuses to pending |
| `scripts/nats-monitor.sh` | Live stream of all NATS messages |

## Troubleshooting

**NATS connection refused**: Run `./scripts/setup-nats.sh` or check `pgrep nats-server`.

**Ollama not responding**: Run `ollama serve` in another terminal.

**Agent not picking up messages**: Try `nudge <agent>` in the orchestrator pane.

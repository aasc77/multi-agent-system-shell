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

On macOS, this opens **two iTerm windows**:
- **iTerm window 1 (control)**: orchestrator + NATS monitor + manager agent
- **iTerm window 2 (agents)**: one pane per agent (dev, QA, etc.) in a tiled grid

Each window is a grouped tmux session, so they can independently show different
tmux windows while sharing the same session.

### tmux Navigation

| Key | Action |
|-----|--------|
| `Ctrl-b o` | Cycle to next pane |
| `Ctrl-b q` | Show pane numbers |
| `Ctrl-b d` | Detach (session keeps running) |
| `Ctrl-b V` | Paste clipboard image to current agent pane |

## 5. Interactive Commands

Type these in the orchestrator pane:

| Command | Description |
|---------|-------------|
| `status` | Current task and progress |
| `tasks` | List all tasks with status |
| `nudge <agent>` | Manually nudge an agent |
| `msg <agent> TEXT` | Send text to an agent's pane |
| `broadcast TEXT` | Send text to ALL agent panes |
| `img <file> [agent]` | Share file to workspaces and notify agent |
| `conversation on\|off` | Toggle conversation mode (hear agents on speakers) |
| `skip` | Skip current stuck task |
| `pause` / `resume` | Pause/resume outbox processing |
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

## Sharing Images/Files with Agents

From the orchestrator pane, use the `img` command to distribute a file to all agent workspaces and notify a specific agent:

```
img ~/Screenshots/bug.png hub
```

Without an agent name, it targets the currently active agent:

```
img ~/diagram.png
```

From the shell (outside the orchestrator):

```bash
cd ~/Repositories/multi-agent-system-shell
./scripts/share-file.sh remote-test ~/Screenshots/bug.png
```

Files land in `shared/<filename>` in each agent's workspace. Agents can view them with their Read tool.

## Utility Scripts

| Script | Purpose |
|--------|---------|
| `scripts/setup-nats.sh` | Install and start NATS server with JetStream |
| `scripts/start.sh <project>` | Launch two iTerm windows with all agents |
| `scripts/stop.sh <project>` | Graceful shutdown |
| `scripts/reset-tasks.sh <project>` | Reset all task statuses to pending |
| `scripts/share-file.sh <project> <file>` | Distribute file to all agent workspaces |
| `scripts/nats-monitor.sh` | Live stream of all NATS messages |
| `scripts/tmux-paste-image.sh` | Paste clipboard image into any agent pane |
| `scripts/ssh-reconnect.sh` | Auto-reconnect wrapper for remote SSH agents |
| `scripts/notify.sh "message"` | macOS text-to-speech announcement |
| `scripts/push-notify.py "message"` | Pushover push notification |
| `scripts/sms-notify.py "message"` | Twilio SMS notification |
| `scripts/conversation-mode.py` | Standalone conversation mode listener |
| `scripts/reset-demo.sh [project]` | Full reset: kill tmux + NATS stream + tasks + logs |

## Clean Start (Recommended)

If things are in a weird state, do a full reset before starting:

```bash
cd ~/Repositories/multi-agent-system-shell
bash scripts/reset-demo.sh demo
bash scripts/start.sh demo
```

On macOS, `start.sh` automatically opens two iTerm windows (control + agents).
On other systems, attach manually: `tmux attach -t demo`.

## Troubleshooting

**NATS connection refused**: Run `./scripts/setup-nats.sh` or check `pgrep nats-server`.

**Ollama not responding**: Run `ollama serve` in another terminal.

**Agent not picking up messages**: Try `nudge <agent>` in the orchestrator pane.

**Orchestrator exits immediately**: Stale NATS consumers. Run `bash scripts/reset-demo.sh demo` for a clean slate.

**CLAUDECODE env var leak**: If agents behave oddly, tmux may have inherited CLAUDECODE from a parent Claude Code session. `reset-demo.sh` clears this, or manually: `tmux set-environment -g -u CLAUDECODE`

**Stale NATS consumers**: If you see "consumer is already bound" errors, run `bash scripts/reset-demo.sh demo` which deletes the AGENTS stream entirely.

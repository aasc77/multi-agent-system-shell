#!/usr/bin/env python3
"""
Seed the operational_knowledge collection with key system documentation.

Indexes architecture, network layout, agent roles, communication flow,
startup flow, and restart procedures so agents can recall how the system
works via search_knowledge.

Usage:
    cd /path/to/multi-agent-system-shell
    python3 scripts/index-ops-knowledge.py

Environment:
    CHROMADB_PATH  -- ChromaDB storage path (default: data/chromadb)
    OLLAMA_URL     -- Ollama endpoint (default: http://localhost:11434)
"""

import asyncio
import os
import sys

# Add knowledge-store to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "knowledge-store"))
import store

# ---------------------------------------------------------------------------
# Operational knowledge documents
# ---------------------------------------------------------------------------

DOCUMENTS = [
    {
        "title": "Tmux Pane Layout",
        "category": "architecture",
        "content": """\
The MAS (Multi-Agent System) uses two tmux windows in a session called "mas":

**Control window** (window: control):
- Pane 0: Orchestrator process (python3 -m orchestrator <project>)
- Pane 1: Manager agent (autonomous monitor, Claude Code with MCP bridge)
- Pane 2: NATS monitor (nats sub 'agents.>')

**Agents window** (window: agents):
- Pane 0: hub (dev agent, local Mac)
- Pane 1: dgx (compute agent, remote DGX 192.168.1.51)
- Pane 2: macmini (QA agent, remote Mac Mini 192.168.1.44)
- Pane 3: hassio (home automation agent, remote Home Assistant)
- Pane 4: dgx2 (second compute agent, remote DGX)

Pane labels are set via tmux @label option. Layout is "tiled" for even distribution.
The tmux session name is project-dependent (configured in project config.yaml, e.g. "remote-test").
To select a pane: tmux select-pane -t <session>:agents.<pane_index>
To send text to a pane: tmux send-keys -t <session>:agents.<pane_index> "text" Enter
""",
    },
    {
        "title": "Network Layout",
        "category": "config",
        "content": """\
Machine IP addresses and roles:

- Mac Studio (orchestrator/hub): localhost / 192.168.1.37
  - Runs: NATS server, orchestrator, hub agent (dev), knowledge store, Ollama
  - NATS URL for local agents: nats://127.0.0.1:4222
  - NATS URL for remote agents: nats://<hostname>.local:4222 (mDNS, auto-resolved)

- DGX (compute): 192.168.1.51
  - SSH: dgx@192.168.1.51
  - Runs: vLLM (port 5000), Whisper (port 5112), Piper TTS (port 5111), Magentic-UI (port 8888)
  - GPU inference only -- minimal packages

- Mac Mini (QA): 192.168.1.44
  - SSH: angelserrano@192.168.1.44
  - Runs: macmini agent (QA), OpenClaw (port 18790)

- Home Assistant (hassio): homeassistant.local
  - Runs: home automation agent, speaker services
  - HA API at localhost:8123 (long-lived token in Keychain)

NATS is the central message bus. All agents communicate via NATS JetStream.
The NATS stream is called "AGENTS" with subject prefix "agents.>".
""",
    },
    {
        "title": "Agent Roles",
        "category": "architecture",
        "content": """\
Agent roles in the Multi-Agent System:

- **hub** (dev): Primary developer agent on Mac Studio. Writes code, builds features,
  deploys services, fixes bugs. Working directory: workspace/ under the project.
  Always refer to hub as "dev" in conversation.

- **macmini** (QA): Quality assurance agent on Mac Mini. Verifies work done by hub,
  runs tests, validates deployments. After hub finishes work, notify macmini to verify.

- **dgx** (compute): GPU compute agent on DGX machine. Handles ML inference tasks,
  model serving (vLLM), speech processing (Whisper/Piper). All ML inference runs here.

- **dgx2** (compute): Second compute agent on DGX for parallel GPU tasks.

- **hassio** (home automation): Home Assistant agent. Controls smart home devices,
  speaker announcements via Piper TTS.

- **manager** (monitor): Autonomous manager in the control window. Monitors system
  health, coordinates tasks between agents, routes human-facing requests. Manager does
  NOT write code -- delegates to hub (dev). Route all human-facing requests through manager.
""",
    },
    {
        "title": "Communication Flow",
        "category": "architecture",
        "content": """\
How agents communicate in the MAS:

1. **MCP Bridge -> NATS**: Each agent has an MCP bridge (mcp-bridge/index.js) that
   connects to NATS JetStream. The bridge exposes three MCP tools:
   - check_messages: Pull messages from agents.<role>.inbox
   - send_message: Send results back to the orchestrator
   - send_to_agent: Send a direct message to another agent's inbox

2. **NATS Subjects**: Messages flow through subjects like:
   - agents.<agent>.inbox -- incoming tasks/messages for an agent
   - agents.<agent>.outbox -- results sent back by an agent

3. **Nudging**: After publishing a message to an agent's inbox, the orchestrator
   (or another agent) sends a tmux nudge: types "check_messages" + Enter into the
   agent's tmux pane. This prompts the Claude Code instance to call the MCP tool.

4. **Important rules**:
   - Always send via MCP bridge first, THEN nudge in tmux -- never type messages directly
   - After completing work, always call send_message with status "pass" or "fail"
   - To message another agent: use send_to_agent with target_agent and message
   - Never send /commands via tmux send-keys (triggers search mode)

5. **Knowledge Store**: ChromaDB + Ollama embeddings. The indexer daemon subscribes
   to agents.> and indexes all messages. Agents can search via search_knowledge MCP tool.
   Operational docs are in the operational_knowledge collection.
""",
    },
    {
        "title": "Startup Flow",
        "category": "runbook",
        "content": """\
How the MAS starts up (scripts/start.sh <project>):

1. **Preflight**: Checks required tools (tmux, python3, nats-server)
2. **Config parsing**: Merges global config.yaml + projects/<project>/config.yaml
3. **MCP config generation**: Creates per-agent .json MCP configs in
   projects/<project>/.mcp-configs/. Each config wires the MCP bridge with:
   - AGENT_ROLE, NATS_URL, WORKSPACE_DIR
   - For remote agents: uses hostname.local (mDNS) for NATS URL
4. **SCP to remotes**: Copies MCP configs to remote agents via SSH
   (stored at ~/.mas-mcp-configs/<agent>.json on remote machines)
5. **NATS auto-start**: Runs setup-nats.sh if nats-server isn't running
6. **tmux session**: Creates session with control + agents windows
7. **Control window**: Launches orchestrator, manager agent, NATS monitor
8. **Knowledge indexer**: Starts indexer.py as background daemon
9. **Agent panes**: Creates one pane per agent, launches Claude Code with
   --dangerously-skip-permissions --mcp-config <path> --allowedTools <tools>
10. **Remote agents**: Wrapped in ssh-reconnect.sh for auto-reconnection
11. **Terminal windows**: Opens two iTerm2 windows (control + agents)
""",
    },
    {
        "title": "Agent Restart Procedures",
        "category": "runbook",
        "content": """\
How to restart agents in the MAS:

**Local agents (hub)**:
- Use the respawn-pane script or manually:
  1. tmux send-keys -t mas:agents.<pane> C-c  (kill current process)
  2. tmux send-keys -t mas:agents.<pane> "cd <working_dir> && claude ..." Enter

**Remote agents (dgx, macmini, hassio, dgx2)**:
- Use ssh-reconnect.sh which auto-reconnects:
  1. tmux send-keys -t mas:agents.<pane> C-c  (kill SSH session)
  2. ssh-reconnect.sh will automatically retry the connection
  3. Or manually: tmux send-keys -t mas:agents.<pane> "bash scripts/ssh-reconnect.sh <agent>" Enter

**Manager agent (control window)**:
- tmux send-keys -t mas:control.1 C-c
- Re-launch with the same claude command from start.sh

**Full system restart**:
- Run: ./scripts/start.sh <project>
- This kills the existing tmux session and recreates everything from scratch

**Troubleshooting**:
- If an agent is unresponsive, check its tmux pane for error output
- If NATS is down: check with "pgrep nats-server" and restart via setup-nats.sh
- If Ollama is down: knowledge store searches will fail but messaging still works
""",
    },
]


async def main():
    # Pre-flight check
    ok, msg = await store.check_ollama_health()
    if not ok:
        print(f"ERROR: {msg}", file=sys.stderr)
        sys.exit(1)

    print(f"Seeding operational knowledge ({len(DOCUMENTS)} documents)...")

    for doc in DOCUMENTS:
        try:
            doc_id = await store.index_document(
                text=doc["content"],
                title=doc["title"],
                category=doc["category"],
            )
            print(f"  OK  {doc['title']} -> {doc_id}")
        except Exception as e:
            print(f"  FAIL  {doc['title']}: {e}", file=sys.stderr)

    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())

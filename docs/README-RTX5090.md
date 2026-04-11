# RTX5090 Agent

The `RTX5090` agent runs on a Windows 11 workstation at **192.168.1.41** (hostname `dev`) equipped with an NVIDIA RTX 5090. It participates in the multi-agent system (MAS) as a `claude_code` runtime — same contract as `hassio`, `dgx`, and `macmini` — and is intended as the **fast local-inference hub** for the fleet (LLM/TTS/STT workloads that benefit from the 5090's speed).

> **Architecture pivot in progress (2026-04-11).** This document describes the **target** architecture — native Windows Claude Code with WSL2 as a subordinate runtime. The production box was previously bootstrapped in WSL2-hosted mode (`4c2acf1`); cutover to native Windows is tracked in issue **#29** and ships via branch `feat/29-rtx5090-native-windows`. Until the user has completed the interactive install ceremony in [QUICKSTART-RTX5090.md](QUICKSTART-RTX5090.md), the production box is still running the WSL2 routing described in git history.

## Architecture (target, post-#29)

```
┌────────────────── Windows 11 host (192.168.1.41) ──────────────────┐
│                                                                     │
│   Windows OpenSSH sshd  :22                                         │
│          │                                                          │
│          ▼                                                          │
│   C:\ProgramData\ssh\administrators_authorized_keys                 │
│     ┌───────────────────────────────────────────────────────────┐  │
│     │ command="...native-shell.ps1"  ssh-ed25519  <Mac key>      │  │
│     │ ssh-rsa  <Mac id_rsa>    # escape-hatch fallback           │  │
│     └───────────────────────────────────────────────────────────┘  │
│          │ (ed25519 key match)                                      │
│          ▼                                                          │
│   C:\ProgramData\ssh\native-shell.ps1                               │
│     - reads $env:SSH_ORIGINAL_COMMAND                               │
│     - refuses sftp-server subsystem (forces scp -O legacy)          │
│     - writes the command to a temp .sh file                         │
│     - execs: "C:\Program Files\Git\bin\bash.exe" -l <tmp>           │
│     - returns exit code, deletes tmp                                │
│          │                                                          │
│          ▼                                                          │
│   Git for Windows (Git Bash)                                        │
│     - POSIX-style paths with ~ resolving to C:\Users\angel          │
│     - PATH inherited from Windows (so claude.exe, wsl.exe, etc.)    │
│          │                                                          │
│          ▼                                                          │
│   claude.exe (native Windows binary)                                │
│     - Installed via: irm https://claude.ai/install.ps1 | iex        │
│     - OAuth credentials: C:\Users\angel\.claude\.credentials.json   │
│     - MCP config: C:\Users\angel\.mas-mcp-configs\RTX5090.json      │
│     - CWD: C:\Users\angel\mas-workspace (== ~/mas-workspace in Bash)│
│     - Can shell out to:                                             │
│         • cmd.exe / powershell.exe — native Windows automation      │
│         • wsl.exe -d Ubuntu -- <cmd> — WSL2 subordinate runtime     │
│         • Git Bash builtins — bash, sed, awk, coreutils             │
│                                                                     │
│   ┌── Subordinate: WSL2 Ubuntu 24.04 (reachable, not hosting) ──┐  │
│   │  - CUDA / NVIDIA drivers for GPU workloads                   │  │
│   │  - Reached via wsl.exe from Windows claude, not from sshd    │  │
│   │  - No bridge code runs inside WSL anymore                    │  │
│   └──────────────────────────────────────────────────────────────┘  │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

**Why this design:**

- **Native Windows claude reaches both surfaces.** WSL2-hosted claude could see Windows only through `/mnt/c/` and `wsl.exe -- powershell.exe` round-trips; that was lossy and awkward for anything touching Windows processes, drivers, or the registry. Native Windows claude runs Windows commands directly AND retains full WSL2 access via `wsl.exe -d Ubuntu -- <cmd>`.
- **Git Bash as the SSH landing zone.** Git for Windows is a hard prereq for Claude Code install on Windows anyway (per [Anthropic's docs](https://code.claude.com/docs/en/setup.md)). Routing the ForceCommand wrapper into `"C:\Program Files\Git\bin\bash.exe"` lets the orchestrator keep its existing bash-syntax launch command strings unchanged (`cd ~/mas-workspace && unset CLAUDECODE; claude ...` still parses, because Git Bash exposes the Windows user profile as `~` and honors `&&` / `||` / environment semantics the same way Linux bash does).
- **Orchestrator launch command is unchanged.** `scripts/start.sh` does not branch on `remote_shell` or learn Windows syntax. The only difference on the wire is the ForceCommand wrapper target — `native-shell.ps1` instead of `wsl-shell.ps1`. That means the rearch is swappable at the `administrators_authorized_keys` layer without a bounce of the orchestrator config.
- **SFTP refusal clause stays.** Same rationale as the old wrapper: Windows OpenSSH's built-in SFTP subsystem would otherwise hang `scp` in its default mode. `native-shell.ps1` refuses `sftp-server(\.exe)?|internal-sftp` early so the client's `scp -O` legacy-mode fallback runs as a regular shell command, which Git Bash then handles via its own `scp.exe`.
- **id_rsa fallback key stays.** If `native-shell.ps1` breaks, `ssh -i ~/.ssh/id_rsa -o IdentitiesOnly=yes angel@192.168.1.41` still drops into a raw Windows cmd.exe session for repair without needing console access.
- **Admin authorized_keys, not user file.** `angel` is still a local Administrator on the box, so Windows OpenSSH *only* reads `C:\ProgramData\ssh\administrators_authorized_keys`. `$env:USERPROFILE\.ssh\authorized_keys` is ignored.

## Config

Agent entry in `projects/remote-test/config.yaml` (unchanged from the WSL2-era shape — `~/mas-workspace` now resolves to `C:\Users\angel\mas-workspace` through Git Bash):

```yaml
RTX5090:
  runtime: claude_code
  ssh_host: angel@192.168.1.41
  remote_working_dir: ~/mas-workspace
  remote_bridge_path: ~/mas-bridge/index.js
  label: RTX5090
  system_prompt: "You are the fast-inference agent on the RTX5090 box ..."
```

## Tmux Pane

- Session: `remote-test-agents`
- Window: `1` (`agents`)
- Pane: `4`
- Label: `RTX5090`

(Pane map, for reference: `hub=0, macmini=1, dgx=2, dgx2=3, RTX5090=4, hassio=5`.)

## Owned Responsibilities

RTX5090 owns its own box (per the fleet-wide rule that each agent manages its own machine). Expected workloads are in the sub-issues of epic **#14** (5090 voice + inference hub):

- **#16** — CUDA/NVIDIA driver plumbing (now reached through `wsl.exe` rather than hosted inside WSL)
- **#17** — vLLM / llama.cpp local inference endpoint
- **#18** — Whisper / Piper fast paths (offloaded from DGX when 5090 is idle)
- **#19** — model cache layout & disk hygiene
- **#20** — GPU watchdog + temperature guards

Issue **#29** is a strict prerequisite for unblocking #16 docs work — the architecture pivot must complete before the provider-layer swap-path doc can cite a working RTX5090 agent.

## Files Touched on the 5090 Host

| Path | Purpose |
|---|---|
| `C:\ProgramData\ssh\administrators_authorized_keys` | SSH key auth for the `angel` admin user — carries the ForceCommand prefix pointing at `native-shell.ps1` |
| `C:\ProgramData\ssh\administrators_authorized_keys.bak-<ts>` | Rolling backups taken before each wrapper edit |
| `C:\ProgramData\ssh\native-shell.ps1` | New PowerShell ForceCommand wrapper that routes SSH commands through Git Bash |
| `C:\ProgramData\ssh\wsl-shell.ps1` | *Deprecated* — pre-#29 WSL2 wrapper, deleted in Step 4 cleanup after smoke tests pass |
| `C:\Users\angel\.claude\.credentials.json` | Anthropic OAuth token, written by `claude login` during Step 1 of the install ceremony |
| `C:\Users\angel\.mas-mcp-configs\RTX5090.json` | MCP config pushed by `scripts/start.sh` on project start (scp target) |
| `C:\Users\angel\mas-workspace\CLAUDE.md` | Agent system prompt and instructions |
| `C:\Users\angel\mas-workspace\shared\` | Orchestrator-managed file drop zone |

## See Also

- [QUICKSTART-RTX5090.md](QUICKSTART-RTX5090.md) — the exact commands to bring the agent up on native Windows Claude from scratch (post-#29)
- [QUICKSTART-DGX.md](QUICKSTART-DGX.md) — companion doc for the DGX inference agent
- `scripts/start.sh` — the orchestrator launch logic (search for `RUNTIME_CLAUDE_CODE`)
- [PIR-2026-04-11-rtx5090-rearch.md](PIR-2026-04-11-rtx5090-rearch.md) *(pending, filed with the cutover PR)* — post-incident review of the WSL2 OAuth expiry that triggered this rearch

# RTX5090 Agent

The `RTX5090` agent runs on a Windows 11 workstation at **192.168.1.41** (hostname `dev`) equipped with an NVIDIA RTX 5090. It participates in the multi-agent system (MAS) as a `claude_code` runtime — same contract as `hassio`, `dgx`, and `macmini` — and is intended as the **fast local-inference hub** for the fleet (LLM/TTS/STT workloads that benefit from the 5090's speed).

## Architecture

```
┌────────────────── Windows 11 host (192.168.1.41) ──────────────────┐
│                                                                     │
│   Windows OpenSSH sshd  :22                                         │
│          │                                                          │
│          ▼                                                          │
│   administrators_authorized_keys                                    │
│     ┌──────────────────────────────────────────────────────────┐   │
│     │ command="...wsl-shell.ps1"  ssh-ed25519  <Mac key>        │   │
│     │ ssh-rsa  <Mac id_rsa>    # escape-hatch fallback          │   │
│     └──────────────────────────────────────────────────────────┘   │
│          │ (ed25519 key match)                                      │
│          ▼                                                          │
│   C:\ProgramData\ssh\wsl-shell.ps1                                  │
│     - reads $env:SSH_ORIGINAL_COMMAND                               │
│     - writes it to a temp .sh file                                  │
│     - execs: wsl.exe -d Ubuntu -- bash -l <tmp>                     │
│     - returns exit code, deletes tmp                                │
│          │                                                          │
│          ▼                                                          │
│   WSL2 Ubuntu 24.04  (distro "Ubuntu", default user "angel")        │
│     ~/mas-bridge/index.js    # mas-bridge (node + NATS MCP)         │
│     ~/mas-workspace          # agent cwd, CLAUDE.md, shared/        │
│     ~/.mas-mcp-configs/*.json # orchestrator-provisioned MCP cfg    │
│     /usr/local/bin/claude    # symlink → ~/.npm-global/bin/claude   │
│     /usr/bin/node (v20)                                             │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

**Why this design:**

- **No DefaultShell registry flip.** Windows OpenSSH's default login shell stays as cmd.exe. All routing to Linux happens in an `authorized_keys` `command="…"` prefix — reversible by editing one file and nothing else. Explicitly rejected: Git Bash, native PowerShell agents, or `sshd_config` DefaultShell registry changes.
- **ForceCommand via PowerShell wrapper**, not inline. Writing `SSH_ORIGINAL_COMMAND` to a temp file and running `bash -l <file>` means the wrapper never has to escape bash quoting — any command the orchestrator sends goes through byte-for-byte.
- **id_rsa fallback key.** `administrators_authorized_keys` also carries the Mac's RSA public key **without** a `command=` prefix. If the wrapper ever breaks, `ssh -i ~/.ssh/id_rsa -o IdentitiesOnly=yes angel@192.168.1.41` drops into a raw Windows cmd.exe session so you can repair the primary entry without needing console access.
- **Admin authorized_keys, not user file.** The `angel` account is a local Administrator, so Windows OpenSSH *only* reads `C:\ProgramData\ssh\administrators_authorized_keys`. Never edit `$env:USERPROFILE\.ssh\authorized_keys` and expect it to take effect.

## Config

Agent entry in `projects/remote-test/config.yaml`:

```yaml
RTX5090:
  runtime: claude_code
  ssh_host: angel@192.168.1.41
  remote_working_dir: ~/mas-workspace
  remote_bridge_path: ~/mas-bridge/index.js
  label: RTX5090
  system_prompt: "You are the fast-inference agent on the RTX5090 box ..."
```

Before this issue (#15), the entry was:

```yaml
RTX5090:
  runtime: script
  ssh_host: angel@192.168.1.41
  command: powershell
  label: RTX5090
```

— which only opened a bare PowerShell shell (no MCP bridge, no agent loop, no inbox).

## Tmux Pane

- Session: `remote-test-agents`
- Window: `1` (`agents`)
- Pane: `4`
- Label: `RTX5090`

(Pane map, for reference: `hub=0, macmini=1, dgx=2, dgx2=3, RTX5090=4, hassio=5`.)

## Owned Responsibilities

RTX5090 owns its own box (per the fleet-wide rule that each agent manages its own machine). Expected workloads are in the sub-issues of epic **#14** (5090 voice + inference hub):

- **#16** — CUDA/NVIDIA driver plumbing into WSL
- **#17** — vLLM / llama.cpp local inference endpoint
- **#18** — Whisper / Piper fast paths (offloaded from DGX when 5090 is idle)
- **#19** — model cache layout & disk hygiene
- **#20** — GPU watchdog + temperature guards

Issue **#15** (this doc's scope) is the prerequisite: get the agent **onto the bus** as a first-class `claude_code` runtime. Nothing in #16–#20 is unblocked until `ssh angel@192.168.1.41` lands in WSL bash and the orchestrator can scp an MCP config across.

## Files Touched on the 5090 Host

| Path | Purpose |
|---|---|
| `C:\ProgramData\ssh\administrators_authorized_keys` | SSH key auth for the `angel` admin user — carries the ForceCommand prefix |
| `C:\ProgramData\ssh\administrators_authorized_keys.bak-<ts>` | Rolling backups taken before each wrapper edit |
| `C:\ProgramData\ssh\wsl-shell.ps1` | The PowerShell ForceCommand wrapper |
| `\\wsl.localhost\Ubuntu\home\angel\mas-bridge\` | NATS MCP bridge (node, copied from the repo's `mcp-bridge/`) |
| `\\wsl.localhost\Ubuntu\home\angel\mas-workspace\` | Agent cwd, holds `CLAUDE.md` and the orchestrator-managed `shared/` dir |
| `\\wsl.localhost\Ubuntu\home\angel\.mas-mcp-configs\RTX5090.json` | MCP config pushed by `scripts/start.sh` on project start |

## See Also

- [QUICKSTART-RTX5090.md](QUICKSTART-RTX5090.md) — the exact commands to bring this up from scratch
- [QUICKSTART-DGX.md](QUICKSTART-DGX.md) — companion doc for the DGX inference agent
- `scripts/start.sh` — the orchestrator launch logic (search for `RUNTIME_CLAUDE_CODE`)

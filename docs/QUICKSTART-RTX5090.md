# RTX5090 Quickstart: Native Windows Claude + Git Bash SSH Wrapper

Step-by-step to bring the `RTX5090` agent online on **native Windows Claude Code** (or rebuild it if the Windows box is wiped). See [README-RTX5090.md](README-RTX5090.md) for the architectural *why*.

> **Rearch note (2026-04-11, issue #29).** This quickstart describes the **target** architecture. If you are landing on a box that was previously bootstrapped to WSL2-hosted Claude (per the pre-#29 commit `4c2acf1`), either follow the cutover steps in [§ Cutover from WSL2-hosted Claude](#cutover-from-wsl2-hosted-claude) at the bottom, or rebuild from scratch by running every step below in order on a fresh Windows install.

## Prerequisites

- Windows 11 workstation at `192.168.1.41` with an **Administrator** account named `angel`.
- Windows OpenSSH Server (`sshd`) already running and accepting the Mac's `id_ed25519` key in `C:\ProgramData\ssh\administrators_authorized_keys`. (If SSH key auth is not yet set up at all, sit at the console once and paste the Mac's `~/.ssh/id_ed25519.pub` into that file — single line, CRLF line endings.)
- From the Mac orchestrator host, `ssh angel@192.168.1.41 'whoami'` must return `angel` before you start.
- **Git for Windows** is a hard prereq for Claude Code on Windows (per Anthropic's docs). The installer from <https://git-scm.com/downloads/win> puts `bash.exe` at `C:\Program Files\Git\bin\bash.exe` — that exact path is what `native-shell.ps1` invokes, so accept the default install location.
- **WSL2 Ubuntu 24.04** remains installed as a *subordinate* runtime (reached from Windows claude via `wsl.exe -d Ubuntu -- <cmd>` for CUDA / NVIDIA / inference work). If you need to install it fresh, run `wsl --install -d Ubuntu --no-launch` once from PowerShell and let Windows finish registering it — everything else about WSL2 is pass-through from claude and does not need bootstrapping for agent participation.

## Step 1 — User runs the interactive Claude Code install (RTX5090 console or RDP)

This step **cannot be done headlessly from the orchestrator** — `claude login` needs a browser/code OAuth flow. The user runs this at the RTX5090 console (or an RDP session) exactly once per box.

```powershell
# At a PowerShell prompt on the RTX5090 box, as the `angel` admin user:

# 1. Verify Git for Windows is installed (hard prereq).
Get-Command bash.exe -ErrorAction Stop
# Expect:  CommandType  Name     Source
#          Application  bash.exe C:\Program Files\Git\bin\bash.exe

# 2. Install Claude Code (Anthropic-recommended method).
irm https://claude.ai/install.ps1 | iex

# 3. Verify the install.
claude --version
# Expect: Claude Code v2.x.x

# 4. Log in to Anthropic. This opens a browser for OAuth.
claude login
# Complete the OAuth flow; the CLI writes C:\Users\angel\.claude\.credentials.json

# 5. Quick sanity check — run one trivial command that exercises the credential.
claude -p "say hi in one word"
# Expect: some short response, no auth error
```

Once step 1 is done, the `angel` user has a long-lived OAuth credential at `C:\Users\angel\.claude\.credentials.json` and the `claude.exe` binary is on the Windows PATH. Everything in step 2+ is scripted from the Mac orchestrator host and does not require further console access to the 5090.

## Step 2 — Install the `native-shell.ps1` ForceCommand wrapper

The new wrapper replaces `wsl-shell.ps1` — it routes incoming SSH commands through Git Bash (native Windows) instead of through WSL2 Ubuntu. All of the following runs **from the Mac orchestrator host** over SSH.

### 2a. Take a backup of the current authorized_keys

**⚠️ If you corrupt `administrators_authorized_keys`, you are locked out and need either password auth or physical console access.**

```bash
ssh angel@192.168.1.41 'powershell.exe -NoProfile -Command "Copy-Item C:\ProgramData\ssh\administrators_authorized_keys C:\ProgramData\ssh\administrators_authorized_keys.bak-$(Get-Date -Format yyyyMMddHHmmss) -Force"'
```

### 2b. Upload `native-shell.ps1`

Same heredoc convention as the old wrapper — fully-quoted `<<'PSEOF'` delimiter so bash does not interpret PowerShell backticks. **Do not swap to an unquoted delimiter**; PowerShell uses backtick as its escape character and an unquoted bash heredoc silently runs `r` and `n` as commands when it sees `` `r`n ``. See the "Known pitfalls" section at the bottom.

```bash
cat <<'PSEOF' | ssh angel@192.168.1.41 'powershell.exe -NoProfile -Command "$input | Out-File -FilePath C:\ProgramData\ssh\native-shell.ps1 -Encoding ascii -Force"'
# native-shell.ps1 - SSH ForceCommand wrapper that routes incoming commands
# through Git for Windows' bash.exe (native Windows, not WSL2).
#
# Preserves TTY by writing SSH_ORIGINAL_COMMAND to a temp .sh file and
# exec'ing bash -l with that file as a script. stdin/stdout/stderr inherit
# the PTY that sshd set up, so interactive TUIs like claude render correctly.
#
# SFTP subsystem requests (scp in default SFTP mode) arrive with
# SSH_ORIGINAL_COMMAND set to "sftp-server.exe" -- Windows OpenSSH's
# Subsystem handler. We refuse those fast so the client's legacy-mode
# fallback (`scp -O`) kicks in and runs as a regular `scp -t` command,
# which the wrapper then routes through Git Bash where Git for Windows
# ships its own scp.exe binary. Without this early reject, the wrapper
# would try to execute sftp-server.exe through Git Bash interop and hang
# on the garbled pipe protocol.
$ErrorActionPreference = 'Stop'
$bash = 'C:\Program Files\Git\bin\bash.exe'
$cmd = $env:SSH_ORIGINAL_COMMAND

if (-not (Test-Path -LiteralPath $bash)) {
    [Console]::Error.WriteLine("native-shell.ps1: Git for Windows bash.exe not found at $bash")
    exit 1
}

if ([string]::IsNullOrEmpty($cmd)) {
    if ([Console]::IsInputRedirected) {
        [Console]::Error.WriteLine('native-shell.ps1: refusing non-interactive empty command')
        exit 1
    }
    & $bash -l -i
    exit $LASTEXITCODE
}

$trimmed = $cmd.Trim()
if ($trimmed -match '^(sftp-server(\.exe)?|internal-sftp)(\s|$)') {
    [Console]::Error.WriteLine('native-shell.ps1: SFTP subsystem not supported through this wrapper; use scp -O (legacy mode)')
    exit 1
}

$tmpPath = Join-Path $env:TEMP ("mas-ssh-" + [guid]::NewGuid().ToString("N") + ".sh")
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($tmpPath, $cmd + "`n", $utf8NoBom)
$rc = 1
try {
    & $bash -l "$tmpPath"
    $rc = $LASTEXITCODE
} finally {
    Remove-Item -LiteralPath $tmpPath -ErrorAction SilentlyContinue
}
exit $rc
PSEOF
```

**Test the wrapper standalone** before wiring it to a key (this is the dry-run; it does not touch `authorized_keys` yet):

```bash
# Should print Windows kernel info, a bash version banner, and "which claude"
# pointing at the native Windows claude.exe. Proves the wrapper works end-to-end.
PSCODE='$env:SSH_ORIGINAL_COMMAND = "uname -a && whoami && which claude && which wsl.exe && cd ~/mas-workspace 2>/dev/null && pwd"; & powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\ProgramData\ssh\native-shell.ps1'
B64=$(printf '%s' "$PSCODE" | iconv -f utf-8 -t utf-16le | base64)
ssh angel@192.168.1.41 "powershell.exe -NoProfile -EncodedCommand $B64"
```

Expected lines (order may vary):

- `MINGW64_NT-10.0-... 192.168.1.41 ...` (Git Bash uname banner — confirms you are in Git Bash, **not** WSL)
- `angel`
- `/c/Program Files/Claude/claude.exe` (or similar — confirms native Windows claude is on PATH)
- `/c/WINDOWS/system32/wsl.exe` (confirms WSL2 is still reachable from inside Git Bash for GPU work)
- Current working dir of the `angel` home (`/c/Users/angel` if `mas-workspace` does not exist yet, or `/c/Users/angel/mas-workspace` if you already created it)

### 2c. Write the new `administrators_authorized_keys`

Same guardrails as the old wrapper — never put PowerShell backticks inside an unquoted bash heredoc; use `[char]13 + [char]10` for CR/LF; let PowerShell build the final file contents from env vars instead of via interpolation.

```bash
# Export the keys as env vars so the SSH'd PowerShell can read them without interpolation.
export MAS_ED25519="$(cat ~/.ssh/id_ed25519.pub)"
export MAS_RSA="$(cat ~/.ssh/id_rsa.pub)"

ssh -o SendEnv=MAS_ED25519 -o SendEnv=MAS_RSA angel@192.168.1.41 'powershell.exe -NoProfile -Command @'\''
$ed  = $env:MAS_ED25519
$rsa = $env:MAS_RSA
$fc  = "command=\"powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File C:\ProgramData\ssh\native-shell.ps1\""
$nl  = [char]13 + [char]10
$content = "$fc $ed$nl$rsa$nl"
$utf8 = New-Object System.Text.UTF8Encoding $false
[System.IO.File]::WriteAllText("C:\ProgramData\ssh\administrators_authorized_keys", $content, $utf8)
icacls "C:\ProgramData\ssh\administrators_authorized_keys" /inheritance:r /grant "SYSTEM:F" /grant "Administrators:F" | Out-Null
Get-Content "C:\ProgramData\ssh\administrators_authorized_keys"
'\''@'
```

> **Note on `SendEnv`:** Windows OpenSSH needs `AcceptEnv MAS_*` in `C:\ProgramData\ssh\sshd_config` for the env vars to transit. If that is not available, inline the key values with `sed` *before* the SSH call instead — still no heredoc expansion.

### 2d. Verify auth still works (both primary and fallback)

```bash
# Primary: ed25519 key -> ForceCommand -> native-shell.ps1 -> Git Bash.
# Should return "MINGW64_NT-10.0-..." — NOT the Linux kernel banner.
ssh angel@192.168.1.41 'uname -a'

# Escape-hatch: id_rsa -> raw Windows cmd.exe. Should return "Microsoft Windows ...".
ssh -i ~/.ssh/id_rsa -o IdentitiesOnly=yes angel@192.168.1.41 'ver'
```

If the primary works but returns `Linux ... Ubuntu` output, the old `wsl-shell.ps1` is still attached to the key — re-check step 2c. If the primary fails entirely, use the id_rsa fallback to restore the backup:

```bash
ssh -i ~/.ssh/id_rsa -o IdentitiesOnly=yes angel@192.168.1.41 'powershell.exe -NoProfile -Command "Copy-Item C:\ProgramData\ssh\administrators_authorized_keys.bak-<timestamp> C:\ProgramData\ssh\administrators_authorized_keys -Force"'
```

## Step 3 — Stage the agent workspace and MCP config directory

Git Bash expands `~` to `C:\Users\angel`, so the orchestrator's existing bash-syntax paths (`~/mas-workspace`, `~/.mas-mcp-configs/RTX5090.json`) resolve to sensible Windows locations with no config changes:

```bash
ssh angel@192.168.1.41 'bash -lc "mkdir -p ~/mas-workspace ~/.mas-mcp-configs && ls -la ~/mas-workspace ~/.mas-mcp-configs"'
# Should list two empty directories under /c/Users/angel/ (Git Bash path view).
```

Write the agent's `CLAUDE.md` via heredoc (adjust content as needed):

```bash
cat <<'EOF' | ssh angel@192.168.1.41 'bash -lc "cat > ~/mas-workspace/CLAUDE.md"'
# RTX5090 Agent - CLAUDE.md
## Role
You run on the RTX5090 box (native Windows 11 claude, Git Bash shell,
WSL2 Ubuntu 24.04 available as a subordinate runtime via wsl.exe).
You are the fast-inference agent for the MAS.

## Reaching the GPU / WSL2
- Native Windows commands: just run them (`powershell.exe -NoProfile -Command "..."`, `nvidia-smi.exe`, etc.)
- WSL2 Ubuntu: `wsl.exe -d Ubuntu -- <cmd>` (e.g. `wsl.exe -d Ubuntu -- nvidia-smi` for CUDA work)
- Git Bash builtins: bash, sed, awk, coreutils — all shipped with Git for Windows
EOF
```

## Step 4 — Update `projects/remote-test/config.yaml` (no-op if unchanged)

The rearch is deliberately transparent to the orchestrator — no config field changes are required. Keep the existing entry:

```yaml
RTX5090:
  runtime: claude_code
  ssh_host: angel@192.168.1.41
  remote_working_dir: ~/mas-workspace
  remote_bridge_path: ~/mas-bridge/index.js
  label: RTX5090
  system_prompt: "You are the fast-inference agent on the RTX5090 box ..."
```

`~/mas-workspace` now resolves to `C:\Users\angel\mas-workspace` via Git Bash; `~/.mas-mcp-configs/RTX5090.json` resolves to `C:\Users\angel\.mas-mcp-configs\RTX5090.json`; the orchestrator does not need to know.

**Optional:** update the `system_prompt` to drop any "WSL2-hosted" language and mention the subordinate-runtime reachability through `wsl.exe`. That is not strictly required — the agent will function — but it will nudge the agent toward using the right tool for the job.

## Step 5 — Bounce the orchestrator and watch pane 4

```bash
cd /Users/angelserrano/Repositories/multi-agent-system-shell
bash scripts/stop.sh projects/remote-test/config.yaml
bash scripts/start.sh projects/remote-test/config.yaml

# Attach and look at pane 4 of the agents window.
tmux attach -t remote-test-agents
# Prefix + q to flash pane numbers; pane 4 should show the claude code TUI
# running NATIVELY ON WINDOWS -- the banner should say something like
# "Claude Code v2.1.x" with a Windows-looking cwd (/c/Users/angel/mas-workspace).
```

From any other agent (or the dev's Claude Code session):

```python
# Should land in RTX5090's inbox and the pane should log a check_messages call
# with no -32603 auth error.
send_to_agent(target_agent="RTX5090", message="hello from dev — ack please")
```

## Step 6 — Smoke tests (acceptance criteria from issue #29)

Run each from the Mac orchestrator host unless noted:

1. **Native file access**

   ```bash
   ssh angel@192.168.1.41 'bash -lc "echo hello > ~/mas-workspace/probe.txt && cat ~/mas-workspace/probe.txt && powershell.exe -NoProfile -Command Get-Content C:\\Users\\angel\\mas-workspace\\probe.txt && rm ~/mas-workspace/probe.txt"'
   # Both `cat` and `Get-Content` should emit "hello" — confirms the bash-path
   # and Windows-path views of the same file match.
   ```

2. **WSL2 reachability from Git Bash**

   ```bash
   ssh angel@192.168.1.41 'bash -lc "wsl.exe -d Ubuntu -- nvidia-smi | head -5"'
   # Should return NVIDIA driver / GPU info from inside WSL2.
   ```

3. **MCP bridge round-trip**

   From the orchestrator host, send a message through the MCP bridge (or any live agent):

   ```python
   send_to_agent(target_agent="RTX5090", message="smoke test: reply with 'pong'")
   ```

   The RTX5090 pane should run `check_messages`, see the task, and reply `pong` via `send_message`. No `-32603` auth error in pane capture. Watchdog (if PR #30 is merged) should NOT flag the pane.

4. **End-to-end task delegation**

   Manager assigns a trivial task to RTX5090 via the normal task flow, RTX5090 executes, reports `{status: pass, ...}` back. Confirms state machine transitions + delivery protocol work against a Windows-hosted agent.

## Cutover from WSL2-hosted Claude

If the box is currently running the pre-#29 WSL2 routing (per commit `4c2acf1`), the cutover is a single step once the user has finished step 1 above:

1. Run step 2a (backup), step 2b (upload `native-shell.ps1`), and step 2d (standalone wrapper test) from the sections above.
2. Rewrite `administrators_authorized_keys` per step 2c. **This is the cutover moment** — the next SSH session to the box will land in Git Bash instead of WSL bash.
3. Run the step 6 smoke tests.
4. Once smoke tests pass, delete the now-unused wrapper and legacy artifacts:

   ```bash
   ssh angel@192.168.1.41 'powershell.exe -NoProfile -Command "Remove-Item C:\ProgramData\ssh\wsl-shell.ps1 -Force; wsl.exe -d Ubuntu -u angel -- bash -lc \"rm -rf ~/mas-bridge ~/.mas-mcp-configs ~/mas-workspace\" 2>&1 | Out-Null"'
   ```

   (The WSL2 Ubuntu distro itself stays installed — it is now a subordinate runtime for GPU work, reached from Windows claude via `wsl.exe`. Only the *agent-specific* artifacts under `~/mas-bridge`, `~/.mas-mcp-configs`, and `~/mas-workspace` inside WSL are cleared.)

## Re-bootstrap checklist (box was wiped)

If the 5090 is reset to a fresh Windows install, repeat steps 1–6 in order. Budget ~20 minutes of real time: most of it is the interactive `claude login` OAuth flow in step 1 and Git for Windows / Claude Code installers.

## Known pitfalls

1. **Heredoc corruption — same as the WSL2 quickstart.** PowerShell uses backtick (`` ` ``) as its escape character. zsh and bash also treat backticks as command substitution inside *unquoted* heredocs — which means `` `r`n `` in a `<<PSEOF` block is silently replaced by the output of running `r` and `n` as commands (both "not found"). Always use `<<'PSEOF'` (quoted delimiter) when the body contains PowerShell, and use `[char]13 + [char]10` for CR/LF instead of `` `r`n ``.
2. **Modern scp (SFTP mode) has to be refused explicitly.** Same rationale as the old wrapper — Windows OpenSSH's built-in SFTP subsystem runs `sftp-server.exe` with a name/protocol that does not speak POSIX stdio to Git Bash any better than it did to WSL bash. The refusal clause in `native-shell.ps1` forces clients into `scp -O` legacy mode, which runs as a regular `scp -t` shell command that Git Bash's bundled `scp.exe` handles natively. Do not remove the refusal clause.
3. **`$env:USERPROFILE\.ssh\authorized_keys` is still ignored.** `angel` is an admin, so Windows OpenSSH only reads `C:\ProgramData\ssh\administrators_authorized_keys`. Unchanged from pre-#29.
4. **Git Bash's `~` is `C:\Users\angel`, not `/home/angel`.** If you are used to WSL paths, remember that `~/mas-workspace` in the Git Bash world resolves to `C:\Users\angel\mas-workspace` — a Windows filesystem path, visible from `dir C:\Users\angel\mas-workspace` in cmd.exe and `Get-ChildItem` in PowerShell. The `/mnt/c/` prefix from WSL land does not apply.
5. **`claude login` cannot be run from SSH.** The OAuth flow opens a browser; headless `claude login` over SSH hangs. Step 1 really does require a console or RDP session at the RTX5090.
6. **If `which claude` from inside the Git Bash wrapper is empty**, the PATH did not pick up the Claude install. Fix by re-running step 1 as the `angel` user from a fresh PowerShell window (the installer edits the user PATH) and then logging out and back in on the Windows side, or reboot. The ForceCommand wrapper sources `.bash_profile` via `-l` so any login-shell PATH edits take effect for subsequent SSH sessions.

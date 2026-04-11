# RTX5090 Quickstart: WSL2 Bootstrap + MAS Agent

Step-by-step to bring the `RTX5090` agent online (or rebuild it if the Windows box is wiped). See [README-RTX5090.md](README-RTX5090.md) for the architectural *why*.

## Prerequisites

- Windows 11 workstation at `192.168.1.41` with an **Administrator** account named `angel`.
- Windows OpenSSH Server (`sshd`) already running and accepting the Mac's `id_ed25519` key in `C:\ProgramData\ssh\administrators_authorized_keys`. (If SSH key auth is not yet set up at all, sit at the console once and paste the Mac's `~/.ssh/id_ed25519.pub` into that file — single line, CRLF line endings.)
- From the Mac orchestrator host, `ssh angel@192.168.1.41 'whoami'` must return `angel` before you start.
- WSL2 **platform** enabled. (Docker Desktop's `docker-desktop` distro being present is enough signal that it is.)

## One-shot bootstrap (assumes baseline SSH works)

Run all of the following **from the Mac orchestrator host**. Every command SSHes into the 5090; nothing is typed at the 5090 console.

```bash
# 0. Sanity check
ssh angel@192.168.1.41 'powershell.exe -NoProfile -Command "wsl --status; wsl --list --verbose"'
```

### 1. Install Ubuntu 24.04 in WSL2 (non-interactive)

`wsl --install -d Ubuntu --no-launch` stages the Appx package but does **not** register a distro — you must invoke the per-user launcher with `install --root` to actually register it without prompting for a username/password:

```bash
ssh angel@192.168.1.41 'powershell.exe -NoProfile -Command "wsl --install -d Ubuntu --no-launch"'
ssh angel@192.168.1.41 'powershell.exe -NoProfile -Command "$u = (Get-AppxPackage | Where-Object Name -like *Ubuntu*).InstallLocation; & \"$u\\ubuntu.exe\" install --root"'
ssh angel@192.168.1.41 'wsl --list --verbose'   # Ubuntu should show up as Running/Stopped
```

### 2. Create the `angel` user inside Ubuntu and set defaults

```bash
ssh angel@192.168.1.41 'wsl -d Ubuntu -u root -- bash -lc "useradd -m -s /bin/bash -u 1000 -G sudo angel && echo \"angel ALL=(ALL) NOPASSWD:ALL\" > /etc/sudoers.d/angel && chmod 440 /etc/sudoers.d/angel && id angel"'

ssh angel@192.168.1.41 'powershell.exe -NoProfile -Command "$u = (Get-AppxPackage | Where-Object Name -like *Ubuntu*).InstallLocation; & \"$u\\ubuntu.exe\" config --default-user angel; wsl --set-default Ubuntu"'

# Verify: should print `angel` and `/home/angel`-ish.
ssh angel@192.168.1.41 'wsl -- bash -lc "whoami && id && cat /etc/os-release | head -2"'
```

### 3. Install node + git + claude code + openssh-client inside WSL

```bash
ssh angel@192.168.1.41 'wsl -- bash -lc "sudo apt-get update -qq && sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq curl git ca-certificates build-essential openssh-client"'

ssh angel@192.168.1.41 'wsl -- bash -lc "curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash - && sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq nodejs && node --version && npm --version"'

ssh angel@192.168.1.41 'wsl -- bash -lc "mkdir -p ~/.npm-global && npm config set prefix ~/.npm-global && grep -q npm-global ~/.bashrc || echo \"export PATH=\\\$HOME/.npm-global/bin:\\\$PATH\" >> ~/.bashrc && export PATH=\$HOME/.npm-global/bin:\$PATH && npm install -g @anthropic-ai/claude-code && ~/.npm-global/bin/claude --version"'

# Symlink claude into /usr/local/bin so it is on PATH for non-interactive shells.
ssh angel@192.168.1.41 'wsl -- bash -lc "sudo ln -sf ~/.npm-global/bin/claude /usr/local/bin/claude && which claude && claude --version"'
```

`openssh-client` is required because the orchestrator's `scripts/start.sh` uses **classic mode** `scp -O` (legacy protocol) to push MCP configs — that path runs `scp -t <dest>` as a remote command, which the ForceCommand wrapper will then execute inside WSL.

### 4. Ship the mas-bridge into WSL

From the repo root on the Mac:

```bash
cd /Users/angelserrano/Repositories/multi-agent-system-shell
tar czf - --exclude=node_modules -C mcp-bridge . | \
  ssh angel@192.168.1.41 'wsl -- bash -c "mkdir -p ~/mas-bridge && cd ~/mas-bridge && tar xzf - && rm -f ._*"'

ssh angel@192.168.1.41 'wsl -- bash -lc "cd ~/mas-bridge && npm install --omit=dev"'

# Smoke test - should print usage/env-var error (means the JS loaded cleanly).
ssh angel@192.168.1.41 'wsl -- bash -lc "cd ~/mas-bridge && node -e \"require(\\\"./index.js\\\")\" 2>&1 | tail -1"'
```

### 5. Create the workspace and drop a CLAUDE.md

```bash
ssh angel@192.168.1.41 'wsl -- bash -c "mkdir -p ~/mas-workspace"'

# Write the agent's CLAUDE.md via heredoc (adjust content as needed):
cat <<'EOF' | ssh angel@192.168.1.41 'wsl -- bash -c "cat > /home/angel/mas-workspace/CLAUDE.md"'
# RTX5090 Agent - CLAUDE.md
## Role
You run on the RTX5090 box. You are the fast-inference agent for the MAS.
# ... (see the copy already on the box for the canonical text)
EOF
```

### 6. Install the SSH ForceCommand wrapper on Windows

**⚠️ Take a backup first.** If you corrupt `administrators_authorized_keys`, you are locked out and need either password auth or physical console access.

```bash
ssh angel@192.168.1.41 'powershell.exe -NoProfile -Command "Copy-Item C:\ProgramData\ssh\administrators_authorized_keys C:\ProgramData\ssh\administrators_authorized_keys.bak-$(Get-Date -Format yyyyMMddHHmmss) -Force"'
```

Upload `wsl-shell.ps1` to the Windows side. **Do not use a bash/zsh heredoc that contains PowerShell backtick escapes (`\``r\``n`)** — those get interpreted as command substitution by the shell and will silently corrupt the script. Use `Out-File` over a pipe with a `'PSEOF'`-quoted heredoc (single-quoted delimiter disables expansion):

```bash
cat <<'PSEOF' | ssh angel@192.168.1.41 'powershell.exe -NoProfile -Command "$input | Out-File -FilePath C:\ProgramData\ssh\wsl-shell.ps1 -Encoding ascii -Force"'
# wsl-shell.ps1 - SSH ForceCommand wrapper that routes to WSL Ubuntu bash.
# Preserves TTY by writing SSH_ORIGINAL_COMMAND to a temp file and exec'ing
# bash with that file as a script (stdin/stdout/stderr inherit PTY from sshd).
#
# SFTP subsystem requests (scp in default SFTP mode) arrive with
# SSH_ORIGINAL_COMMAND set to "sftp-server.exe" — Windows OpenSSH's
# Subsystem handler. We refuse those fast so the client's legacy-mode
# fallback (`scp -O`) kicks in and runs as a regular `scp -t` command,
# which the wrapper then correctly routes through WSL bash where the
# actual scp binary lives. Without this early reject, the wrapper would
# try to execute sftp-server.exe through WSL interop and hang on the
# garbled pipe protocol.
$ErrorActionPreference = 'Stop'
$cmd = $env:SSH_ORIGINAL_COMMAND

if ([string]::IsNullOrEmpty($cmd)) {
    if ([Console]::IsInputRedirected) {
        [Console]::Error.WriteLine('wsl-shell.ps1: refusing non-interactive empty command')
        exit 1
    }
    & wsl.exe -d Ubuntu
    exit $LASTEXITCODE
}

$trimmed = $cmd.Trim()
if ($trimmed -match '^(sftp-server(\.exe)?|internal-sftp)(\s|$)') {
    [Console]::Error.WriteLine('wsl-shell.ps1: SFTP subsystem not supported through this wrapper; use scp -O (legacy mode)')
    exit 1
}

$tmpWin = Join-Path $env:TEMP ("mas-ssh-" + [guid]::NewGuid().ToString("N") + ".sh")
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($tmpWin, $cmd + "`n", $utf8NoBom)
$rc = 1
try {
    $tmpWsl = (& wsl.exe -d Ubuntu -e wslpath -u "$tmpWin" | Out-String).Trim()
    & wsl.exe -d Ubuntu -- bash -l "$tmpWsl"
    $rc = $LASTEXITCODE
} finally {
    Remove-Item -LiteralPath $tmpWin -ErrorAction SilentlyContinue
}
exit $rc
PSEOF
```

**Test the wrapper standalone** before wiring it to a key (this is the dry-run; it doesn't touch `authorized_keys` yet):

```bash
# This should print Linux kernel info, proving the wrapper works end-to-end.
PSCODE='$env:SSH_ORIGINAL_COMMAND = "uname -a && whoami && cd ~/mas-workspace && pwd && which claude"; & powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\ProgramData\ssh\wsl-shell.ps1'
B64=$(printf '%s' "$PSCODE" | iconv -f utf-8 -t utf-16le | base64)
ssh angel@192.168.1.41 "powershell.exe -NoProfile -EncodedCommand $B64"
```

### 7. Write the new `administrators_authorized_keys`

This is the step where the heredoc escaping bit me the first time around. **Never** put PowerShell backticks inside an *unquoted* bash heredoc. Use a fully-quoted `<<'PSEOF'` delimiter, and let PowerShell build the final file contents using its own string operators — pass external values in via environment variables, not interpolation.

```bash
# Export the keys as env vars so the SSH'd PowerShell can read them without interpolation.
export MAS_ED25519="$(cat ~/.ssh/id_ed25519.pub)"
export MAS_RSA="$(cat ~/.ssh/id_rsa.pub)"

ssh -o SendEnv=MAS_ED25519 -o SendEnv=MAS_RSA angel@192.168.1.41 'powershell.exe -NoProfile -Command @'\''
$ed  = $env:MAS_ED25519
$rsa = $env:MAS_RSA
$fc  = "command=\"powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File C:\ProgramData\ssh\wsl-shell.ps1\""
$nl  = [char]13 + [char]10
$content = "$fc $ed$nl$rsa$nl"
$utf8 = New-Object System.Text.UTF8Encoding $false
[System.IO.File]::WriteAllText("C:\ProgramData\ssh\administrators_authorized_keys", $content, $utf8)
icacls "C:\ProgramData\ssh\administrators_authorized_keys" /inheritance:r /grant "SYSTEM:F" /grant "Administrators:F" | Out-Null
Get-Content "C:\ProgramData\ssh\administrators_authorized_keys"
'\''@'
```

> **Note on `SendEnv`:** Windows OpenSSH needs `AcceptEnv MAS_*` in `C:\ProgramData\ssh\sshd_config` for the env vars to transit. If that's not available, inline the key values with `sed` *before* the SSH call instead — still no heredoc expansion.

### 8. Verify auth still works (both primary and fallback)

```bash
# Primary: ed25519 key → ForceCommand → WSL bash. Should return Linux.
ssh angel@192.168.1.41 'uname -a'

# Escape-hatch: id_rsa → raw Windows cmd.exe. Should return Microsoft Windows.
ssh -i ~/.ssh/id_rsa -o IdentitiesOnly=yes angel@192.168.1.41 'ver'
```

If the primary works but returns Windows output, the wrapper isn't on the key → re-check step 7. If the primary fails entirely, use the id_rsa fallback to restore the backup:

```bash
ssh -i ~/.ssh/id_rsa -o IdentitiesOnly=yes angel@192.168.1.41 'powershell.exe -NoProfile -Command "Copy-Item C:\ProgramData\ssh\administrators_authorized_keys.bak-<timestamp> C:\ProgramData\ssh\administrators_authorized_keys -Force"'
```

### 9. Update `projects/remote-test/config.yaml`

Replace the stub entry:

```diff
-  RTX5090:
-    runtime: script
-    ssh_host: angel@192.168.1.41
-    command: powershell
-    label: RTX5090
+  RTX5090:
+    runtime: claude_code
+    ssh_host: angel@192.168.1.41
+    remote_working_dir: ~/mas-workspace
+    remote_bridge_path: ~/mas-bridge/index.js
+    label: RTX5090
+    system_prompt: "You are the fast-inference agent on the RTX5090 box ..."
```

### 10. Bounce the orchestrator and watch pane 4

```bash
cd /Users/angelserrano/Repositories/multi-agent-system-shell
bash scripts/stop.sh projects/remote-test/config.yaml
bash scripts/start.sh projects/remote-test/config.yaml

# Attach and look at pane 4 of the agents window.
tmux attach -t remote-test-agents
# Prefix + q to flash pane numbers; pane 4 should show the claude code TUI,
# not a cmd.exe/PowerShell prompt.
```

From any other agent (or the dev's Claude Code session):

```python
# Should land in RTX5090's inbox and the pane should log a check_messages call.
send_to_agent(target_agent="RTX5090", message="hello from dev — ack please")
```

## Re-bootstrap checklist (box was wiped)

If the 5090 is reset to a fresh Windows install, repeat steps 0–9 in order. The only step that **must** be done at the console (because SSH isn't working yet) is the initial push of the Mac's `id_ed25519.pub` into `administrators_authorized_keys`; everything else is scripted from the Mac. Budget ~15 minutes of real time, most of which is `apt` downloads inside WSL.

## Known pitfalls

1. **`wsl --install -d Ubuntu --no-launch` says "Ubuntu has been installed" but `wsl -l -v` does not show it.** The Appx is staged; the distro isn't registered. Run `ubuntu.exe install --root` from the Appx install dir to complete registration.
2. **Modern scp (SFTP mode) has to be refused explicitly or it hangs forever.** When scp runs in its default SFTP mode, the client negotiates the `sftp` subsystem and sshd honors our `command="…wsl-shell.ps1"` ForceCommand by invoking the wrapper with `SSH_ORIGINAL_COMMAND='sftp-server.exe '` (the Windows-side `Subsystem sftp sftp-server.exe` handler name). If the wrapper naively writes that to a bash temp file and runs it, WSL's Windows-interop will launch `sftp-server.exe` on the Windows side with its stdio piped through WSL — the SFTP wire protocol gets garbled and **scp hangs indefinitely with no error**. That's why the wrapper has an explicit `sftp-server(\.exe)?|internal-sftp` refusal clause that exits 1 immediately; scp's `-O` legacy-mode retry then runs as a regular `scp -t` shell command, which the wrapper routes through WSL bash, and the real `scp` binary in Ubuntu handles the transfer. `scripts/start.sh` already issues `scp` without `-O` first and falls back to `scp -O` on failure — the refusal + fallback combo is what makes config copies work. Don't remove the refusal clause.
3. **`$env:USERPROFILE\.ssh\authorized_keys` looks like it has the key but SSH ignores it.** Correct — `angel` is an admin, so Windows OpenSSH *only* reads `C:\ProgramData\ssh\administrators_authorized_keys`. Edit that file.
4. **`wsl.exe -d Ubuntu` drops you into `/mnt/c/Users/angel`, not `/home/angel`.** That's because the Windows cwd is inherited. The orchestrator always prefixes `cd ~/mas-workspace` so it doesn't matter, but be aware when running ad-hoc commands.
5. **Heredoc corruption (the bootstrap incident).** PowerShell uses backtick (`` ` ``) as its escape character. zsh and bash also treat backticks as command substitution inside *unquoted* heredocs — which means `\`r\`n` in a ``<<PSEOF`` block is silently replaced by the output of running `r` and `n` as commands (both "not found"). Always use `<<'PSEOF'` (quoted delimiter) when the body contains PowerShell, and use `[char]13 + [char]10` for CR/LF instead of `` `r`n ``.

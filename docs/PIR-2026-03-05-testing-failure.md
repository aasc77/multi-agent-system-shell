# Post-Incident Review: TDD Testing Failure

**Date**: 2026-03-05
**Project**: multi-agent-system-shell
**Summary**: 839 unit tests passed, but the actual demo failed on 10 different issues. None were caught by the test suite.

---

## What Happened

Despite a comprehensive test suite (839 tests, 18 test files), every real-world failure during the multi-machine demo was in the **shell integration layer** -- flag names, SSH options, path expansion, environment variables, TTY handling. The unit tests validated component logic (state machine, task queue, NATS client) thoroughly but the launch script was tested only by coarse substring matches on dry-run output.

## The 10 Real Failures

| # | Failure | Testable? | Had Test? | Why Missed |
|---|---------|-----------|-----------|------------|
| 1 | Wrong CLI flag (`--dangerouslySkipPermissions`) | Yes | Partial | Test only checked for `claude` + `--mcp-config`, not exact flags |
| 2 | `~` not expanded by Node in MCP configs | Yes | No | All test fixtures used absolute paths |
| 3 | SSH "too many auth failures" | Yes (flag check) | No | SSH tests only checked hostname presence |
| 4 | SSH in while-read consumed stdin | Yes | No | No test for SCP config copying phase |
| 5 | Claude Code onboarding prompts on remote | No (environmental) | No | Requires real first-launch on machine |
| 6 | `$HOME` expanded by bash, not SSH | Yes | No | No SSH agent MCP config content test |
| 7 | `allow-rename` vs `allow-set-title` (tmux) | Yes | No | Only pane-border-status tested |
| 8 | Missing `--strict-mcp-config` flag | Yes | No | No test for this flag |
| 9 | Missing `ssh -t` for TTY | Yes | No | SSH tests only checked hostname |
| 10 | CLAUDECODE env var blocking nested launch | Partially | No | Root cause is environmental |

## Root Cause Analysis

### 1. Dry-run tests were too coarse

Tests searched for high-level concepts ("does `ssh` appear?") instead of exact flag correctness ("does `ssh -t -o IdentitiesOnly=yes` appear?"). They verified **category** but not **correctness**.

### 2. Test fixtures never exercised edge cases

Every fixture used Python's `tmp_path` with absolute paths. No fixture ever used `~`, relative paths, or multi-host SSH configs. The tilde-expansion code path was never hit.

### 3. Zero tests for remote agent specifics

The SSH agent fixture existed but only 3 tests used it, all checking just "does `ssh` and the hostname appear in output?" Nothing verified:
- SSH flags (`-t`, `-n`, `-o IdentitiesOnly=yes`)
- SCP to multiple remotes (stdin bug)
- Remote path resolution (`echo ~` via SSH)
- Bash quoting in SSH-wrapped commands

### 4. Environmental issues can't be unit tested

Claude Code onboarding (failure 5) and CLAUDECODE env var (failure 10) require actual runtime. No mock can simulate "first launch on a fresh machine."

### 5. Tests validated contracts, not commands

The 839 tests checked: "Does the state machine transition correctly? Does the task queue track status?" These are in-process logic tests with mocked dependencies. The test pyramid had a solid base (unit tests) but a **completely empty top** (0 integration tests, 0 smoke tests).

## Token Cost

The TDD approach consumed significant tokens generating and iterating on tests that:
- Tested mocked behavior, not real behavior
- Gave false confidence that the system worked
- Required the same amount of real-world debugging anyway

## Improvement Plan

### Phase 1: Fix the dry-run tests (low effort, high value)

Add **exact flag assertions** to existing start.sh dry-run tests:

```python
# Instead of:
assert "claude" in output
assert "--mcp-config" in output

# Do:
assert "--dangerously-skip-permissions" in output
assert "--strict-mcp-config" in output
assert "--allowedTools" in output

# For SSH agents:
assert "ssh -t" in output
assert "IdentitiesOnly=yes" in output
assert "ssh -n" in scp_output  # for config copying
assert "allow-set-title off" in output
```

### Phase 2: Add MCP config content tests (low effort, high value)

Test the **generated JSON files**, not just that they exist:

```python
def test_remote_mcp_config_has_absolute_paths():
    """Verify ~ is expanded to absolute paths in remote MCP configs"""
    config = json.load(open(mcp_config_path))
    bridge_path = config["mcpServers"]["mas-bridge"]["args"][0]
    assert "~" not in bridge_path
    assert bridge_path.startswith("/")

def test_remote_mcp_config_uses_custom_node():
    """Verify remote_node_path is used as command"""
    config = json.load(open(mcp_config_path))
    assert config["mcpServers"]["mas-bridge"]["command"] == "/Users/user/local/bin/node"
```

### Phase 3: Add a smoke test script (medium effort, highest value)

Create `scripts/smoke-test.sh` that actually launches a minimal 2-agent project and verifies:

```bash
#!/usr/bin/env bash
# Smoke test: launches a real session, verifies agents start, sends a test message

1. Start NATS
2. Launch start.sh with demo project
3. Wait 15s for agents to initialize
4. Check all tmux panes exist and are running
5. Check NATS stream has messages
6. Check orchestrator log for "Started first task"
7. Wait for task completion (timeout 60s)
8. Verify tasks.json shows "completed"
9. Kill session
10. Report pass/fail
```

This single script would have caught failures 1, 2, 8, 9, and 10.

### Phase 4: Add a remote smoke test checklist (manual, for new machines)

A pre-flight checklist for setting up remote agents, since these can't be automated:

```
[ ] claude auth status shows loggedIn: true
[ ] claude -p "hello" returns a response
[ ] claude (interactive) starts without login/theme prompts
[ ] node --version returns expected version
[ ] node ~/mas-bridge/index.js errors with "AGENT_ROLE required" (proves path works)
[ ] ssh from orchestrator host connects without password prompt
[ ] nats connectivity: echo test from remote to orchestrator NATS
```

### Phase 5: Reduce TDD scope for shell scripts

**Stop unit-testing bash scripts with mocked subprocess output.** Instead:
- Unit test Python logic (state machine, task queue, config parsing) -- these tests are valuable
- Smoke test shell scripts with real execution -- dry-run assertions have diminishing returns
- Integration test the full pipeline -- this is what actually matters

### Priority Order

1. **Smoke test script** (Phase 3) -- would have prevented the most pain
2. **Exact flag assertions** (Phase 1) -- 30 minutes of work, catches typos
3. **MCP config content tests** (Phase 2) -- catches path/expansion bugs
4. **Remote checklist** (Phase 4) -- prevents environmental issues
5. **Reduce TDD scope** (Phase 5) -- saves future tokens

## Key Takeaway

> 839 unit tests with mocks gave false confidence. One 20-line smoke test that actually launches the system would have caught more bugs than all of them combined.

The test pyramid was inverted: heavy at the unit level (cheap to write, low real-world coverage), empty at the integration level (harder to write, catches actual failures). For shell-heavy orchestration systems, **real execution tests beat mocked dry-run tests every time**.

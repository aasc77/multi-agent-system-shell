# DGX Quickstart: Fara-7B + Magentic-UI

## Prerequisites

- NVIDIA DGX Spark (ARM64/Blackwell) at `192.168.1.51`
- Docker with NVIDIA container toolkit installed
- Hugging Face account with access to `microsoft/Fara-7B`
- SSH access: `ssh dgx@192.168.1.51`

## One-Shot Deploy

SSH into the DGX and run everything:

```bash
ssh dgx@192.168.1.51
cd ~/multi-agent-system-shell
bash scripts/dgx/deploy-all.sh
```

This runs all steps in order: HF token setup, vLLM container, Magentic-UI install, healthcheck wait, and smoke test.

## Step-by-Step

### 1. Store HF Token

```bash
cd ~/multi-agent-system-shell
bash scripts/dgx/setup-hf-token.sh
```

Prompts for your token (hidden input), stores at `~/.config/huggingface/token` with `600` perms.

### 2. Start vLLM with Fara-7B

```bash
cd ~/multi-agent-system-shell
bash scripts/dgx/setup-vllm.sh
```

Pulls the NVIDIA ARM64 vLLM image and starts Fara-7B on port 5000. Preview commands first with `--dry-run`.

Wait for the model to load (can take a few minutes):

```bash
bash scripts/dgx/manage-vllm.sh status
```

### 3. Install Magentic-UI

```bash
cd ~/multi-agent-system-shell
bash scripts/dgx/setup-magentic-ui.sh
```

Clones, installs, and configures Magentic-UI to use the local vLLM at `http://localhost:5000/v1`.

### 4. Start Magentic-UI

```bash
bash scripts/dgx/manage-magentic-ui.sh start
```

### 5. Verify

```bash
bash scripts/dgx/smoke-test.sh
```

## Managing the Stack

### vLLM

```bash
bash scripts/dgx/manage-vllm.sh status    # healthcheck
bash scripts/dgx/manage-vllm.sh logs      # tail logs
bash scripts/dgx/manage-vllm.sh stop      # stop container
bash scripts/dgx/manage-vllm.sh start     # restart container
bash scripts/dgx/manage-vllm.sh flush     # reclaim memory (sudo)
```

### Magentic-UI

```bash
bash scripts/dgx/manage-magentic-ui.sh status
bash scripts/dgx/manage-magentic-ui.sh stop
bash scripts/dgx/manage-magentic-ui.sh start
```

## Troubleshooting

### vLLM healthcheck fails
```bash
docker logs fara-vllm    # check for OOM or model download issues
```

### Slow inference after prolonged use
Flush unified memory caches:
```bash
bash scripts/dgx/manage-vllm.sh flush
```

### Magentic-UI can't reach vLLM
Verify vLLM is listening:
```bash
curl http://localhost:5000/v1/models
```

# legacy-mod-amf

Generic legacy modernization platform built on **Microsoft Agent Framework (MAF)** and Python, with a pluggable **cartridge** model for different migration pairs.

## What this is

A platform for automated legacy code modernization that separates:

- **Platform core** (invariant): discovery, dependency graph, strangler-fig wave planning, semantic chunking, MAF workflow execution, verification, eval.
- **Migration cartridges** (variant): per-pair rules, LLM translators, SDK/API mappings, test-framework mappings, rubrics, golden corpus.

Cartridges range from almost-fully-deterministic (Python 2→3, ~95% rules) to LLM-heavy (COBOL→Java, ~90% LLM). One platform, many cartridges.

## Bundled cartridge

- **`aws_lambda_polyglot_to_azure_fn_py`** — migrate AWS Lambdas (Python, Node, TypeScript, Java, C#) to Azure Functions in Python, platform-scale with cross-Lambda dependency analysis and strangler-fig wave planning.

Planned cartridges: `python2_to_python3`, `java8_to_java22`, `dotnet_fw48_to_dotnet8`, `angular1_to_react18`, `cobol_to_java17`.

## Install

```bash
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"
cp .env.example .env
```

## Run the migrator

The top-level driver is `run_migration.py`. It reads its configuration from `.env` at the repo root and migrates whatever source repo you point it at. The bundled example migrates `aws_legacy/` (AWS Lambda Python app) into `azure_fn_migrated/` (Azure Functions v2, Python).

### 1. Configure `.env`

```dotenv
# --- LLM (OpenAI gpt-5.4-mini) ---
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-5.4-mini
LEGACY_MOD_PROVIDER=openai

# --- where to read from and write to ---
SOURCE_DIR=aws_legacy/generated_code       # legacy AWS Lambda app
MIGRATED_DIR=azure_fn_migrated             # target: Azure Functions v2 (Python)
REPO_ID=legacy-aws                         # logical id (names the output workspace)
CARTRIDGE=aws_lambda_polyglot_to_azure_fn_py

# --- optional knobs ---
MAX_PARALLEL=2                             # parallel unit-workers per wave
MAX_BUDGET_USD=10.0                        # soft budget cap
```

### 2. Dry-run (plan only, no LLM calls)

Good for sanity-checking discovery + wave planning before spending tokens:

```bash
DRY_RUN=1 python run_migration.py
```

Expected output:
```
[startup] provider=openai  cartridge=aws_lambda_polyglot_to_azure_fn_py  repo_id=legacy-aws
[startup] source=.../aws_legacy/generated_code
[startup] target=.../azure_fn_migrated/legacy-aws
[discover] inventory has 2 module(s): ['handlers', 'services']
[plan] 2 backlog items across 1 wave(s)
```

### 3. Full migration

```bash
python run_migration.py
```

Artifacts land under:

```
azure_fn_migrated/legacy-aws/
├── units/<unit_id>/           # migrated Azure Functions (function_app.py, requirements.txt, ...)
└── checkpoints/<run_id>.json  # full pipeline state (inventory + graph + backlog + wave results)
```

### Alternative providers

Swap the top section of `.env` to point at a different LLM; no other changes needed:

```dotenv
# Azure OpenAI
LEGACY_MOD_PROVIDER=azure-openai
AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com
AZURE_OPENAI_API_KEY=...                   # or DefaultAzureCredential
AZURE_OPENAI_DEPLOYMENT=gpt-5.4-mini

# Anthropic direct
LEGACY_MOD_PROVIDER=anthropic
ANTHROPIC_API_KEY=...
ANTHROPIC_MODEL=claude-sonnet-4-5

# Azure Claude (via Azure AI Foundry)
LEGACY_MOD_PROVIDER=azure-claude
AZURE_ANTHROPIC_ENDPOINT=https://your-foundry.services.ai.azure.com
AZURE_ANTHROPIC_API_KEY=...                # or DefaultAzureCredential
AZURE_ANTHROPIC_MODEL=claude-sonnet-4-5

# Local Ollama (free — uses your GPU/CPU)
LEGACY_MOD_PROVIDER=ollama
OLLAMA_BASE_URL=http://localhost:11434/v1
OLLAMA_MODEL=qwen3.6:latest                # whatever `ollama ls` shows
```

#### Running against Ollama

1. Install Ollama (`brew install ollama` / see ollama.com).
2. Make sure `ollama serve` is running and the target model is pulled. Check with:
   ```bash
   ollama ls
   # NAME                  ID              SIZE      MODIFIED
   # qwen3.6:latest        07d35212591f    23 GB     ...
   ```
3. Point `.env` at Ollama (paste the `NAME` column verbatim into `OLLAMA_MODEL`):
   ```dotenv
   LEGACY_MOD_PROVIDER=ollama
   OLLAMA_MODEL=qwen3.6:latest
   ```
4. Run as usual:
   ```bash
   python run_migration.py
   ```

Cost is zero — the budget tracker recognizes `qwen*`, `llama*`, `gemma*`, `phi*`, `mistral*`, `mixtral*`, `deepseek*`, and other local-model prefixes as free. Your `MAX_BUDGET_USD` cap still loads but won't trigger.

> Note: migration quality depends heavily on the model. Mid-size models (14B+) are recommended; smaller models (7B and below) often struggle with the tool-call + JSON-schema discipline required by the reviewer/security agents. If you hit reviewer-verdict parsing errors, try a larger Ollama model before switching back to OpenAI.

### Cost controls

Every LLM call's `input_tokens`/`output_tokens` are priced against the per-model rate table in `maf_generic_migrator_v1/platform_core/maf_workflows/budget.py` and accumulated against `MAX_BUDGET_USD`. When the cap is hit:

- The current agent call's cost is recorded.
- Every subsequent call (translator, reviewer, security) is **skipped** and the unit is marked failed with a `budget exhausted` error.
- Remaining waves are not started.

Additional levers:

| Setting | Effect |
| --- | --- |
| `MAX_BUDGET_USD=1.0` | HARD halt at $1.00 total — run can fail partway but will never overspend. |
| `MAX_PARALLEL=1` | One unit at a time; eliminates peak concurrent LLM spend. |
| `DRY_RUN=1` | Plan only — zero LLM calls. Always start here to verify discovery + wave planning. |

The final section of every run prints running cost and per-model breakdown:

```
[cost] $0.0183 of $10.00 used across 7 LLM calls  (45231 in / 12004 out tok)  [ok]
       gpt-5.4-mini: $0.0183  (7 calls, 45231/12004 tok)
```

If the cap was hit, `[ok]` becomes `[exhausted]`.

Unknown models (ones not in `PRICING`) are priced at a conservative fallback (`$1/1M` input, `$5/1M` output) so silent zero-cost reporting is impossible.

**Per-run cost persistence.** Every run writes two artifacts under the workdir:

- `azure_fn_migrated/<repo_id>/cost_reports/<run_id>.json` — full structured report (budget + per-model + per-role + per-unit breakdown).
- `azure_fn_migrated/<repo_id>/cost_log.jsonl` — append-only one-line summary per run, so run-over-run cost history is greppable:
  ```bash
  # all costs so far
  jq -c '{run_id, used_usd, calls}' azure_fn_migrated/legacy-aws/cost_log.jsonl

  # total spend across every run of this repo_id
  jq -s '[.[].used_usd] | add' azure_fn_migrated/legacy-aws/cost_log.jsonl
  ```

Per-unit cost lives on each `UnitResult.cost_usd` in the checkpoint too, so `cat azure_fn_migrated/legacy-aws/checkpoints/<run_id>.json | jq '.waves[].unit_results[] | {unit_id, cost_usd}'` shows which unit cost what.

### Offline plan only (no LLM)

You can also exercise discovery + graph + planner without touching `run_migration.py` via the `legacy-mod` CLI:

```bash
legacy-mod plan \
  --cartridge aws_lambda_polyglot_to_azure_fn_py \
  --source aws_legacy/generated_code \
  --out .workspace/plan
```

Produces `inventory.json`, `graph.json`, `graph.dot`, `graph.mmd`, `graph.svg` (if graphviz is installed), `backlog.json`.

## Provider matrix

| `LEGACY_MOD_PROVIDER` | Model default | Env vars required |
| --- | --- | --- |
| `openai` (default) | `gpt-5.4-mini` | `OPENAI_API_KEY` |
| `azure-openai` | `gpt-5.4-mini` (deployment) | `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY` (or DefaultAzureCredential), `AZURE_OPENAI_DEPLOYMENT` |
| `azure-claude` | `claude-sonnet-4-5` | `AZURE_ANTHROPIC_ENDPOINT`, `AZURE_ANTHROPIC_API_KEY` (or DefaultAzureCredential) |
| `anthropic` | `claude-sonnet-4-5` | `ANTHROPIC_API_KEY` |

When `LEGACY_MOD_PROVIDER` is unset, the platform autodetects from which credential env vars are present.

## Wave verification with emulators (optional)

For end-to-end integration checks, start the Azure emulator stack:

```bash
docker compose -f docker-compose.emulators.yaml up -d
export LEGACY_MOD_REQUIRE_EMULATORS=1     # fail wave if emulators are down
python maf_generic_migrator_v1/examples/run_live_migration.py
```

Azurite (Blob/Queue/Table), Service Bus Emulator, and Cosmos DB Emulator ports are probed; missing services are soft-passed unless `LEGACY_MOD_REQUIRE_EMULATORS=1`.

## Repository layout

```
maf_generic_migrator_v1/platform_core/       # invariant platform (discovery, planner, chunker, MAF workflow, verifier, eval)
maf_generic_migrator_v1/adapters/            # language -> IR normalizers (python, node, ts, java, csharp, ...)
maf_generic_migrator_v1/recipe_backends/     # bridges to deterministic engines (LibCST, OpenRewrite, jscodeshift)
maf_generic_migrator_v1/cartridges/          # migration-pair plugins
maf_generic_migrator_v1/eval_harness/        # generic rubric-based scoring
maf_generic_migrator_v1/examples/      # demo fixtures + live-migration script
maf_generic_migrator_v1/tests/         # platform tests (conformance + smoke + integration)
maf_generic_migrator_v1/docs/          # architecture + cartridge authoring + MAF integration
aws_legacy/                            # source code being migrated (sample)
run_migration.py                       # top-level driver script
```

## Architecture

See `maf_generic_migrator_v1/docs/architecture.md`. Summary: outer MAF workflow (six invariant stages) + inner subagents per unit + deterministic-recipes-first, LLM-translators-only-for-residuals. Strangler-fig wave planning across cross-unit dependencies.

## Tests

```bash
pytest                        # 28 tests, ~1s, no credentials required
pytest -v                     # verbose
pytest maf_generic_migrator_v1/tests/integration/   # full-pipeline E2E using MockChatClient
```

## Status

- Platform core: complete.
- Cartridge `aws_lambda_polyglot_to_azure_fn_py`: complete; polyglot adapters + SAM/serverless IaC ingestion + 18 AWS→Azure service mappings + AWS SDK call resolver + 5 translators + reviewer/tester/security agents + 6-row rubric + LibCST recipe.
- MAF workflow: real `Workflow` with FileCheckpointStorage, fan-out per wave, reviewer accept/revise/reject retry loop (2 retries by default).
- Providers: OpenAI (default), Azure OpenAI, Azure Claude, Anthropic direct.

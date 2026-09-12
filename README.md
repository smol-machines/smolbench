# smolbench
Performance benchmarks for smolvm. Measures cold start, CPU, IO, and network
across host, CLI, and HTTP API paths.

## Quick start

```bash
# Run everything with defaults (5 iterations)
./smolbench/run_all.sh

# Specific suites
./smolbench/run_all.sh --suites cold_start,io

# More iterations for stable numbers
./smolbench/run_all.sh --iterations 20

# Custom output path
./smolbench/run_all.sh --output /tmp/bench.json

# Run a single suite directly
./smolbench/bench_io.sh 10
```

## Requirements

- bash 4+, python3, curl, wget, dd, nslookup
- `/dev/kvm` access (user must be in `kvm` group or root)
- smolvm binary (auto-detected from `target/release/` or `target/debug/`)

Optional: write access to `/proc/sys/vm/drop_caches` for accurate IO read
benchmarks (the scripts work without it, reads will just be cached).

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `SMOLVM` | auto-detect | Path to smolvm binary |
| `ITERATIONS` | `5` | Default iteration count |
| `BENCH_API_PORT` | `18099` | Port for the HTTP API server |

Use `SMOLVM` to bench a specific binary:

```bash
SMOLVM=/usr/local/bin/smolvm ./smolbench/run_all.sh
SMOLVM=$(which smolvm) ./smolbench/bench_cold_start.sh 20
```

## Suites

### cold_start

Measures VM startup latency: fork, kernel boot, init, agent ready.

| Test | What it measures |
|------|-----------------|
| `microvmStart` | Stop-to-start cycle |
| `microvmStartExec` | Start + first exec round-trip |
| `sandboxCreateStart` | Create + start from scratch |


### cpu

Measures compute and exec overhead. Host baseline shows the floor cost of
running a command locally vs through the VM.

| Test | Host | CLI/API |
|------|------|---------|
| `execRoundTrip` | `echo x` (50 iters) | Same, via vsock/HTTP |
| `singleCore` | — | 50k-iteration shell loop |
| `multiCore` | — | 4 parallel loops |

CPU loop baselines are skipped on host (KVM is near-native for compute).

### io

Measures disk throughput via `dd`. Host baselines show virtiofs overhead.

| Test | Unit | Description |
|------|------|-------------|
| `sequentialWriteMBps` | MB/s | 128 MiB write |
| `sequentialReadMBps` | MB/s | 128 MiB read (caches dropped) |
| `randomIo4kMs` | ms | 1000 x 4K file writes |

### network

Measures DNS and HTTP latency. Requires `--net` on the VM. Gracefully skips
VM tests if the guest has no connectivity (host baselines still run).

| Test | Unit | Description |
|------|------|-------------|
| `dnsResolutionMs` | ms | `nslookup example.com` |
| `httpRoundTripMs` | ms | `wget http://example.com` |

## Output

Each suite writes JSON to stdout (logs go to stderr). `run_all.sh` merges
suite outputs into a single report:

```json
{
  "system": { "cpu": "...", "arch": "...", "memoryMb": 16384, ... },
  "config": { "iterations": 5, "suites": ["cold_start", "cpu", "io", "network"] },
  "results": {
    "io": {
      "host": { "sequentialWriteMBps": { "avg": 2800, "min": 2600, ... }, ... },
      "cli":  { "sequentialWriteMBps": { "avg": 1400, ... }, ... },
      "api":  { "sequentialWriteMBps": { "avg": 1350, ... }, ... }
    },
    ...
  }
}
```

Each stat object contains: `min`, `max`, `avg`, `stdDev`, `median`, `samples`.

Reports are saved to `smolbench/results/` by default.

## File structure

```
smolbench/
  common.sh            # Shared utilities (timing, stats, server, cleanup)
  run_all.sh           # Orchestrator — runs suites, merges JSON
  bench_cold_start.sh  # VM startup latency
  bench_cpu.sh         # Compute + exec overhead
  bench_io.sh          # Disk throughput (dd)
  bench_network.sh     # DNS + HTTP latency
  results/             # Auto-generated reports
```
## Workload and branching benchmarks

This repository contains reproducible workload experiments for Smol's branchable machine runtime. It began as the Harbor integration, but now covers [Harbor](https://github.com/laude-institute/harbor), Aider, SWE-bench, Terminal-Bench, τ²-bench, BrowserGym, [Braintrust](https://github.com/braintrustdata/bash-agent-evals), CPU and memory density, cloud branching, and high-fanout lifecycle tests.

The Harbor provider itself ships in the `smolmachines` SDK. It prepares one running machine for a task image, then gives each trial an isolated copy-on-write branch with the same warm memory and filesystem state. This package keeps the older `harbor_smolvm:SmolvmEnvironment` import working and houses the public benchmark harness.

Open the [public scorecard](results/scorecard.html) for the consolidated results and links to every raw artifact. Run `./demo-public-suite.sh` for a shorter two-workload reproduction: it executes a verified Terminal-Bench task and Braintrust's pinned data application through equivalently prepared Smol and Docker environments, then writes one standalone HTML report.

## Pick a demo

| Workload | What the run proves | Fan-out | Smol versus Docker |
| --- | --- | ---: | ---: |
| Aider Polyglot | Ordinary Dockerfile-backed coding trials | 4 and 16 | **2.03× and 1.54× faster** |
| τ²-bench retail | Branch/evaluate/select over initialized tool state | 4 | **1.29× faster** |
| Terminal-Bench `regex-log` | Prepared shell environment plus official verifier | 4 | **1.68× faster** |
| SWE-bench Verified | Full Django issue patch and verifier | 4 | **1.08× faster** |
| Harbor Index GSO | Large NumPy artifact and isolated verifier on bare metal | 4 | Within 2% of Podman |
| BrowserGym MiniWoB | Fork a live browser into candidate actions | 4 | 1.91× slower |
| Braintrust `bash-agent-evals` | Branch an inherited live Node/SQLite worker | 4 | **2.58× faster than fresh Podman** |
| CPU and memory control | Same Python hashing/compression/JSON image | 16 | **1.05× faster, 6.12× lower memory pressure** |

## Compare Smol Cloud and Daytona on DeepSWE

The provider comparison runs two pinned public DeepSWE tasks through both hosted systems. It prepares one live VM per task, measures sequential and four-way branch paths, applies official oracle/no-op candidates, and grades every result in an independent official verifier environment. It also records finalized Smol utilization billing and a Daytona reserved-capacity estimate from the measured sandbox lifetimes.

```bash
# Inspect the exact plan without credentials or billable work.
./demo-deepswe-daytona.sh --dry-run

SMOL_CLOUD_TOKEN=... DAYTONA_API_KEY=... \
  ./demo-deepswe-daytona.sh --repetitions 3
```

See [the one-page comparison](docs/smol-vs-daytona.md) for the billing model, where branching applies to sequential rollouts, and the limits of the claim.

Most agent/eval rows are full steady-state lifecycle comparisons on the same 26-vCPU host. Harbor Index, Braintrust, and the CPU/memory control are identified separately on an eight-core bare-metal host. These are not claims that guest instructions run faster than native containers. The negative controls are kept on purpose: they show that branching helps when initialized state is material, and does not help when the whole task is already a few hundred milliseconds. Each section below contains the exact command, pinned workload identity, repetitions, correctness gate and raw validated report.

## Reproduce the Terminal-Bench demo

You need Python 3.12+, `uv`, KVM on Linux (or HVF on Apple Silicon), and enough memory for the requested concurrency. Add a working Docker daemon when you include the Docker baseline.

```bash
git clone https://github.com/smol-machines/smolbench.git
cd smolbench
uv sync --extra dev

# Fast lifecycle-only check: no model or verifier network traffic.
ATTEMPTS=16 CONCURRENCY=16 REPETITIONS=3 \
  ./demo-terminal-bench.sh --install-only
```

To compare the complete verified task with Harbor's default Docker provider:

```bash
PROVIDERS="smol-branch docker" ATTEMPTS=4 CONCURRENCY=4 REPETITIONS=3 \
  ./demo-terminal-bench.sh \
  --prepare-script bench/warmups/terminal_bench_verifier.sh
```

The harness downloads the public `terminal-bench-sample@2.0` dataset and runs its `regex-log` task unchanged. Every oracle trial must score `1.0`; a missing, errored, or lower reward makes the command fail. Provider order reverses between repetitions, checkpoint preparation is reported separately, and each run writes raw Harbor artifacts plus JSON, CSV, and a standalone HTML report.

## Current measured result

On a 26-vCPU Intel Xeon Platinum 8480+ host, four concurrent `regex-log` trials were run three times per provider:

| Path | Median wall time per 4-trial wave | Setup p50 | Verifier p50 | Correct |
| --- | ---: | ---: | ---: | ---: |
| Prepared Smol branches | 11.75 s | 0.80 s | 7.44 s | 12/12 |
| Equivalently prepared Harbor Docker | 19.72 s | 1.00 s | 4.25 s | 12/12 |

That is a **1.68× steady-state lifecycle speedup** for this task. Both environments ran the same preparation script: the live Smol checkpoint took 19.47 seconds to create and warm, while building the warmed Docker image took 10.49 seconds on a cold cache. Charging both costs across only three waves leaves Smol at **1.27×** using the median wave times.

The result is narrower than “VM execution is faster.” Docker's verifier finished 1.75× faster, while Smol reached the four isolated environments 1.25× faster and avoided Docker's long default teardown. This is a comparison of Harbor's user-visible lifecycle, including cleanup; it demonstrates faster repeated eval orchestration despite slower work inside each VM.

Without the preparation script, this task repeatedly downloads verifier dependencies inside every guest. In that deliberately cold shape, concurrent Smol verifier work was slower than Docker on this host. The warm checkpoint is the feature under test: perform repeatable setup once, then branch the initialized state for each clean trial.

For environment lifecycle alone, five four-way waves completed without errors at 3.55 seconds median for Smol versus 14.88 seconds for Harbor Docker. Actual setup p50 was much closer (0.87 versus 1.02 seconds); fast cleanup accounts for most of that full-lifecycle difference.

## Run a public Aider Polyglot task

This experiment downloads Harbor's public `aider/aider-polyglot` dataset and runs the ordinary Python `polyglot_python_simple-linked-list` task. The task only supplies a Dockerfile, so the harness builds it once and gives the exact same content-addressed image to Docker and Smol. It records the source-task hash, resolved image ID, software versions and raw Harbor results.

```bash
# Four isolated coding trials, repeated three times per provider.
./demo-aider-polyglot.sh

# Repeat the same comparison at 16-way fan-out.
ATTEMPTS=16 CONCURRENCY=16 ./demo-aider-polyglot.sh

# Optional real-agent compatibility run.
ANTHROPIC_API_KEY=... AGENT=claude-code MODEL=anthropic/claude-opus-4-1 \
  ATTEMPTS=1 CONCURRENCY=1 REPETITIONS=1 ./demo-aider-polyglot.sh
```

On the 26-vCPU reference host, all 120 scored outcomes matched exactly across the four- and 16-way runs. At N=4, prepared Smol branches completed a wave in 8.38 seconds median versus Docker's 16.98 seconds, a **2.03× full-lifecycle speedup**. At N=16, Smol took 12.90 seconds versus Docker's 19.93 seconds, or **1.54× faster**.

The reusable Smol machine took 23.6–23.8 seconds to create once and is reported outside every wave. Docker reached individual workers faster and ran the verifier faster at both scales; Smol won the user-visible Harbor lifecycle by reusing initialized state and avoiding repeated container teardown. The oracle deliberately removes model latency from this infrastructure comparison. Set `AGENT` and its model credentials to run another Harbor agent, but do not interpret shared inference latency as a sandbox speedup. See the [validated report](results/aider-polyglot.html).

## Run a package-optimization task from Harbor Index

This compatibility experiment runs [Harbor Index](https://github.com/harbor-framework/harbor-index)'s published `gso-speedup-numpy-strings` task. It starts from the real 3.9 GB NumPy development image, collects the 93 MB source artifact, hands it to a separate verifier image, rebuilds NumPy and runs the benchmark suite. Both OCI images are pulled before timing, then a temporary task copy pins Docker and Smol to the same repository digests; the downloaded source task remains untouched.

```bash
# Infrastructure-only control: unchanged source should complete and score 0.
./demo-harbor-index.sh

# Let a real agent optimize the task; optionally require a positive speedup.
ANTHROPIC_API_KEY=... AGENT=claude-code MODEL=anthropic/claude-opus-4-1 \
  MINIMUM_REWARD=1 ./demo-harbor-index.sh
```

The default `nop` agent is deliberate: it makes the large-image, artifact-transfer and isolated-verifier path deterministic without presenting model quality as infrastructure performance. A successful control must finish every trial with no runtime errors and a reward of `0.0`; a positive score is only expected when an agent changes NumPy correctly. The report separates environment setup, artifact-to-verifier handoff and verifier execution so a lifecycle improvement cannot hide slower guest compute.

On an eight-core Intel i7-9700 bare-metal host, all 24 provider trials completed without runtime errors across three four-way waves. The comparison used rootless Podman 6.1 through Harbor's Docker-compatible provider. Smol's median full wave was 53.20 seconds versus Podman's 52.17 seconds, putting the two within **2% end to end**. Smol handed the large task artifact to the verifier in 12.63 seconds p50 versus Podman's 25.99 seconds, while CPU-heavy verifier execution remained slower at 34.92 versus 19.47 seconds p50.

Before local archive and file work moved off Harbor's event loop, the same matched Smol path took 73.37 seconds. The fix in [smol#153](https://github.com/smol-machines/smol/pull/153) reduced that wave by 27% and removed the concurrency staircase without changing the workload or artifact. The result is lifecycle parity, not a claim that guest CPU execution is faster. See the [validated phase report](results/harbor-index-gso-n4.html).

## Prove branch semantics locally

For a short demo on either Linux or Apple Silicon, branch one running Alpine machine four ways and verify inherited RAM, inherited disk state and isolated child writes:

```bash
./demo-branch-state.sh
```

Across three strict waves on an eight-core M1 Pro running macOS 26.5, all 12 branches passed and four-way branch creation took 0.634 seconds median. A missing inherited file or cross-child write fails the command, and the cleanup trap removes every test machine.

To exercise the same lifecycle through Smol Cloud—including a continued source, batch fan-out, a branch of a branch, state divergence, and bottom-up cleanup—run:

```bash
SMOL_CLOUD_TOKEN=... SMOL_CLOUD_URL=https://api.smolmachines.com \
  ./demo-cloud-branch-state.sh
```

The command works against the hosted service or a locally deployed SmolCloud endpoint and fails on any inherited RAM/disk mismatch, cross-branch state leak, frozen source, nested-branch error, or cleanup failure.

Against SmolCloud main at `bdfac6b0` and SmolVM main at `78e5250f`, three four-way waves passed 36/36 lifecycle checks. The median cloud SDK call to create four ready branches was 1.179 seconds; creating a branchable child and then its grandchild took 1.199 seconds. Both sources remained running and independently writable. See the [validated cloud result](results/cloud-branch-state.json).

## Measure CPU parity and physical memory

This control runs one content-addressed Python image through both providers. It initializes 256 MiB of immutable state, then performs hashing, zlib compression, JSON parsing and regex work with a unique input in every worker. Smol initializes the state once before branching; each native container must initialize its own process. Every digest and checksum must match across providers.

```bash
./demo-cpu-density.sh --container-runtime podman
```

On an eight-core Intel i7-9700, five alternating N=16 repetitions produced:

| Path | Median 16-worker wave | Work p50 | Host-memory pressure | Incremental memory/worker |
| --- | ---: | ---: | ---: | ---: |
| Smol branches | 1.440 s | 528.9 ms | 669.5 MiB | 11.9 MiB |
| Rootless Podman | 1.513 s | 316.9 ms | 4,099.1 MiB | 256.2 MiB |

Smol completed the full wave **1.05× faster** while applying **6.12× less physical-memory pressure**. Its guest work remained 1.67× slower under 2× CPU oversubscription; shared initialization and concurrent branch admission recovered that difference at the end-use-case boundary. The one-time source preparation took 1.97 seconds outside the wave, its idle VMM consumed 0.0% CPU, and the median source-visible checkpoint/resume window was 36 ms. The measured runtime tree is released on SmolVM main as `78e5250f`.

The harness alternates provider order, takes nine samples per host-memory observation, keeps all workers alive during measurement and records every raw result. `MemAvailable` includes the retained source and kernel/runtime costs; process RSS and PSS are deliberately excluded because they miss resident memfd snapshot pages with no current process mapping. At low fan-out the fixed source can outweigh sharing, so the report publishes total and incremental memory separately. See the [validated report](results/cpu-density.html).

## Soak high fan-out

The scale demo repeatedly branches the public `regex-log` environment at N=16, N=32 and N=64, then runs a matched N=16 Harbor Docker control. Add `BOUNDARY=128` for a single larger probe.

```bash
SMOLVM_REVISION="$(git -C /path/to/smolvm rev-parse HEAD)" \
  BOUNDARY=128 ./demo-scale-soak.sh
```

On the same 26-vCPU Linux 6.8 host, current SmolVM main at `8a571dc` completed 80/80 environments at N=16, 96/96 at N=32 and 192/192 at N=64, with zero runtime errors. One N=128 probe also passed 128/128. A separate three-wave N=128 qualification then failed its third wave: 32 trials inherited one failed transactional batch after a clone hit the [upstream KVM first-run `ENOMEM` defect](https://github.com/torvalds/linux/commit/916b7f42b3b3b539a71c204a9b49fdc4ca92cd82) twice. All temporary machines were cleaned up. N=64 is therefore the repeated clean ceiling on this unpatched host; N=128 is not production-qualified.

The repeated N=16 Smol lifecycle took 5.14 seconds per wave versus 17.07 seconds for Harbor Docker, or **3.32× faster end to end**. Docker reached each environment about 1.90× faster; Smol's advantage came from the complete copy-on-write lifecycle and cleanup, not faster guest setup. The report treats host `MemAvailable` deltas as approximate observations only.

## Run Braintrust's public data-agent workload

The second experiment uses Braintrust's real `bash-agent-evals` application at a pinned commit and a digest-pinned Node base image. It downloads and transforms the project's 958 MB GH Archive corpus, installs its TypeScript dependencies and native SQLite module, checkpoints that initialized state, and gives different eval questions to independent branches. The optional Docker control uses the same prepared application and the same two-CPU, 4 GiB runtime limit.

```bash
# No model key: compare an inherited live worker with fresh and prewarmed Docker.
uv run python bench/braintrust_warm_fanout.py \
  --fanout 4 --parallel 4 --repetitions 5

# Run the repository's ordinary SQL agent unchanged.
ANTHROPIC_API_KEY=... uv run python bench/braintrust_fanout.py \
  --fanout 4 --parallel 4 --mode agent --agent sql \
  --model claude-sonnet-4-5
```

On an eight-core i7-9700 bare-metal host (`systemd-detect-virt: none`), five four-way repetitions produced 80/80 correct outputs across direct Smol branches, retained Smol slots, fresh rootless Podman containers, and prewarmed Podman workers. Direct Smol branch-to-result latency was 1.651 seconds median versus 0.327 seconds for fresh Podman, so the container control was **5.06× faster**. The first direct branch completed in 0.543 seconds; later direct branches recaptured the continuing source and took 1.632–1.763 seconds.

The low-latency path is a retained branch slot: release-to-result took 0.126 seconds median, **2.58× faster than a fresh Podman container**, but still 2.05× slower than a prewarmed Podman worker at 0.062 seconds. The first post-branch query took 12–52 ms and its immediate repeat took 2–37 ms, while fresh Podman queries took 2–37 ms. This workload is therefore a useful negative control for direct branching and a positive result only when a retained slot replaces fresh container creation. The host-memory counters were too sensitive to page-cache state to support a density claim in this run. See the [bare-metal five-run artifact](results/braintrust-warm-baremetal-i9700-5x4.json).

Use `--keep-checkpoint` to retain the expensive prepared state, then pass its printed name through `--checkpoint NAME` for later fan-out waves.

## Run a real SWE-bench Verified issue

The third experiment downloads Harbor's 500-task SWE-bench Verified package and selects `django__django-10999`, a real Django issue. It applies the published oracle patch independently in every environment and runs the official issue-specific test and grading path. The task contents, source-tree hash, and public SWE-bench image digest are recorded rather than replaced with a toy fixture.

```bash
uv run python bench/swebench_verified.py \
  --fanout 4 --parallel 4 --repetitions 3
```

On the same 26-vCPU host, Smol completed each four-trial wave in 24.93 seconds median versus Docker's 27.03 seconds, a modest **1.08× lifecycle speedup**. Both paths passed 12/12 trials at reward `1.0`. Smol setup was 0.946 versus 1.023 seconds, while Docker's actual verifier was much faster at 11.38 versus 19.30 seconds. This is best read as end-to-end compatibility and approximate lifecycle parity on a recognizable coding-agent task—not as faster VM compute. The selected SWE-bench image is x86-64, so this demo currently requires an x86-64 Linux host.

## Branch and hill-climb candidate fixes

The hill-climb demo makes the SWE-bench workflow concrete. It prepares the same Django issue once, branches four isolated repositories, applies four competing fixes, and runs the official verifier in parallel. Only the published resolved patch should pass; the original issue suggestion, a partial fix and the unchanged base must fail for the run to count.

```bash
./demo-swebench-hillclimb.sh --repetitions 3
```

Against SmolVM main at `8a571dc`, all 24 provider/candidate outcomes matched exactly across three repetitions. Smol created each four-branch wave in 0.397 seconds median and scored it in 11.756 seconds; Docker created and scored the same four candidates in 4.667 seconds. Docker is 2.60× faster end to end on this CPU-bound verifier. The result demonstrates a reproducible branch/evaluate/select primitive over a real coding task, while also showing that current Smol guest execution—not environment fan-out—is the bottleneck.

## Branch a running browser

The visual demo starts Chromium and a stateful Playwright service once, checkpoints the running processes and page, then branches four independent browsers. Each branch enters a different value, clicks the page and captures a screenshot. The exact-state check requires every browser to begin with an action count of zero.

```bash
./demo-live-browser.sh --fanout 4 --parallel 4 --repetitions 3
```

On the 26-vCPU host, 12/12 branched browser actions passed against SmolVM main at `8a571dc`. Four-way branch creation took 0.656 seconds median and all four first actions finished in 1.692 seconds. Four fresh Docker browsers finished in 0.842 seconds, making Smol 2.79× slower end to end on this tiny page. The profile attributes the remaining cost to first-touch execution inside restored Chromium, not fan-out: the demo proves that live browser state branches correctly, but it does not claim a speed advantage for trivial browser startup. The generated standalone HTML report contains the screenshots for recording or sharing.

## Search a real BrowserGym task

This demo uses [BrowserGym MiniWoB](https://github.com/ServiceNow/BrowserGym), not a purpose-built page. It initializes the pinned `click-test` environment and Chromium once, then branches the live state into four candidate futures. Each branch calls the ordinary BrowserGym `env.step(...)` API with a correct click, no-op, scroll or wrong click. The report checks the common initial screenshot, pristine action counter, exact reward and terminal state, then selects the rewarded branch.

```bash
./demo-browsergym-branch.sh
```

This source-continuation check requires the SDK native extension and boot helper to be built from the same SmolVM revision. The validated run used `8a571dc`; the harness fails rather than silently accepting the frozen-source behavior in the 1.13.1 SDK wheel.

On the 26-vCPU host, three waves produced the expected 24/24 Smol and Docker outcomes with no leaked machines or containers. Four-way Smol branching took 1.715 seconds median and the candidate actions took 4.096 seconds, versus 3.044 seconds for four fresh prepared Docker containers. Smol was **1.91× slower end to end** on this small task because live source continuation and restored Chromium's first action cost more than starting the prepared containers. Every wave also proved that the original BrowserGym source remained unchanged and responsive after its children ran. The value demonstrated here is exact live-state search through a recognizable agent environment; it is not presented as a throughput win. The [validated visual report](results/browsergym-branch-search.html) includes all four final browser states.

## Search a real stateful tool-agent task

The τ²-bench demo uses Sierra's current retail benchmark at a pinned source revision. It loads retail task 33 and its database into one running Python environment, then branches four candidate decisions: update the customer's profile to the correct Seattle address, update it to the wrong city, cancel the pending order, or make no change. Every branch uses τ²-bench's ordinary tool interface and official database evaluator; exactly one must receive reward `1.0`.

```bash
./demo-tau2-branch.sh
```

The Docker control starts four containers from an equivalently prepared, digest-pinned image. It uses the same τ²-bench revision, task, candidate calls, two-CPU/2 GiB limits and disabled execution-time network. Model inference is deliberately outside this measurement because it costs the same for either sandbox provider. The experiment isolates whether a live, already-initialized agent environment is a useful branch point for testing state-mutating decisions.

The command alternates provider order over three waves, checks every initial and final database hash, verifies expected city and order state, proves the source remains unchanged and responsive, rejects partial outcomes, and writes a standalone visual report.

On the 26-vCPU reference host, all 24 Smol and Docker outcomes matched the official evaluator across three waves. Smol's median four-way branch took 1.285 seconds and all four evaluations took 1.601 seconds, for 2.885 seconds end to end. Four fresh prepared Docker containers took 3.718 seconds, making Smol **1.29× faster** in steady state. The warm runtime is the advantage: actual evaluator work remained slower after restore (0.800 versus 0.162 seconds p50), but avoiding 2.772 seconds of Python and benchmark initialization in every container more than recovered that cost. The one-time Smol environment preparation took 39.73 seconds and is reported separately. See the [validated visual report](results/tau2-branch-search.html).

To run the complete official conversation with a real agent and user model inside a Smol branch, point the optional model demo at any OpenAI-compatible endpoint that the guest can reach:

```bash
OPENAI_API_BASE=https://your-model-endpoint/v1 \
OPENAI_API_KEY=... \
AGENT_MODEL=openai/gpt-4.1 \
./demo-tau2-agent.sh
```

This compatibility run records the official reward, database match, compact tool-call timeline, branch latency, and whether the live source stayed pristine. The command distinguishes a functioning Smol runtime from the model's task score; set `REQUIRE_REWARD=1` when a failed model answer should also fail the command. It does not compare model latency with Docker: both providers can use the same external inference endpoint, so the deterministic four-candidate experiment above remains the controlled infrastructure comparison.

The end-to-end path was validated with `Qwen/Qwen2.5-32B-Instruct-AWQ` at revision `5c7cb76a268f`: the model conversed, called the official retail tools and reached the evaluator inside a branch while the source stayed live and unchanged. Its task reward varied between `1.0` and `0.0` across temperature-zero trials, so the model run is compatibility evidence, not a sandbox performance or model-quality claim.

## Compatibility proven beyond the headline demo

- `sqlite-with-gcov`: 8/8 trials passed across branched and cold machines. Each trial installed build tools, unpacked SQLite, configured coverage instrumentation, compiled in parallel, and ran the Python verifier. Branch readiness was 24.4× faster (0.192 versus 4.684 seconds p50), but network and compilation variance were too large for an honest full-runtime claim.
- `configure-git-webserver`: 2/2 concurrent branches passed after installing and starting SSH and nginx, creating users, initializing a bare Git repository, and exercising deployment hooks. This verifies that branches are not limited to shell snippets; independent long-running service state works.

## Use the provider directly

```bash
pip install "smolmachines[harbor]"

harbor run --path /path/to/task --agent oracle \
  --env smol.harbor:SmolEnvironment \
  --n-attempts 16 --n-concurrent 16
```

The same provider accepts `--ek target=cloud` with Smol Cloud credentials. Set `auto_checkpoint=false` to create one cold machine per trial, or pass a prepared machine through the provider's `checkpoints` map when setup must happen before the benchmark starts.

## What the harness records

- Full wall time and Harbor job time.
- Environment setup, agent, and verifier p50/p95/p99.
- One-time checkpoint preparation.
- Trial errors and verifier rewards.
- Approximate host-memory pressure, clearly labeled as a `MemAvailable` delta rather than per-machine RSS.
- Host, Python, Harbor, and SDK versions.

Raw jobs and ad hoc results stay untracked. `bench/render_results.py` turns any result JSON into a self-contained report suitable for a browser or screen recording.

## Honest boundaries

- Local Dockerfile-backed Harbor tasks are built and cached automatically; cloud runs still need a published OCI image. Docker Compose tasks are not yet supported by the Smol provider.
- Setup-heavy tasks only benefit when the useful prepared state is inside the checkpoint. Repeating package downloads after every branch can dominate the entire run.
- One public `build-cython-ext` sample currently scores `0.0` from both cold and branched Smol machines because the same upstream `pyknotid` repository test fails in each. The harness caught this dependency/test drift and excludes it from performance claims.
- Current main at `8a571dc` passed repeated prepared-branch waves through N=64 and one N=128 probe on this Linux 6.8 host, but a later three-wave N=128 qualification failed 32 trials in its third wave after an affected KVM clone exhausted the bounded retry. Fork-heavy production hosts need a kernel containing upstream fix `916b7f4`; the harness rejects the partial wave rather than publishing it as a pass.
- An early multi-threaded browser worker took another 3.4–3.5 seconds to wake its pre-checkpoint work loop after restore with both condition-variable and pipe controls. A normal single-threaded HTTP/Playwright event loop removed that delay; resumed blocked-thread latency remains an open runtime edge case and is not hidden in the published browser numbers.

## Development

```bash
uv run ruff format --check bench src tests
uv run ruff check bench src tests
uv run pytest -q
```

Apache-2.0.

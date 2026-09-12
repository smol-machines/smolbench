# Smol Cloud and Daytona for coding-agent rollouts

The short version: both products now support live copy-on-write VM forks. Smol's distinct bet is that a running sandbox should be billed for the CPU, resident memory, and disk it actually uses, and that one warm state should fan out transactionally in a single batch request.

## Billing

Daytona documents billing started sandboxes from their **reserved** vCPU, RAM, and disk. Smol charges a $0.04 running-machine base plus **active** CPU, resident memory, used disk, and egress.

For the 2-vCPU, 8-GiB shape used by the two default DeepSWE tasks, Daytona's public CPU and RAM rates model to $0.2304/hour before disk. Smol's CPU and RAM portion changes with the workload:

| Observed Smol usage | Modeled Smol/hour | Difference from Daytona reserved CPU + RAM |
| --- | ---: | ---: |
| 0.2 active vCPU + 2 GiB RSS | $0.0824 | 64% lower |
| 0.5 active vCPU + 4 GiB RSS | $0.1298 | 44% lower |
| 2 active vCPU + 8 GiB RSS | $0.2696 | 17% higher |

These are scenarios, not measured DeepSWE results. The benchmark records Smol's finalized per-machine meter and models Daytona from measured sandbox lifetime and its published rates, so the resulting report replaces scenarios with observed data. Daytona snapshot storage, credits, and network egress are excluded and identified as such in the artifact.

## Branch compute

Both systems can fork a running Linux VM with memory and filesystem state, retain lineage, and fork a child again. The practical differences under test are:

| | Smol Cloud | Daytona |
| --- | --- | --- |
| Fan-out API | One transactional batch request; all children or none | One fork at a time per parent; the benchmark uses Daytona's documented concurrent tree fan-out by default |
| Billing while an agent waits on a model | Active CPU and resident memory meters fall with use | Reserved vCPU and RAM continue billing while started |
| Local and self-hosted path | Same Smol engine and SDK locally, hosted, or self-hosted | Hosted Daytona API and runner ecosystem |
| Persistent artifacts | Portable `.smolmachine` and live `.smolcheckpoint` formats | Snapshots, hot VM snapshots, and warm pools |

Daytona is the more mature managed sandbox surface today: it has warm pools, broad language SDKs, snapshot management, and published rollout guides. This comparison should not erase those strengths.

## Does branching apply to sequential DeepSWE rollouts?

Yes for setup, not automatically for every turn. A sequential rollout still starts each attempt from a prepared repository, dependency cache, tools, and services. Branching that state removes repeated setup. Branching again in the middle of a trajectory only matters when the workload retries from a checkpoint, evaluates multiple candidate actions, or launches subagents.

The default experiment therefore runs two shapes:

- Fan-out 1: one sequential attempt from a prepared state.
- Fan-out 4: four candidate attempts and four isolated official verifiers from the same state.

It uses the official DeepSWE images, solution patches, and separate verifiers. Oracle and no-op candidates must receive the expected 1 and 0 rewards. Model calls are excluded so model latency and model quality cannot hide an infrastructure difference.

The preflight was validated on 2026-09-12 with Podman 6.1 using both pinned images: FastAPI oracle/no-op scored `1/0`, and wasmi oracle/no-op scored `1/0`, each through a pristine separate official verifier. This validates the workload packaging and correctness gate, not hosted-provider performance. No Smol-vs-Daytona numbers are published until both hosted runs complete.

## Reproduce

```bash
git clone https://github.com/smol-machines/smolbench
cd smolbench

# See the exact images, resource reservations, and sandbox count without credentials.
./demo-deepswe-daytona.sh --dry-run

export SMOL_CLOUD_TOKEN=...
export DAYTONA_API_KEY=...

# Quick two-task, one-repetition comparison.
./demo-deepswe-daytona.sh

# Publication-quality run.
./demo-deepswe-daytona.sh --repetitions 3

# Optional diagnostic: compare Daytona's serial single-parent path.
./demo-deepswe-daytona.sh --daytona-fanout serial
```

The command writes raw JSON and a standalone one-page HTML report under `results/`. A missing trial, unexpected reward, provider error, or cleanup error remains visible in the artifact; an incorrect reward stops the run.

If an existing scheduler is hard to replace, use the [shadow-lane onboarding plan](daytona-ablation-onboarding.md) to keep its round-robin, retry, and fault-tolerance behavior while changing only the sandbox adapter for a fixed task cohort.

Sources: [Smol pricing](https://smolmachines.com/pricing), [Daytona billing](https://www.daytona.io/docs/en/billing/), [Daytona pricing](https://www.daytona.io/), [Daytona forks](https://www.daytona.io/docs/sandboxes), and [DeepSWE](https://github.com/datacurve-ai/deep-swe).

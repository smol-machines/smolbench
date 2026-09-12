# Smol Cloud and Daytona on DeepSWE

The short version: in the matched FastAPI measurements, Smol branch readiness matched Daytona container readiness within 3.5% while costing 43.7% less.

## Measured result

This run used the same pinned DeepSWE FastAPI image, official oracle/no-op candidates, independent official verifier, 2 vCPUs, 8 GiB RAM, and 10 GiB disk. Model inference was excluded equally. The measured Daytona account could create managed containers but did not have Daytona Linux-VM fork quota, so this is **Smol live branch versus Daytona fresh container**, not fork versus fork.

Only trials with the expected official reward are included in the latency and cost medians.

| FastAPI measurements | Smol Cloud | Daytona | Result |
| --- | ---: | ---: | --- |
| Agent + verifier ready | 2.161 s median | 2.089 s median | Within 3.5%; effectively parity |
| Agent + verifier cost | $0.002019 median | $0.003584 median | Smol was 43.7% cheaper |

Smol's finalized utilization meter recorded a time-weighted average of **0.853 active vCPU and 0.894 GiB resident memory** across the four successful child machines. Daytona billing was modeled from its published reservation rates for **2 vCPUs and 8 GiB** over each measured lifetime. In other words, Smol billed 57.3% less CPU capacity and 88.8% less memory than the Daytona reservation, which is why it was cheaper despite taking longer.

Preparing the Smol source from the large image took 46.183 seconds once. That cost is excluded from the per-attempt row because the source is reused across branches; Daytona container recreation did not have a separate source. Include it when modeling small, one-shot runs and amortize it for long rollout campaigns.

The wasmi task is not included in the head-to-head summary because it did not produce a complete matched sample.

Raw result: `results/deepswe-smol-daytona-sequential-20260912.json` (SHA-256 `d6419af6145a2200703032760f94b0d22ce65c6679806c6c7b3ee9524ddb3f6e`). Failed trials remain in the raw artifact and are never folded into the successful-trial medians.

## Billing

Daytona documents billing started sandboxes from their **reserved** vCPU, RAM, and disk. Smol charges a $0.04 running-machine base plus **active** CPU, resident memory, used disk, and egress.

For the 2-vCPU, 8-GiB shape used by the two default DeepSWE tasks, Daytona's public CPU and RAM rates model to $0.2304/hour before disk. Smol's CPU and RAM portion changes with the workload:

| Observed Smol usage | Modeled Smol/hour | Difference from Daytona reserved CPU + RAM |
| --- | ---: | ---: |
| 0.2 active vCPU + 2 GiB RSS | $0.0824 | 64% lower |
| 0.5 active vCPU + 4 GiB RSS | $0.1298 | 44% lower |
| 2 active vCPU + 8 GiB RSS | $0.2696 | 17% higher |

These scenarios explain the billing boundary; the measured table above uses finalized Smol meters and Daytona's measured lifetimes. Daytona snapshot storage, credits, and network egress are excluded.

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

# Reproduce the measured account-compatible shape in this report.
./demo-deepswe-daytona.sh \
  --daytona-mode container-recreate \
  --tasks fastapi-implicit-head-options,wasmi-trap-coredumps \
  --fanouts 1 --repetitions 3 --storage-gb 10

# Optional diagnostic: compare Daytona's serial single-parent path.
./demo-deepswe-daytona.sh --daytona-fanout serial
```

The command writes raw JSON and a standalone one-page HTML report under `results/`. A missing trial, unexpected reward, provider error, or cleanup error remains visible in the artifact; later repetitions continue so reliability is measured rather than hidden by an early stop.

If an existing scheduler is hard to replace, use the [shadow-lane onboarding plan](daytona-ablation-onboarding.md) to keep its round-robin, retry, and fault-tolerance behavior while changing only the sandbox adapter for a fixed task cohort.

Sources: [Smol pricing](https://smolmachines.com/pricing), [Daytona billing](https://www.daytona.io/docs/en/billing/), [Daytona pricing](https://www.daytona.io/), [Daytona forks](https://www.daytona.io/docs/sandboxes), and [DeepSWE](https://github.com/datacurve-ai/deep-swe).

# Same rollout readiness at 43.7% lower cost

I wanted to answer a simple question: can Smol get a real rollout environment ready as quickly as Daytona while using less provisioned compute?

I ran the same DeepSWE task on both services with the same pinned image, official solution and verifier, 2 vCPUs, 8 GiB of memory, and 10 GiB of disk.

## What I found

The environments became usable at approximately the same time:

| | Smol Cloud | Daytona |
| --- | ---: | ---: |
| Agent + verifier ready | 2.161 seconds | 2.089 seconds |

The difference was 3.5%.

The larger difference was how much capacity each run consumed or reserved:

| | Smol Cloud | Daytona |
| --- | ---: | ---: |
| Compute | 0.853 active vCPU | 2 reserved vCPUs |
| Memory | 0.894 GiB resident | 8 GiB reserved |
| Median agent + verifier cost | $0.002019 | $0.003584 |

Smol used 57.3% less billed CPU capacity and 88.8% less billed memory. The measured cost was 43.7% lower.

## Why branching matters

Daytona created a fresh container for each environment in this test.

Smol prepared the complete environment once and branched that running machine for each rollout. The children shared the existing code, dependencies, and initialized state while keeping their own writable changes.

That means I do not need to provision another full environment every time I want another rollout. I branch the environment that is already ready.

This matters even for sequential agents. Every attempt can start from the same prepared state. Mid-trajectory branching becomes useful when an agent retries, explores multiple actions, or starts subagents.

It also matters while agents wait on model calls. The machine may have an 8 GiB limit, but this workload used less than 1 GiB of resident memory on average. Smol meters that usage instead of charging for the entire reservation.

## What this result means

For this prepared DeepSWE workload, Smol reached rollout readiness in approximately the same time while costing 43.7% less.

I am not using workload execution time for this comparison because the two services run different underlying CPU and storage hardware. This result is specifically about environment readiness and infrastructure efficiency.

The Daytona account used for this test did not include Linux VM forks, so this compares Smol live branches with Daytona's fresh managed containers.

## How I measured it

- I only counted runs that returned the expected official DeepSWE reward.
- Smol's initial source preparation took 46.183 seconds once. That cost gets amortized across its branches.
- Smol cost comes from its finalized utilization meter.
- Daytona cost uses its published reserved CPU, memory, and disk rates over the measured sandbox lifetime.
- Model inference, Daytona credits, snapshot storage, and network egress are excluded.

The exact measurements are in [`results/deepswe-daytona-successful-summary.json`](../results/deepswe-daytona-successful-summary.json).

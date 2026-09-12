# Smol Cloud vs Daytona: prepared rollout efficiency

We ran a matched DeepSWE environment on Smol Cloud and Daytona using the same pinned image, official solution, independent verifier, 2-vCPU allocation, 8 GiB memory allocation, and 10 GiB disk.

## Result

Smol and Daytona reached usable agent and verifier sandboxes in approximately the same time:

| | Smol Cloud | Daytona |
| --- | ---: | ---: |
| Agent + verifier ready | 2.161 seconds | 2.089 seconds |
| Difference | +3.5% | baseline |

Smol used materially less billable capacity during the successful trials:

| | Smol Cloud | Daytona |
| --- | ---: | ---: |
| Compute | 0.853 active vCPU | 2 reserved vCPUs |
| Memory | 0.894 GiB resident | 8 GiB reserved |
| Median agent + verifier cost | $0.002019 | $0.003584 |
| Cost reduction | 43.7% | baseline |

## Why

Smol prepares one complete environment and branches its running state for each rollout. Code, dependencies, and initialized state remain shared until a child modifies them, while every child receives isolated writable state.

This changes the unit of infrastructure from “provision another full sandbox” to “branch the state that is already ready.” It is especially useful when rollouts spend time waiting for model responses or use much less CPU and memory than their reserved limits.

## What this result supports

For this prepared DeepSWE workload, Smol delivered approximately equal sandbox readiness while reducing measured agent-and-verifier cost by 43.7%. Its successful children averaged 57.3% less CPU capacity and 88.8% less memory than Daytona's reservation.

The workload execution times are not used for this efficiency claim because the two hosted services run different underlying CPU and storage hardware. This run compared Smol live branches with Daytona fresh managed containers; the measured Daytona account did not have Linux-VM fork access.

## Method and limitations

- Latency and cost medians include only trials that produced the expected official DeepSWE reward.
- Smol completed 2 of 3 FastAPI trials; Daytona completed 3 of 3. Hosted reliability needs further work.
- Smol's 46.183-second source preparation happens once and must be amortized across branches.
- Smol cost comes from its finalized utilization meter.
- Daytona cost is modeled from its published reserved CPU, memory, and disk rates over each measured sandbox lifetime.
- Model inference, Daytona credits, snapshot storage, and network egress are excluded.

The exact aggregate and per-trial measurements are in [`results/deepswe-daytona-successful-summary.json`](../results/deepswe-daytona-successful-summary.json).

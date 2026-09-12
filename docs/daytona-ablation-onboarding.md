# Run a Smol/Daytona ablation without replacing your scheduler

Keep the current round-robin, retry, and fault-tolerance logic. Change only the provider adapter for a fixed cohort of tasks.

## Weekend-sized test

1. Pick two pinned DeepSWE task IDs and keep their image, vCPU, memory, disk, timeout, candidate patch, and verifier identical.
2. Assign attempts deterministically with `hash(task_id + attempt) % 2`; do not route based on availability or retry outcome.
3. Prepare one source sandbox per task and provider before timing the attempts.
4. Branch one candidate and one independent verifier for every attempt.
5. Keep the existing retry policy, but record every initial provider error before retrying.
6. Delete every child with usage reporting enabled, then export the raw result rather than a screenshot alone.

The included comparison does this directly:

```bash
git clone https://github.com/smol-machines/smolbench
cd smolbench

./demo-deepswe-daytona.sh --dry-run

export SMOL_CLOUD_TOKEN=...
export DAYTONA_API_KEY=...
./demo-deepswe-daytona.sh --repetitions 3
```

It writes `results/deepswe-daytona.json` and a standalone HTML one-pager. The JSON contains source preparation, branch-to-ready time, candidate and verifier time, expected and observed reward, cleanup failures, and billing inputs.

## Integration boundary

An existing scheduler only needs these operations from either provider:

```text
prepare_source(task) -> source
branch(source, attempt_id) -> sandbox
exec/upload/download(sandbox)
delete(sandbox) -> usage
```

Use idempotent attempt IDs and retain the provider name on every attempt. Do not move scheduling, model calls, reward logic, or retry decisions into the provider adapter.

## Decision gates

- Correctness: every oracle receives `1`; every no-op receives `0`.
- Reliability: no missing attempts or leaked sandboxes after cleanup.
- Speed: compare branch-to-ready and infrastructure-only end-to-end time separately from model latency.
- Cost: compare finalized Smol utilization meters with Daytona's reserved resource-seconds using the public list rates embedded in the artifact.
- Rollout fit: count how often a task can branch after setup, at a retry point, or for candidate/subagent search. Do not claim mid-trajectory value for strictly sequential rollouts that never revisit state.

Start with this shadow lane. A provider does not need to take over the production scheduler for the result to be representative.

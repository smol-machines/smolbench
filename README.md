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

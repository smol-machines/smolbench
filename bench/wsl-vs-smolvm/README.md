# WSL vs smolvm, same box, same guest

Reproduces the workloads from [WSL2 vs WSL3 benchmarks](https://tonym.us/wsl2-vs-wsl3-benchmarks.html):
`perf bench syscall/basic`, `sched/pipe`, `sched/messaging` (20 groups, 800 tasks), `mem/memcpy` (1 GB),
and optionally a cold `go build` of GoReleaser, inside an Alpine guest pinned to 2 vCPU and 4 GB, once
under WSL and once under smolvm on the Windows Hypervisor Platform.

`guest.sh` is the only thing that runs inside either guest, so the two sides differ only in what boots it.
`fs.sh` adds file I/O: untar, find, stat, read and delete 2000 small files, then a 1 GB write (with
fsync) and read, once on a Windows folder shared into the guest (WSL: 9p under `/mnt/c`; smolvm: a
`-v` virtiofs mount) and once on the guest's own disk. `run.ps1` alternates WSL and smolvm runs, takes
the median, and writes `results.csv`.

```powershell
.\run.ps1                       # perf workloads, 3 runs each
.\run.ps1 -Full -Fs -Runs 3     # adds the GoReleaser cold build and the file I/O suite
```

smolvm pulls Alpine from `mirror.gcr.io`, because back-to-back ephemeral runs hit Docker Hub's
anonymous pull limit.

Use smolvm 1.27.0 or newer; older releases miss the Windows fixes and land well behind WSL2.
Results from a Ryzen AI Max+ 395 laptop are under `results/`; `z13-2026-10-11` is the 1.27.0 release.

Setup on the Windows side: an Alpine WSL distro (import the minirootfs with `wsl --import`), a
`.wslconfig` with `processors=2` and `memory=4GB` followed by `wsl --shutdown`, and `smolvm.exe` on
PATH with the Windows Hypervisor Platform feature enabled. Report the WSL version (`wsl --version`),
the smolvm version, the CPU, and whether VBS is on; the post's box was an i5-8500T with VBS active.

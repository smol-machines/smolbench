# ASUS ROG Flow Z13, 2026-10-11, smolvm 1.27.0

AMD Ryzen AI MAX+ 395 (Zen 5), Windows 11 build 26200, VBS on, AC power, Balanced plan.
WSL 2.7.10 (kernel 6.18.33), Alpine 3.23 distro, `.wslconfig` processors=2 memory=4GB. smolvm 1.27.0 release build (guest kernel 6.18.55), 2 vCPU / 4 GB.
`smolvm-1.27.0.csv` is one `.\run.ps1 -Distro AlpineBench -Full -Fs -Runs 3` from a clean copy of this kit.

| workload | WSL2 | smolvm 1.27.0 |
|---|---|---|
| getppid syscalls | 8.26M/s | 31.7M/s |
| pipe round trip | 16.9 µs | 14.0 µs |
| hackbench, 20 groups | 6.43 s | 3.06 s |
| memcpy 1 GB | 17.6 GB/s | 17.6 GB/s |
| GoReleaser cold build | 108.8 s | 86.3 s |
| Windows folder: untar 2,000 files | 13.3 s | 7.54 s |
| Windows folder: find ×3 | 3.19 s | 0.48 s |
| Windows folder: ls -lR ×3 | 3.27 s | 0.47 s |
| Windows folder: read 2,000 files | 4.39 s | 1.27 s |
| Windows folder: write 1 GB | 204 MB/s | 1,101 MB/s |
| Windows folder: read 1 GB | 227 MB/s | 1,113 MB/s |
| Windows folder: delete 2,000 files | 2.46 s | 1.45 s |
| guest disk: untar 2,000 files | 0.11 s | 0.05 s |
| guest disk: write 1 GB + fsync | 1,561 MB/s | 2,300 MB/s |
| guest disk: read 1 GB | 5,494 MB/s | 12,175 MB/s |

Medians of 3. The guest-disk 1 GB rows swing a lot between runs on both sides. The syscall gap is mostly
SRSO: Zen 5 isn't affected between user and kernel mode, but Hyper-V hides that CPUID bit, so WSL2's
kernel applies Safe RET anyway; smolvm passes the bit through on AMD family 1Ah+ only.

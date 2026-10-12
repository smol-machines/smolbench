# ASUS ROG Flow Z13, 2026-10-12, git workloads

Same box and settings as `z13-2026-10-11`. `smolvm-1.27.0-libkrun183.csv` is one
`.\run.ps1 -Distro AlpineBench -Git -Runs 3` with the smolvm 1.27.0 release and the
krun.dll from smol-machines/libkrun#183. The 1.27.0 release alone cannot run `git.sh`
on a Windows folder: `git clone` fails there (stale inode path after rename, and
symlinks that need Developer Mode).

git's own repository, v2.45.0, checkout to v2.47.0. `git status` is timed after the index
settles, since files written in the same second as the index make git re-hash them on every
status regardless of the VM. Medians of 3, seconds:

| workload | Windows folder WSL2 | Windows folder smolvm | guest disk WSL2 | guest disk smolvm |
|---|---|---|---|---|
| clone | 32.3 | 19.6 | 0.90 | 0.50 |
| status ×10 | 11.4 | 3.92 | 0.12 | 0.06 |
| checkout v2.45 → v2.47 | 7.25 | 5.04 | 0.28 | 0.19 |
| log --stat -n 2000 | 3.45 | 2.00 | 1.21 | 0.95 |
| grep | 3.64 | 1.97 | 0.11 | 0.07 |
| rm -rf | 5.81 | 3.43 | 0.12 | 0.06 |

# ASUS ROG Flow Z13, 2026-10-10

AMD Ryzen AI MAX+ 395 (Zen 5), Windows 11 build 26200, VBS on, AC power, Balanced plan.
WSL 2.7.10, Alpine 3.23 distro, `.wslconfig` processors=2 memory=4GB. smolvm 2 vCPU / 4 GB.
Each file is one `.\run.ps1 -Full -Fs -Runs 3` (WSL and smolvm alternate, so each file also has its own WSL rows).

- `smolvm-1.25.4.csv`: stock smolvm 1.25.4.
- `patched.csv`: smolvm 1.25.4 with libkrun from smol-machines/libkrun#180.

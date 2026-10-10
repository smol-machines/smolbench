#!/bin/sh
# Filesystem workloads, run identically in WSL and in a smolvm guest.
# Usage: sh fs.sh <shared_dir> <local_dir>
#   shared_dir: a folder on the Windows host (WSL: under /mnt/c, 9p; smolvm: a -v mount, virtiofs)
#   local_dir:  the guest's own disk (WSL: ext4 in its VHDX; smolvm: its ext4 overlay disk)
# Prints `name,value,unit` lines.
set -eu
N=${FS_FILES:-2000}
# Monotonic nanoseconds from /proc/timer_list; /proc/uptime's 10 ms ticks are too coarse for
# the ~0.1 s 1 GB transfers.
now() { awk '/now at/{printf "%.6f\n", $3/1e9; exit}' /proc/timer_list; }
el() { awk "BEGIN{printf \"%.3f\", $2-$1}"; }
# A source tree to copy around: N files of 4 KiB in 20 directories.
src=/tmp/fs-src.$$; mkdir -p $src
i=0; while [ $i -lt $N ]; do d=$src/d$((i % 20)); [ -d $d ] || mkdir $d; head -c 4096 /dev/urandom > $d/f$i; i=$((i+1)); done
tar -C $src -cf /tmp/fs-src.$$.tar .
for where in shared local; do
  if [ $where = shared ]; then base=$1; else base=$2; fi
  w=$base/fsbench.$$; rm -rf $w; mkdir -p $w; sync
  t0=$(now); tar -C $w -xf /tmp/fs-src.$$.tar; sync; t1=$(now)
  echo "${where}_untar_${N}_files_s,$(el $t0 $t1),s"
  t0=$(now); for r in 1 2 3; do find $w -type f | wc -l >/dev/null; done; t1=$(now)
  echo "${where}_find_x3_s,$(el $t0 $t1),s"
  t0=$(now); for r in 1 2 3; do ls -lR $w >/dev/null; done; t1=$(now)
  echo "${where}_stat_x3_s,$(el $t0 $t1),s"
  t0=$(now); cat $w/d*/f* >/dev/null; t1=$(now)
  echo "${where}_read_small_s,$(el $t0 $t1),s"
  t0=$(now); dd if=/dev/zero of=$w/big bs=1M count=1024 conv=fsync 2>/dev/null; t1=$(now)
  echo "${where}_write_1g_mb_s,$(awk "BEGIN{printf \"%.0f\", 1024/($t1-$t0)}"),MB/s"
  sync; echo 3 > /proc/sys/vm/drop_caches 2>/dev/null || true
  t0=$(now); dd if=$w/big of=/dev/null bs=1M 2>/dev/null; t1=$(now)
  echo "${where}_read_1g_mb_s,$(awk "BEGIN{printf \"%.0f\", 1024/($t1-$t0)}"),MB/s"
  t0=$(now); rm -rf $w; sync; t1=$(now)
  echo "${where}_rm_s,$(el $t0 $t1),s"
done
rm -rf $src /tmp/fs-src.$$.tar

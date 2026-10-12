#!/bin/sh
# git workloads, run identically in WSL and in a smolvm guest.
# Usage: sh git.sh <shared_dir> <local_dir>
#   shared_dir: a folder on the Windows host (WSL: under /mnt/c, 9p; smolvm: a -v mount, virtiofs)
#   local_dir:  the guest's own disk
# Works on a local copy of git's own repository, fetched once into
# <shared_dir>/.gitbench-cache.git and copied to /tmp before timing, so the
# network never counts. Prints `name,value,unit` lines.
set -eu
A=${GIT_TAG_A:-v2.45.0}
B=${GIT_TAG_B:-v2.47.0}
command -v git >/dev/null 2>&1 || apk add --no-cache -q git >/dev/null 2>&1 || { echo "apk add git failed (need --net)" >&2; exit 1; }
# smolvm's agent runs without HOME, and git refuses to start without one.
export HOME=${HOME:-/root}
export GIT_CONFIG_GLOBAL=/tmp/gitbench.gitconfig
git config --global safe.directory '*'
git config --global advice.detachedHead false
now() { awk '/now at/{printf "%.6f\n", $3/1e9; exit}' /proc/timer_list; }
el() { awk "BEGIN{printf \"%.3f\", $2-$1}"; }
cache=$1/.gitbench-cache.git
[ -d "$cache" ] || git clone -q --bare https://github.com/git/git "$cache"
src=/tmp/gitbench-src.$$.git
cp -r "$cache" $src
for where in shared local; do
  if [ $where = shared ]; then base=$1; else base=$2; fi
  w=$base/gitbench.$$; rm -rf $w; sync
  t0=$(now); git clone -q --no-hardlinks --no-checkout $src $w; git -C $w checkout -q $A; sync; t1=$(now)
  echo "${where}_git_clone_s,$(el $t0 $t1),s"
  cd $w
  # Files written in the same second as git's index are "racily clean", and
  # git re-hashes them on every status until the index is rewritten later.
  # That window depends on timing, not on the VM, so let it pass first.
  sleep 2; git status --porcelain >/dev/null; sleep 1
  t0=$(now); for i in 1 2 3 4 5 6 7 8 9 10; do git status --porcelain >/dev/null; done; t1=$(now)
  echo "${where}_git_status_x10_s,$(el $t0 $t1),s"
  t0=$(now); git checkout -q $B; sync; t1=$(now)
  echo "${where}_git_checkout_s,$(el $t0 $t1),s"
  t0=$(now); git log --stat -n 2000 >/dev/null; t1=$(now)
  echo "${where}_git_log_stat_s,$(el $t0 $t1),s"
  t0=$(now); git grep -c static >/dev/null; t1=$(now)
  echo "${where}_git_grep_s,$(el $t0 $t1),s"
  cd /
  t0=$(now); rm -rf $w; sync; t1=$(now)
  echo "${where}_git_rm_s,$(el $t0 $t1),s"
done
rm -rf $src /tmp/gitbench.gitconfig

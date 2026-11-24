#!/usr/bin/env bash
# leak_check.sh - look for the reference fix hidden inside a benchmark task image.
#
# Purpose: before trusting scores on a SWE-style task image, check that the
# repository at <workdir> does not already carry the future fix: commits not
# reachable from HEAD (other branches, tags, stash, reflog), unreachable objects,
# copies of the fixed code in installed packages, or test/solution folders baked
# into the image.
#
# Usage: eval/scripts/leak_check.sh <image> <workdir-in-container> [<marker-file>]
#   <marker-file>  optional host text file, one distinctive line per row that the
#                  reference fix ADDS (>= 25 chars, <= 10 lines). Mounted read-only.
#
# The container runs with --rm --network none and no other mount.
# Last line: "VERDICT: LEAK (<reasons>)" or "VERDICT: CLEAN".
# LEAK means at least one of: a commit reachable from a ref or the reflog but not
# from HEAD; an unreachable commit; a marker line found anywhere (working tree,
# history, unreachable objects, installed packages); /solution or /oracle present.
# Exit: 0 CLEAN, 1 LEAK, 2 usage/docker error, git missing, or workdir not a repo.
#
# Limit: this only inspects the image. It cannot see leaks through the network:
# the upstream repository is public, so an agent with internet access during its
# run can fetch the fix regardless of a CLEAN verdict.
set -u

usage() { echo "usage: $0 <image> <workdir-in-container> [<marker-file>]" >&2; exit 2; }
[ $# -ge 2 ] && [ $# -le 3 ] || usage
image=$1 workdir=$2 marker=${3:-}

command -v docker >/dev/null || { echo "ERROR: docker not found" >&2; exit 2; }
docker image inspect "$image" >/dev/null 2>&1 || { echo "ERROR: image not found locally: $image" >&2; exit 2; }

mount=()
if [ -n "$marker" ]; then
  [ -f "$marker" ] || { echo "ERROR: marker file not found: $marker" >&2; exit 2; }
  mount=(-v "$(realpath "$marker"):/tmp/leak_markers.txt:ro")
fi

read -r -d '' inner <<'EOF'
wd=$1
reasons=""
leak() { reasons="${reasons:+$reasons; }$1"; }
h() { printf '\n== %s ==\n' "$1"; }
g() { git -c safe.directory='*' "$@"; }

command -v git >/dev/null || { echo "ERROR: git is not installed in the image" >&2; exit 2; }
cd "$wd" 2>/dev/null || { echo "ERROR: workdir does not exist: $wd" >&2; exit 2; }
[ "$(g rev-parse --is-inside-work-tree 2>/dev/null)" = true ] || { echo "ERROR: workdir is not a git repository: $wd" >&2; exit 2; }
tmp=$(mktemp -d)

h "1. HEAD"
echo "HEAD: $(g rev-parse HEAD)"
echo "branch: $(g symbolic-ref -q --short HEAD || echo '(detached)')"
echo "git status --short (first 10):"; g status --short | head -10

h "2. Commit counts"
echo "reachable from HEAD: $(g rev-list --count HEAD)"
echo "reachable from all refs: $(g rev-list --count --all)"
g rev-list --all --not HEAD > "$tmp/notfromhead"
n=$(wc -l < "$tmp/notfromhead")
echo "reachable from a ref but not from HEAD: $n"
[ "$n" -gt 0 ] && { head -10 "$tmp/notfromhead" | g log --no-walk --stdin --format='  %h %cI %s'; leak "$n commit(s) reachable from refs but not HEAD"; }

h "3. Refs, remotes, stash, reflog"
echo "refs:"; g for-each-ref --format='  %(objectname:short) %(refname)'
echo "remotes:"; g remote -v | sed 's/^/  /'
echo "stash:"; g stash list | sed 's/^/  /'
echo "reflog entries: $(g reflog --all 2>/dev/null | wc -l)"
g rev-list --reflog --not --all > "$tmp/reflogonly" 2>/dev/null
n=$(wc -l < "$tmp/reflogonly")
echo "commits only reachable from the reflog: $n"
[ "$n" -gt 0 ] && { head -10 "$tmp/reflogonly" | g log --no-walk --stdin --format='  %h %cI %s'; leak "$n commit(s) only in reflog"; }

h "4. Unreachable objects (git fsck --unreachable --no-reflogs)"
g fsck --unreachable --no-reflogs > "$tmp/fsck" 2> "$tmp/fsck.err"
for t in commit tree blob tag; do echo "unreachable $t: $(grep -c "^unreachable $t " "$tmp/fsck")"; done
[ -s "$tmp/fsck.err" ] && { echo "fsck stderr (first 5):"; head -5 "$tmp/fsck.err"; }
awk '$2=="commit"{print $3}' "$tmp/fsck" > "$tmp/unreach_commits"
n=$(wc -l < "$tmp/unreach_commits")
[ "$n" -gt 0 ] && { head -10 "$tmp/unreach_commits" | g log --no-walk --stdin --format='  %h %cI %s'; leak "$n unreachable commit(s)"; }

h "5. Object store"
g count-objects -v
echo "shallow: $(g rev-parse --is-shallow-repository)"

h "6. Dates"
echo "HEAD commit date: $(g log -1 --format='%cI' HEAD)"
newest=$({ g rev-list --all; cat "$tmp/reflogonly" "$tmp/unreach_commits"; } | g log --no-walk --stdin --format='%ct %cI %h %s' 2>/dev/null | sort -n | tail -1)
echo "newest commit among all found: ${newest#* }"
if [ "${newest%% *}" -gt "$(g log -1 --format=%ct HEAD)" ] 2>/dev/null; then echo "a commit NEWER than HEAD exists"; else echo "no commit newer than HEAD"; fi

h "7. Marker lines"
M=/tmp/leak_markers.txt
if [ -f "$M" ]; then
  grep -v '^[[:space:]]*$' "$M" > "$tmp/m"
  # one grep pass per location; -o prints which marker matched
  grep -rFoh -f "$tmp/m" --exclude-dir=.git . 2>/dev/null | sort -u > "$tmp/hit_tree"
  head -2000 "$tmp/fsck" | awk '$2=="blob"||$2=="commit"{print $3}' | while read -r o; do g cat-file -p "$o"; done | grep -Fo -f "$tmp/m" | sort -u > "$tmp/hit_unreach"
  sp=""
  if command -v python3 >/dev/null; then
    sp=$(python3 -c 'import sys; print("\n".join(p for p in sys.path if p.endswith(("site-packages","dist-packages"))))')
  fi
  sp="$sp $(ls -d /opt/*conda*/envs/*/lib/python*/site-packages 2>/dev/null)"
  real_wd=$(pwd -P)
  for d in $sp; do [ -d "$d" ] && grep -rFo -f "$tmp/m" --include='*.py' "$d" 2>/dev/null | grep -v "^$real_wd/"; done | sort -u > "$tmp/hit_sp"
  echo "site-packages searched: $(echo $sp)"
  while IFS= read -r line; do
    echo "marker: $line"
    a=$(grep -cxF -- "$line" "$tmp/hit_tree")
    b=$(g log --all --reflog -S"$line" --format='%h %cI %s' 2>/dev/null | head -5)
    c=$(grep -cxF -- "$line" "$tmp/hit_unreach")
    d=$(grep -F -- ":$line" "$tmp/hit_sp" | cut -d: -f1 | head -3)
    echo "  (a) working tree: $([ "$a" -gt 0 ] && echo FOUND || echo no)"
    echo "  (b) history (log --all -S): ${b:-no}"
    echo "  (c) unreachable objects: $([ "$c" -gt 0 ] && echo FOUND || echo no)"
    echo "  (d) installed packages: ${d:-no}"
    [ "$a" -gt 0 ] && leak "marker in working tree"
    [ -n "$b" ] && leak "marker in history"
    [ "$c" -gt 0 ] && leak "marker in unreachable objects"
    [ -n "$d" ] && leak "marker in site-packages"
  done < "$tmp/m"
else
  echo "(no marker file given)"
fi

h "8. Test/solution material in the image"
for p in /tests /solution /oracle; do
  if [ -e "$p" ]; then echo "$p EXISTS:"; ls -la "$p" | head -20; else echo "$p: absent"; fi
done
[ -e /solution ] && leak "/solution present"
[ -e /oracle ] && leak "/oracle present"

echo
if [ -n "$reasons" ]; then
  # drop duplicate reasons (several markers can give the same one)
  reasons=$(printf '%s' "$reasons" | tr ';' '\n' | sed 's/^ //' | awk '!s[$0]++' | paste -sd';' | sed 's/;/; /g')
  echo "VERDICT: LEAK ($reasons)"; exit 1
fi
echo "VERDICT: CLEAN"; exit 0
EOF

docker run --rm --network none "${mount[@]}" --entrypoint bash "$image" -c "$inner" leak_check "$workdir"
rc=$?
case $rc in
  0|1) exit $rc ;;
  *) echo "ERROR: check did not complete (docker/container exit $rc); no verdict" >&2; exit 2 ;;
esac

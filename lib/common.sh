# shellcheck shell=bash
# Shared helpers for nu subcommands. Sourced, never executed.

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  C_RESET=$'\033[0m'
  C_DIM=$'\033[2m'
  C_BOLD=$'\033[1m'
  C_RED=$'\033[31m'
  C_GREEN=$'\033[32m'
  C_YELLOW=$'\033[33m'
  C_BLUE=$'\033[34m'
else
  C_RESET= C_DIM= C_BOLD= C_RED= C_GREEN= C_YELLOW= C_BLUE=
fi

NU_STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/nu"
NU_REPO_REGISTRY="$NU_STATE_DIR/repos"

die() { printf '%snu: %s%s\n' "$C_RED" "$*" "$C_RESET" >&2; exit 1; }
warn() { printf '%swarn:%s %s\n' "$C_YELLOW" "$C_RESET" "$*" >&2; }
step() { printf '%s==>%s %s\n' "$C_BLUE" "$C_RESET" "$*"; }
dim() { printf '%s%s%s\n' "$C_DIM" "$*" "$C_RESET"; }

confirm() {
  [ "${NU_ASSUME_YES:-0}" = 1 ] && return 0
  [ -t 0 ] || return 1
  local reply
  printf '%s [y/N] ' "$1" >&2
  read -r reply || return 1
  case "$reply" in
    [yY] | [yY][eE][sS]) return 0 ;;
    *) return 1 ;;
  esac
}

# The shell wrapper passes NU_CD_FILE; whatever we write there is where the
# parent shell lands. Without the wrapper we can only suggest the cd.
request_cd() {
  if [ -n "${NU_CD_FILE:-}" ]; then
    printf '%s\n' "$1" > "$NU_CD_FILE"
  else
    dim "cd $1"
  fi
}

# --- git / repo resolution ------------------------------------------------

# Main worktree of the repo containing $PWD, even when called from inside a
# linked worktree (its first porcelain entry is always the main one).
main_worktree() {
  git rev-parse --is-inside-work-tree > /dev/null 2>&1 \
    || die "not inside a git repository (or pass --repo <name>)"
  git worktree list --porcelain | sed -n 's/^worktree //p' | head -1
}

# Sibling directory convention: /path/to/flip -> /path/to/flip.worktrees
worktrees_root() { printf '%s.worktrees\n' "$1"; }

default_branch() {
  local head
  head=$(git -C "$1" symbolic-ref --quiet refs/remotes/origin/HEAD 2>/dev/null) || head=
  if [ -n "$head" ]; then
    printf '%s\n' "${head#refs/remotes/origin/}"
    return
  fi
  for candidate in main master; do
    if git -C "$1" show-ref --verify --quiet "refs/heads/$candidate"; then
      printf '%s\n' "$candidate"
      return
    fi
  done
  die "cannot determine the default branch of $1"
}

branch_to_dirname() { printf '%s\n' "$1" | tr '/' '-'; }

worktree_path_for_branch() {
  git -C "$1" worktree list --porcelain | awk -v want="refs/heads/$2" '
    /^worktree /  { path = substr($0, 10) }
    /^branch /    { if (substr($0, 8) == want) { print path; exit } }
  '
}

# Every repo we touch gets remembered, so --repo <name> works from anywhere.
register_repo() {
  local path="$1" name
  [ -n "$path" ] && [ -d "$path" ] || return 0
  name=$(basename "$path")
  mkdir -p "$NU_STATE_DIR"
  touch "$NU_REPO_REGISTRY"
  grep -q "^$name	$path$" "$NU_REPO_REGISTRY" 2>/dev/null && return 0
  awk -F'\t' -v n="$name" 'NF == 2 && $1 != "" && $2 != "" && $1 != n' \
    "$NU_REPO_REGISTRY" > "$NU_REPO_REGISTRY.tmp" 2>/dev/null || :
  printf '%s\t%s\n' "$name" "$path" >> "$NU_REPO_REGISTRY.tmp"
  sort -o "$NU_REPO_REGISTRY" "$NU_REPO_REGISTRY.tmp"
  rm -f "$NU_REPO_REGISTRY.tmp"
}

lookup_repo() {
  local name="$1" path
  path=$(awk -F'\t' -v n="$name" '$1 == n { print $2; exit }' "$NU_REPO_REGISTRY" 2>/dev/null) || path=
  [ -n "$path" ] || die "unknown repo '$name'; known: $(known_repos | tr '\n' ' ')"
  [ -d "$path" ] || die "repo '$name' is registered at $path, which no longer exists"
  printf '%s\n' "$path"
}

known_repos() { awk -F'\t' '{ print $1 }' "$NU_REPO_REGISTRY" 2>/dev/null || :; }

human_size() {
  local kb="$1"
  if [ "$kb" -ge 1048576 ]; then
    printf '%.1fG\n' "$(echo "$kb" | awk '{ print $1 / 1048576 }')"
  elif [ "$kb" -ge 1024 ]; then
    printf '%dM\n' "$((kb / 1024))"
  else
    printf '%dK\n' "$kb"
  fi
}

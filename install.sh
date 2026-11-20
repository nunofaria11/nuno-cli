#!/usr/bin/env bash
# Symlinks cli/bin/nu into ~/.local/bin, the nu skills and commands into ~/.agents,
# the Herdr app mods into its mods folder, and wires the zsh wrapper into ~/.zshrc.
set -euo pipefail

root=$(cd -P "$(dirname "$0")" && pwd)
bindir="${NU_BINDIR:-$HOME/.local/bin}"
zshrc="$HOME/.zshrc"
agents="${NU_AGENTS_DIR:-$HOME/.agents}"
marker="# nuno-plugins"
shell_line="[ -f \"$root/cli/shell/nu.zsh\" ] && source \"$root/cli/shell/nu.zsh\" $marker"
wire_shell=1

[ "${1:-}" = "--no-shell" ] && wire_shell=0

mkdir -p "$bindir"
ln -sfn "$root/cli/bin/nu" "$bindir/nu"
echo "linked $bindir/nu -> $root/cli/bin/nu"

case ":$PATH:" in
  *":$bindir:"*) ;;
  *) echo "warning: $bindir is not on your PATH" >&2 ;;
esac

# The skills and commands live next to the nu commands they drive;
# ~/.agents is where Claude Code and omp discover them on this machine.
link_agent() {
  local src=$1 dest="$2/$(basename "$1")"
  mkdir -p "$2"
  if [ -e "$dest" ] && [ ! -L "$dest" ]; then
    echo "warning: $dest exists and is not a link; left alone" >&2
    return
  fi
  ln -sfn "$src" "$dest"
  echo "linked $dest -> $src"
}

for src in "$root"/cli/skills/*/; do
  if [ -d "$src" ]; then link_agent "${src%/}" "$agents/skills"; fi
done
for src in "$root"/cli/commands/*.md; do
  if [ -f "$src" ]; then link_agent "$src" "$agents/commands"; fi
done

# Adds herdr-app/keys.json's bindings to the app's keys.json where the key is free; a key bound
# to something else stays as it is. The app's file is rewritten, never linked: Herdr and other
# installers write it in place.
merge_keys() {
  local src=$1 dest=$2 tmp
  if [ ! -f "$dest" ]; then
    echo "warning: no $dest; open the Herdr app once, then re-run" >&2
    return
  fi
  # One line per binding: added, or kept because the key is taken.
  jq -r --slurpfile add "$src" '
    . as $cur | $add[0] | to_entries[] as $s | $s.value | to_entries[]
    | ($cur[$s.key][.key]) as $have
    | if $have == null then "keys: added \($s.key) \(.key) → \(.value | tojson)"
      elif $have != .value then "warning: keys: \($s.key) \(.key) is \($have | tojson); left as it is"
      else empty end' "$dest"
  tmp=$(mktemp "$dest.XXXXXX")
  jq --slurpfile add "$src" '
    reduce ($add[0] | to_entries[] as $s | $s.value | to_entries[] | {s: $s.key, k: .key, v: .value}) as $e
      (.; if .[$e.s][$e.k] == null then .[$e.s][$e.k] = $e.v else . end)' "$dest" > "$tmp"
  if jq -e --slurpfile new "$tmp" '. == $new[0]' "$dest" > /dev/null; then
    rm -f "$tmp"
  else
    mv -f "$tmp" "$dest"
  fi
}

# The Herdr desktop app hot-reloads mods/ ; the link keeps edits live.
app_home="${HERDR_APP_HOME:-$HOME/.config/herdr-app}"
if [ -d "$app_home" ]; then
  for src in "$root"/herdr-app/mods/*/; do
    link_agent "${src%/}" "$app_home/mods"
  done
  command -v herdr-app > /dev/null 2>&1 && herdr-app check "$root/herdr-app/mods/worktrees"
  if command -v jq > /dev/null 2>&1; then
    merge_keys "$root/herdr-app/keys.json" "$app_home/keys.json"
  else
    echo "warning: jq is not on PATH; add herdr-app/keys.json to $app_home/keys.json by hand" >&2
  fi
else
  echo "no $app_home; skipped linking the Herdr app mods"
fi

if [ "$wire_shell" = 1 ]; then
  if grep -qF "$marker" "$zshrc" 2>/dev/null; then
    echo "zshrc already sources cli/shell/nu.zsh"
  else
    printf '\n%s\n' "$shell_line" >> "$zshrc"
    echo "appended to $zshrc: $shell_line"
    echo "run 'source $zshrc' (or open a new shell) to pick up the nu/wt functions"
  fi
else
  echo "add this to your ~/.zshrc yourself:"
  echo "  $shell_line"
fi

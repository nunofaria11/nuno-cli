#!/usr/bin/env bash
# Symlinks bin/nu into ~/.local/bin, the agent skills and commands into ~/.agents, and
# wires the zsh wrapper into ~/.zshrc.
set -euo pipefail

root=$(cd -P "$(dirname "$0")" && pwd)
bindir="${NU_BINDIR:-$HOME/.local/bin}"
zshrc="$HOME/.zshrc"
agents="${NU_AGENTS_DIR:-$HOME/.agents}"
marker="# nuno-cli"
shell_line="[ -f \"$root/shell/nu.zsh\" ] && source \"$root/shell/nu.zsh\" $marker"
wire_shell=1

[ "${1:-}" = "--no-shell" ] && wire_shell=0

mkdir -p "$bindir"
ln -sfn "$root/bin/nu" "$bindir/nu"
echo "linked $bindir/nu -> $root/bin/nu"

case ":$PATH:" in
  *":$bindir:"*) ;;
  *) echo "warning: $bindir is not on your PATH" >&2 ;;
esac

# The skills and commands that drive nu's own commands live next to the code they
# describe; ~/.agents is where Claude Code and omp discover them.
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

for src in "$root"/agents/skills/*/; do
  if [ -d "$src" ]; then link_agent "${src%/}" "$agents/skills"; fi
done
for src in "$root"/agents/commands/*.md; do
  if [ -f "$src" ]; then link_agent "$src" "$agents/commands"; fi
done

if [ "$wire_shell" = 1 ]; then
  if grep -qF "$marker" "$zshrc" 2>/dev/null; then
    echo "zshrc already sources shell/nu.zsh"
  else
    printf '\n%s\n' "$shell_line" >> "$zshrc"
    echo "appended to $zshrc: $shell_line"
    echo "run 'source $zshrc' (or open a new shell) to pick up the nu/wt functions"
  fi
else
  echo "add this to your ~/.zshrc yourself:"
  echo "  $shell_line"
fi

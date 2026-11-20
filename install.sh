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

# The Herdr desktop app hot-reloads mods/ ; the link keeps edits live.
app_home="${HERDR_APP_HOME:-$HOME/.config/herdr-app}"
if [ -d "$app_home" ]; then
  for src in "$root"/herdr-app/mods/*/; do
    link_agent "${src%/}" "$app_home/mods"
  done
  command -v herdr-app > /dev/null 2>&1 && herdr-app check "$root/herdr-app/mods/worktrees"
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

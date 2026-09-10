#!/usr/bin/env bash
# Symlinks bin/nu into ~/.local/bin and wires the zsh wrapper into ~/.zshrc.
set -euo pipefail

root=$(cd -P "$(dirname "$0")" && pwd)
bindir="${NU_BINDIR:-$HOME/.local/bin}"
zshrc="$HOME/.zshrc"
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

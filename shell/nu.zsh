# Resolved at source time so completion can enumerate libexec/ instead of
# hardcoding a command list that goes stale the moment you add one.
NU_ROOT_DIR=${0:A:h:h}

# Wrapper around bin/nu. A subprocess cannot chdir its parent shell, so nu
# writes the directory it wants us to enter into $NU_CD_FILE and we obey.
nu() {
  local cdfile rc
  cdfile=$(mktemp "${TMPDIR:-/tmp}/nu-cd.XXXXXX") || return 1
  NU_CD_FILE=$cdfile command nu "$@"
  rc=$?
  if [[ -s $cdfile ]]; then
    cd -- "$(<$cdfile)" || rc=$?
  fi
  rm -f -- "$cdfile"
  return $rc
}

wt() { nu wt "$@"; }

_nu_worktree_names() {
  local main
  main=$(git worktree list --porcelain 2>/dev/null | sed -n 's/^worktree //p' | head -1) || return
  [[ -n $main ]] || return
  git worktree list --porcelain 2>/dev/null \
    | sed -n 's/^worktree //p' | grep -v "^${main}$" | xargs -n1 basename 2>/dev/null
}

_nu_commands() {
  local file
  for file in $NU_ROOT_DIR/libexec/nu-*(N); do
    print -r -- ${${file:t}#nu-}
  done
  print -r -- help
}

_nu_branches() {
  git for-each-ref --format='%(refname:short)' refs/heads 2>/dev/null
}

_nu() {
  if (( CURRENT == 2 )); then
    compadd -- ${(f)"$(_nu_commands)"}
    return
  fi
  case $words[2] in
    wt)
      if (( CURRENT == 3 )); then
        compadd -- add list clean cd rm
        return
      fi
      case $words[3] in
        add) compadd -- ${(f)"$(git for-each-ref --format='%(refname:short)' refs/heads refs/remotes/origin 2>/dev/null | sed 's|^origin/||' | sort -u)"} ;;
        cd|rm) compadd -- ${(f)"$(_nu_worktree_names)"} ;;
      esac
      ;;
    rb)
      # Subcommands only in third position; branch names anywhere after it.
      (( CURRENT == 3 )) && compadd -- plan report verify abort cleanup
      compadd -- -y --yes -n --dry-run --base --tip --old-tip
      compadd -- ${(f)"$(_nu_branches)"}
      ;;
  esac
}

_wt() {
  local -a words_shifted
  words=(nu wt "${words[@]:1}")
  (( CURRENT += 1 ))
  _nu
}

if (( $+functions[compdef] )); then
  compdef _nu nu
  compdef _wt wt
fi

# nu wt — worktrees

Worktrees live in a sibling directory of the repo, named after the branch with
slashes turned into dashes:

```
/Users/nuno/code/flipnext/flip
/Users/nuno/code/flipnext/flip.worktrees/act-4171-invite-code-service-publish-...
```

```sh
wt add act-1234-some-ticket   # create (or reuse) and cd into it
wt add feature/x -b master    # branch off something other than origin/HEAD
wt list                       # what exists, and whether it is disposable
wt list --size                # ...with disk usage
wt cd 4171                    # cd into the worktree matching a substring
wt rm 4171                    # remove one
wt clean -n                   # what could go, and how much disk it would free
wt clean                      # ...and remove it, asking per worktree
wt clean -y --delete-branches # no questions, drop the local branches too
```

`wt` is the shell function from `shell/nu.zsh`; `nu wt` is the same thing
without the `cd`. With no subcommand, `nu wt` lists.

## add

```
nu wt add <branch> [-b <ref>] [--fetch] [--no-cd] [--no-hook]
```

`add` picks its source in this order: an existing local branch, then
`origin/<branch>` (tracked), otherwise a new branch off `origin/<default>`
with no upstream so `git push -u` behaves. `-b/--base <ref>` overrides the last
case.

A branch that is already checked out somewhere is not created twice — `add`
just moves you there. A directory that exists at the target path but is *not* a
worktree for that branch is an error, not something to overwrite; `nu wt clean`
will offer to remove it.

`--fetch` runs `git fetch --prune` first, `--no-cd` leaves the shell where it
is, `--no-hook` skips the repo's [post-add hook](#hooks).

## list

```
nu wt list [--size]
```

Every worktree with its verdict and the reason behind it. `--size` measures
disk usage with `du`, which is slow on a large checkout.

## cd, rm

```
nu wt cd <query>
nu wt rm <query> [--force]
```

`<query>` is a case-insensitive substring of the directory name — the ticket
number is usually enough. `cd` asks which one when several match; `rm` refuses
to guess. `rm` warns about uncommitted changes and asks before removing;
`--force` is for the worktrees git itself refuses to drop.

## clean

```
nu wt clean [-n] [-y] [--no-fetch] [--no-size] [--force] [--delete-branches]
```

### What `clean` considers disposable

| verdict | meaning | removable |
|---|---|---|
| `merged` | HEAD is an ancestor of the default branch, or the branch has no commits of its own | yes |
| `gone` | the upstream branch was deleted — the usual footprint of a squash-merged MR | yes |
| `orphan` | a directory in `*.worktrees/` that git does not know about | yes |
| `prunable` | git has a worktree registered whose directory is gone | yes |
| `keep` | uncommitted or untracked files, unpushed commits, an open MR, or detached HEAD | no |

The branch state and the working tree are judged separately, so a `keep` still
tells you where the branch stands: `1 untracked file(s), nowhere else — merged
into master` means the only thing holding that worktree is a file that exists
nowhere else. Untracked files never get deleted for you, however finished the
branch is — `wt rm <name> --force` is the deliberate way out.

`clean` runs `git fetch --prune` first, because `gone` is only accurate against
a pruned remote (`--no-fetch` skips it). Nothing is removed without a `y` per
worktree unless you pass `-y`; with no terminal to ask on it reports and stops.
`orphan` directories are deleted with `rm -rf`, which is why removal refuses
any path that is not inside a `*.worktrees/` directory.

`-n/--dry-run` reports and removes nothing. `--delete-branches` also drops the
local branch of each worktree it removed, and keeps it when git objects.

## Working on another repo

Every subcommand takes `--repo <name>` to work on a repo you are not standing
in; repos register themselves the first time you use `nu wt` inside them
(`~/.local/state/nu/repos`, or `$XDG_STATE_HOME/nu/repos`). The name is the
directory name of the main worktree.

## Hooks

`hooks/<repo-directory-name>/post-add`, if executable, runs inside each new
worktree — the place for copying untracked local config or kicking off a
bootstrap:

```
hooks/flip/post-add        -> runs for /Users/nuno/code/flipnext/flip
hooks/mobile-app/post-add  -> runs for /Users/nuno/code/flipnext/mobile-app
```

cwd is the new worktree, and `NU_WT_PATH`, `NU_WT_BRANCH` and `NU_WT_MAIN` are
exported. See `hooks/example.post-add`.

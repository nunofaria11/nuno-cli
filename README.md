# nuno-cli

Personal commands, dispatched git-style. `bin/nu` finds `libexec/nu-<command>`
and executes it — adding a command means dropping a file in `libexec/`, nothing
to register and nothing to build.

## Install

```sh
./install.sh
```

That symlinks `bin/nu` into `~/.local/bin` and appends one line to `~/.zshrc`
which sources `shell/nu.zsh`. Use `./install.sh --no-shell` to skip the second
part and wire it up yourself.

The shell part matters: a subprocess cannot change its parent shell's
directory. `nu` writes the directory it wants you in to `$NU_CD_FILE` and the
zsh function does the `cd`. Without it every command still works, it just
prints the `cd` for you to run. The function also defines `wt` as a shortcut
for `nu wt`, plus completion for both.

## Changing it

Edit and go — `~/.local/bin/nu` is a symlink into this repo and every
`libexec/nu-*` script is read from disk on each run, so there is nothing to
rebuild or reinstall. Three things do not hot-reload:

- **`shell/nu.zsh`** — the `nu`/`wt` functions live in your shell's memory.
  `exec zsh` (or `source ~/.zshrc`) after touching it.
- **Moving this repo** — the symlink and the `~/.zshrc` line both point at the
  old path. Re-run `./install.sh`; it is idempotent.
- **New completions inside a command** — `nu help` and top-level completion
  enumerate `libexec/` on their own, but a new subcommand's own flags are
  listed by hand in `_nu`.

A new command is a new file:

```sh
printf '#!/usr/bin/env bash\n# summary: what it does\nset -euo pipefail\n' > libexec/nu-thing
chmod +x libexec/nu-thing
```

The `# summary:` line is what `nu help` prints. Source `lib/common.sh` from it
for colours, `confirm`, `die` and `request_cd`.

## nu wt — worktrees

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

`add` picks its source in this order: an existing local branch, then
`origin/<branch>` (tracked), otherwise a new branch off `origin/<default>`
with no upstream so `git push -u` behaves.

Every subcommand takes `--repo <name>` to work on a repo you are not standing
in; repos register themselves the first time you use `nu wt` inside them
(`~/.local/state/nu/repos`).

### What `clean` considers disposable

| verdict | meaning | removable |
|---|---|---|
| `merged` | HEAD is an ancestor of the default branch, or the branch has no commits of its own | yes |
| `gone` | the upstream branch was deleted — the usual footprint of a squash-merged MR | yes |
| `orphan` | a directory in `*.worktrees/` that git does not know about | yes |
| `keep` | uncommitted or untracked files, unpushed commits, an open MR, or detached HEAD | no |

The branch state and the working tree are judged separately, so a `keep` still
tells you where the branch stands: `1 untracked file(s), nowhere else — merged
into master` means the only thing holding that worktree is a file that exists
nowhere else. Untracked files never get deleted for you, however finished the
branch is — `wt rm <name> --force` is the deliberate way out.

`clean` runs `git fetch --prune` first, because `gone` is only accurate against
a pruned remote (`--no-fetch` skips it). Nothing is removed without a `y` per
worktree unless you pass `-y`. `orphan` directories are deleted with `rm -rf`,
which is why removal refuses any path that is not inside a `*.worktrees/`
directory.

## nu rebase — stacked branches

Splitting one big MR into a stack means every review round on an early branch
leaves the branches above it sitting on a tip that no longer exists. `nu rebase`
replays the whole stack in one pass and resolves the conflicts that have exactly
one sane answer.

```sh
nu rebase                     # plan, confirm, rebase the stack above the rewritten branch
nu rebase -y                  # no question
nu rebase -n                  # plan and take backups, rebase nothing
nu rebase b1 b2 b3            # explicit order, bottom first
nu rebase --base origin/master  # restack onto an advanced base
nu rebase verify              # range-diff every rewritten branch against its backup
nu rebase abort               # abort and restore every branch
nu rebase cleanup             # drop the backup refs and the run's state
```

The chain is inferred from the current branch. A parent that was amended is no
longer an ancestor of the branches above it, so candidates are anchored on the
newest tip they had that the stack tip still contains — which is what makes the
`git rebase --onto <new-parent> <old-tip> <stack-tip> --update-refs` arguments
correct, and one rebase enough for the whole stack.

### What it resolves without asking

| class | how | reported |
|---|---|---|
| conflict seen before | `rerere`, enabled per invocation | as a cache hit, worth a glance |
| lockfiles | private merge driver takes the branch's side | yes, they owe a dependency re-resolve |
| disjoint line edits | line-granular three-way merge | yes, with the reason |
| generated files | same merge; one side if it clashes or exceeds 20k lines | yes, they owe a regeneration |

Generated paths are whatever the repo's own `.gitattributes` marks with an
attribute containing `generated`, so the classification stays the repo's
business. `git` conflicts whole hunks, which is why an import inserted next to a
renamed one collides even though the two edits touch different lines; that case
is the bulk of a stacked rebase and it merges cleanly here.

Anything else stops the run with exit `10` and prints the conflict **narrowed to
the lines that actually clash** — the disjoint edits around them are already
applied. Resolve, `git add`, and run `nu rebase` again to continue; mid-rebase it
resumes without asking. `/rebase` in Claude Code or omp drives the same engine
when you would rather have an agent make those calls.

Backups go to `refs/stack-backup/<branch>`, invisible to `git branch`. Nothing in
the repository config, `.gitattributes` or `.git/info` is touched: every git
behaviour change is passed per invocation with `git -c` against a private
attributes file, so a crashed run leaves nothing to undo.

An optional `.stack-rebase.json` at the repo root tunes it:

```json
{
  "takeTheirs": ["api/spec/v4/development/*.yaml"],
  "askAlways": ["**/db/migration/**"],
  "regen": { "api/spec/**": "make -C api build" }
}
```

## Hooks

`hooks/<repo-directory-name>/post-add`, if executable, runs inside each new
worktree — the place for copying untracked local config or kicking off a
bootstrap. See `hooks/example.post-add`.

## Layout

```
bin/nu                 dispatcher
libexec/nu-<command>    one file per command; `# summary:` shows up in `nu help`
lib/common.sh           colours, prompts, cd requests, repo resolution
lib/stack_rebase.py     the stacked-rebase engine behind nu rebase
shell/nu.zsh            nu() wrapper, wt alias, completion
hooks/                  per-repo post-add hooks
```

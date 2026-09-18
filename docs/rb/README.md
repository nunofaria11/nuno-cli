# nu rb — stacked branches

Splitting one big MR into a stack means every review round on an early branch
leaves the branches above it sitting on a tip that no longer exists. `nu rb`
replays the whole stack in one pass and resolves the conflicts that have exactly
one sane answer.

```sh
nu rb                     # plan, confirm, rebase the stack above the rewritten branch
nu rb -y                  # no question
nu rb -n                  # plan and take backups, rebase nothing
nu rb b1 b2 b3            # explicit order, bottom first
nu rb --base origin/master  # restack onto an advanced base
nu rb verify              # range-diff every rewritten branch against its backup
nu rb abort               # abort and restore every branch
nu rb cleanup             # drop the backup refs and the run's state
```

The chain is inferred from the current branch. A parent that was amended is no
longer an ancestor of the branches above it, so candidates are anchored on the
newest tip they had that the stack tip still contains — which is what makes the
`git rebase --onto <new-parent> <old-tip> <stack-tip> --update-refs` arguments
correct, and one rebase enough for the whole stack.

## What it resolves without asking

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

## When it stops

Anything else stops the run with exit `10` and prints the conflict **narrowed to
the lines that actually clash** — the disjoint edits around them are already
applied. Resolve, `git add`, and run `nu rb` again to continue; mid-rebase it
resumes without asking. `nu rb report` re-prints the conflicts of a paused
run. `/rebase` in Claude Code or omp drives the same engine when you would
rather have an agent make those calls.

Exit `10` is a decision request, not a failure — scripts and agents should
branch on it rather than treat it as an error.

## Safety

Backups go to `refs/stack-backup/<branch>`, invisible to `git branch`. Nothing in
the repository config, `.gitattributes` or `.git/info` is touched: every git
behaviour change is passed per invocation with `git -c` against a private
attributes file, so a crashed run leaves nothing to undo.

`nu rb verify` range-diffs every rewritten branch against its backup — the
check that the replay changed nothing but the base. `abort` restores every
branch from backup; `cleanup` drops the backup refs and the run's state once you
are happy.

## Options

```
-y, --yes          do not ask before rewriting
-n, --dry-run      show the plan and take backups, rebase nothing
--base <branch>    branch the stack forks from (default: origin HEAD)
--tip <branch>     top of the stack when inferring (default: current branch)
--old-tip <sha>    pre-rewrite tip of the diverged parent, when the reflog
                   reading looks wrong
```

Subcommands: `plan`, `report`, `verify`, `abort`, `cleanup`.

## Per-repo configuration

An optional `.stack-rebase.json` at the repo root tunes it:

```json
{
  "takeTheirs": ["api/spec/v4/development/*.yaml"],
  "askAlways": ["**/db/migration/**"],
  "regen": { "api/spec/**": "make -C api build" }
}
```

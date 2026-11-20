---
name: stacked-rebase
description: Rebase a chain of stacked git branches after earlier branches in the stack were amended, rewritten, or squash-merged into master, propagating changes branch by branch with correct --onto handling, automatic resolution of mechanical conflicts, backups, and range-diff verification — including stacks whose branches each live in their own git worktree. Use this skill whenever the user asks to rebase stacked branches — "rebase the stack", "rebase the remaining branches", "I changed branch 1 and 2, rebase the rest", "rebase B onto A", "rebase X onto Y", "the bottom MR got merged, rebase onto master", "continue the rebase (operations)", "rebase the next branches in line" — or names a chain of ticket branches (act-XXXX-..., pla-XXXX-...) to be rebased in sequence. Also use when resuming a half-finished stacked rebase or fixing a bad conflict resolution from one.
---

# Stacked Rebase

Propagate a rewrite, or a merge into the base, through a stack of dependent branches. The
mechanics are done by `nu rb` (`nuno-cli`, engine at `lib/stack_rebase.py`); your job is
the judgement calls it refuses to make.

Do not hand-roll the git commands. The engine works out the cut point below every branch,
runs one `git rebase --onto <new parent> <cut> <branch>` per branch in the worktree that
holds it, and keeps each worktree's files in step with its branch.

```bash
nu rb plan [base] [branch...]  # show the stack, each branch's cut, and the rebases
nu rb -n   [base] [branch...]  # plan plus the precondition checks, rewrites nothing
nu rb -y   [base] [branch...]  # rebase, auto-resolving everything mechanical
nu rb report                   # re-print the current pause
nu rb verify                   # range-diff every rewritten branch against its backup
nu rb abort                    # abort and restore every branch and worktree
nu rb cleanup                  # drop backup refs and the run's state
```

Always pass `-y`: without it the command asks the operator for confirmation, and it will
refuse rather than block when there is no terminal. Ask the user yourself instead (step 2).

Exit codes: `0` done, `10` paused for a decision, `20` precondition failed.

## 1. Capture the conflict policy before starting

Parse any stated preference from the prompt ("prefer the new package naming", "keep the
audit entity as User", "branch A's version wins"). Restate it in one line and apply it
consistently. If no policy was given and the stack is large, ask for one — it is cheaper
than interrupting mid-run.

## 2. Plan, then confirm

Run `nu rb plan`. Positional arguments are the stack, bottom first, optionally led by the
base: `nu rb plan master b1 b2 b3`. A trunk given by its local name means its `origin/`
copy, fetched first (`--no-fetch` skips the fetch); a trunk anywhere else in the list is an
error. With no arguments the stack is walked from the current branch through the record in
`branch.<name>.nuParent`: down through recorded parents, up through branches that record it
as theirs. `nu wt add -b <local-branch>` writes that record, and so does every finished
`nu rb -y` — even one with nothing to rebase. A branch nothing records is a stack of one on
the base, so name a multi-branch stack explicitly the first time. A fork (two recorded
children) is an error that asks for the branches by name. User-given order always wins.

For every branch the plan prints its cut — the commit it was built on — and where that
came from:

| source | meaning |
|---|---|
| `tip of <parent>` / `recorded parent tip` | exact |
| `reflog of <parent>` | the parent was amended or squash-merged; newest old tip the branch still contains |
| `below <TICKET>: N commit(s) tagged …` | nothing remembers the parent; its leftover ticket commits are skipped |
| `merge-base with <base>` | weakest reading; say so, it can replay a merged parent's commits |
| `operator supplied` | `--old-tip <sha>` for the lowest branch |

`!!` marks a branch that will be rebased, `ok` one that already sits on its parent; once a
branch below is rebased, everything above it is `!!`.

Bottom branches already in the base show as
`-- <branch>  squash-merged into origin/master, dropped`: merged outright, or squash-merged
(GitLab's default) as detected by patch-id against the branch's own change. Keep a merged
branch until the rebase is done: its reflog is the exact anchor for the branch above. If it
is already gone, the ticket reading above usually still finds the cut.

The **ticket guard** compares the ticket id at the start of a branch name with the ids at
the start of its commit subjects; a branch whose name carries no ticket is not guarded. If
a branch would replay commits tagged with another ticket, the plan prints `ticket guard: …`
and the run refuses with exit 20. That is almost always a wrong cut: find the right one in
`git log --oneline <base>..<branch>` (the last commit of the ticket below) and pass
`--old-tip <sha>`. Pass `--no-ticket-guard` only when the user confirms those commits
belong to the branch.

**Get the user's confirmation of the plan before rewriting anything.**

Preconditions the engine enforces: every worktree a rebase runs in is clean and not already
mid-rebase. A branch no worktree holds is rebased in the worktree `nu rb` was run from,
which is switched back afterwards. Running from any worktree is fine.

## 3. Run the loop

`nu rb -y` rebases bottom first and resolves everything that has exactly one sane answer,
in this order:

1. **Recorded resolutions** — `rerere` is enabled per invocation. The same conflict in a
   later review round, or after an abort and retry, replays for free. Replayed paths are
   listed as such: they are a cache hit, not a fresh merge, so they are worth a glance.
2. **Lockfiles** — a private merge driver takes the replayed branch's side. Lockfiles are
   never mergeable line by line; the answer is always to re-resolve them with the
   dependency tool.
3. **Disjoint line edits** — git conflicts whole hunks, so an inserted import next to a
   renamed one collides even though the two edits touch different lines. The engine
   re-merges at line granularity and applies both.
4. **Generated files** — every path the repository's own `.gitattributes` marks with a
   `*generated*` attribute. Same line-level merge, because a clean merge of generated
   output beats an arbitrary side; if it clashes, or the file is over 20k lines, it takes
   the branch's side. Either way the path is reported as owing a regeneration, with the
   command from `.stack-rebase.json` if one is configured.

Everything else pauses with exit 10 and a report: the conflict narrowed to the lines that
genuinely clash, the three sides written out as files, a fix hint for index-level cases
(delete/modify, add/add, binary), and the **worktree** the conflict is in. Resolve and
`git add` there.

Then, per paused file:

- Resolve it if the captured policy covers it. The narrowed block is the whole decision —
  do not re-merge the rest of the file, the engine already did.
- `[regenerate]` means resolve by running the generator, not by merging text.
- If the conflict falls outside the policy, **stop and ask**. Show both sides and the file
  path. Do not extrapolate a policy to an unrelated conflict.
- An empty-commit pause is a real decision: its change already landed below. Never
  `git rebase --skip` without the user's confirmation — a skipped commit is silent data
  loss when the assumption is wrong.

`git add` each resolved path, then rerun `nu rb -y` from any worktree. Report every
auto-resolution the engine printed — the user gets to disagree with them.

## 4. Verify

A finished run ends with `verify`; run `nu rb verify` again any time. For every rewritten
branch it range-diffs the commits above its old cut against the commits above its new
parent, and flags changed commit counts or commits range-diff cannot pair. Commits of a
dropped squash-merged parent are below the cut, so they are not counted as lost.
Investigate a flag before pushing, with `git range-diff refs/stack-backup/<branch>...<branch>`.

Run builds or tests only if the user asked.

## 5. Push (only when asked)

Bottom of the stack upward, so each merge request's target branch exists before its source
moves:

```bash
git push --force-with-lease origin b1 b2 b3
```

Never bare `--force`. Push every rewritten branch, not just the tip. A dropped
squash-merged branch was not rewritten; do not push it.

## 6. Resume a half-finished run

`nu rb -y` picks up an unfinished run from any worktree: it continues the paused rebase in
its worktree, then the remaining branches. Mid-run it never asks for confirmation, since
there is nothing left to confirm. If this is a new session, re-ask for the conflict policy
first. `nu rb report` re-prints the pause without touching anything.

What was auto-resolved, replayed, or owes a regeneration is journalled in
`$GIT_COMMON_DIR/stack-rebase/journal.tsv`, so the final summary still names it after a
pause.

Backups live in `refs/stack-backup/<branch>` (invisible to `git branch`) and the run in
`$GIT_COMMON_DIR/stack-rebase/state.json`. Both survive across sessions; the engine never
overwrites an old backup with a post-rewrite SHA.

## 7. Clean up

Offer `nu rb cleanup` once the user confirms the stack is good; it refuses while a run is
unfinished. Never delete backups silently. Offer to delete the local branch of a dropped
squash-merged parent only now, after the rebase. The stack record in `branch.<name>.*` is
kept: it is what makes the next bare `nu rb` work.

## Repository policy (optional)

`.stack-rebase.json` at the repository root:

```json
{
  "takeTheirs": ["api/spec/v4/development/*.yaml"],
  "askAlways": ["**/db/migration/**"],
  "regen": { "api/spec/**": "make -C api build" }
}
```

`takeTheirs` extends the merge-driver patterns, `askAlways` forces a question, `regen` maps
paths to the command that rebuilds them. The ticket guard's pattern is
`git config nu.rb.ticketPattern` (default `[A-Za-z]+-[0-9]+`, empty disables).

## What the engine writes

Only the stack record: `branch.<name>.nuParent` and `nuParentTip`, when a run finishes.
`.gitattributes` and `.git/info/*` are left alone: every other behaviour change is passed
per invocation with `git -c`, against a private attributes file under
`$GIT_COMMON_DIR/stack-rebase/`.

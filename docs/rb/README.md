# nu rb — stacked branches

Splitting one big MR into a stack means every review round on an early branch,
and every merge into the base, leaves the branches above it sitting on a tip
that no longer exists. `nu rb` replays the stack branch by branch, each in the
worktree that holds it, and resolves the conflicts that have exactly one sane
answer.

```sh
nu rb master b1 b2 b3     # name the stack once: base first, then bottom to top
nu rb                     # afterwards, from any branch of the stack
nu rb -y                  # no question
nu rb -n                  # plan and check preconditions, change nothing
nu rb verify              # range-diff every rewritten branch against its backup
nu rb abort               # abort and restore every branch and worktree
nu rb cleanup             # drop the backup refs and the run's state
```

## The stack

A stack is recorded, not guessed. Every finished run writes each branch's
parent to the repository config (`branch.<name>.nuParent`, plus the parent's tip
as `nuParentTip`), and `nu wt add <branch> -b <local-branch>` records it when the
branch is created. A bare `nu rb` walks those records down and up from the
current branch. A branch nothing was recorded for is a one-branch stack on the
base; name the stack once to teach it more.

The base defaults to `origin/<default branch>` and is fetched first. A trunk
given by its local name (`nu rb master …`) means its `origin/` copy, because the
local one is usually behind. A trunk anywhere but first in the list is an error:
the base is never rewritten.

## Where each branch is cut

Each branch replays only its own commits: those above its *cut*, the commit it
was built on. The cut is the tightest of what is known — the parent's current
tip, the recorded parent tip, the newest tip in the parent's reflog that the
branch still contains (an amended or squash-merged parent). If none of that
exists, because the parent was deleted after its squash merge and never
recorded, the oldest commits tagged with other tickets are taken as the parent's
leftovers and the cut goes above them. Only then the merge-base.

Bottom branches that were squash-merged into the base share no commit with it,
so each is probed by patch-id: if its change since its cut is already a single
commit in the base, it is dropped, and the branch above is cut where it stood.

## Ticket guard

Branch names and commit subjects that start with a ticket id (`id-4427-…`,
`ID-4427 Follow …`) are compared: if a branch would replay commits tagged with
another ticket, the plan says so and the run refuses. That is the signature of
a wrong cut replaying a merged parent. `--old-tip <sha>` cuts the lowest branch
by hand; `--no-ticket-guard` replays anyway when the commits belong.

The pattern is `[A-Za-z]+-[0-9]+`; set `git config nu.rb.ticketPattern <regex>`
to change it, or to an empty string to turn the guard off. Branches whose name
does not start with a ticket are not guarded.

## Worktrees

One rebase runs per branch, bottom first, in the worktree that has the branch
checked out (`git -C <worktree> rebase --onto <new parent> <cut> <branch>`), so
every worktree's files follow its branch. A branch no worktree holds is rebased
in the one you ran `nu rb` from, which is switched back afterwards. Every
worktree involved must be clean.

A one-pass `--update-refs` rebase cannot do this: git silently leaves branches
that are checked out elsewhere where they were.

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
applied — together with the worktree it is in. Resolve there, `git add`, and
run `nu rb` again from any worktree; mid-run it resumes without asking.
`nu rb report` re-prints the conflicts of a paused run. `/rebase` in Claude Code
or omp drives the same engine when you would rather have an agent make those
calls.

Exit `10` is a decision request, not a failure — scripts and agents should
branch on it rather than treat it as an error.

## Safety

Backups go to `refs/stack-backup/<branch>`, invisible to `git branch`. The only
repository config written is the stack record above; `.gitattributes` and
`.git/info` are not touched. Every other git behaviour change is passed per
invocation with `git -c` against a private attributes file.

`nu rb verify` range-diffs every rewritten branch's own commits against its
backup — the check that the replay changed nothing but the base. `abort` aborts
the rebase in progress and moves every branch back, resetting the worktrees
that hold them; `cleanup` drops the backup refs and the run's state once you are
happy.

## Options

```
-y, --yes            do not ask before rewriting
-n, --dry-run        show the plan and check preconditions, change nothing
--base <branch>      what the stack sits on (default: origin/<default branch>)
--old-tip <sha>      cut point below the lowest branch, when the plan's is wrong
--no-fetch           do not fetch the base first
--no-ticket-guard    replay commits tagged with another ticket anyway
```

Subcommands: `plan`, `report`, `verify`, `abort`, `cleanup`.

## Per-repo configuration

An optional `.stack-rebase.json` at the repo root tunes conflict handling:

```json
{
  "takeTheirs": ["api/spec/v4/development/*.yaml"],
  "askAlways": ["**/db/migration/**"],
  "regen": { "api/spec/**": "make -C api build" }
}
```

## Tests

```sh
python3 -m unittest discover tests
```

They build throwaway repositories with an origin and run the engine end to end.

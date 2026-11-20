---
description: Rebase a stack of dependent branches, auto-resolving every mechanical conflict
argument-hint: "[base] [branch ...] [--old-tip <sha>] [policy in plain words]"
---

Rebase my stack of dependent branches after an earlier branch was amended, rewritten, or
merged into the base. Resolve everything that has exactly one sane answer; ask me only
about real decisions.

Arguments: $ARGUMENTS

Split them before running anything. The base (`master`, `main`, `origin/...`), local branch
names (bottom first), and `--` flags with their values are `<rb-args>` for `nu rb`.
Everything else is my conflict policy in plain words: it never goes on the command line.

Read `~/.agents/skills/stacked-rebase/SKILL.md` and follow it. Short version:

```bash
nu rb plan <rb-args>   # stack + each branch's cut + the rebases; show it, wait for my go-ahead
nu rb -y   <rb-args>   # rebase; exit 10 = paused for a decision, 0 = done and verified
```

While the loop runs:

- Restate my conflict policy in one line before you start, and apply it consistently.
- On exit 10, resolve only what my policy covers. The report's conflict block is already
  narrowed to the lines that genuinely clash — do not re-merge the rest of the file.
- `git add` each resolved path, then rerun `nu rb -y`.
- Anything outside my policy, an empty-commit pause, or a dropped commit: stop and ask me.
  Never `git rebase --skip` on your own.
- Report every auto-resolution, every rerere replay, and every generated file that owes a
  regeneration, so I can disagree.

Do not push, and do not run builds or tests, unless I ask.

# nuno-cli

Personal commands, dispatched git-style. `bin/nu` finds `libexec/nu-<command>`
and executes it — adding a command means dropping a file in `libexec/`, nothing
to register and nothing to build.

| command | docs |
|---|---|
| `nu wt` — git worktrees: create, list, clean up | [docs/wt/](docs/wt/) |
| `nu rb` — rebase a stack of dependent branches | [docs/rb/](docs/rb/) |

What each command does lives in [`docs/`](docs/). This file is about installing
the thing and working on it.

## Install

```sh
./install.sh
```

That symlinks `bin/nu` into `~/.local/bin`, links every skill in `agents/skills/` and
command in `agents/commands/` into `~/.agents/` (`NU_AGENTS_DIR` overrides it), and appends
one line to `~/.zshrc` which sources `shell/nu.zsh`. Use `./install.sh --no-shell` to skip
the last part and wire it up yourself. `NU_BINDIR` overrides the link target.

The shell part matters: a subprocess cannot change its parent shell's
directory. `nu` writes the directory it wants you in to `$NU_CD_FILE` and the
zsh function does the `cd`. Without it every command still works, it just
prints the `cd` for you to run. The function also defines `wt` as a shortcut
for `nu wt`, plus completion for both.

## Changing it

Edit and go — `~/.local/bin/nu` is a symlink into this repo, every
`libexec/nu-*` script is read from disk on each run, and the agent skills and
commands are symlinked too, so there is nothing to rebuild or reinstall. Change a
command's skill in the same commit as the command. Three things do not hot-reload:

- **`shell/nu.zsh`** — the `nu`/`wt` functions live in your shell's memory.
  `exec zsh` (or `source ~/.zshrc`) after touching it.
- **Moving this repo, or adding a skill or command** — the symlinks and the
  `~/.zshrc` line point at paths install time saw. Re-run `./install.sh`; it is
  idempotent.
- **New completions inside a command** — `nu help` and top-level completion
  enumerate `libexec/` on their own, but a new subcommand's own flags are
  listed by hand in `_nu`.

A new command is a new file:

```sh
printf '#!/usr/bin/env bash\n# summary: what it does\nset -euo pipefail\n' > libexec/nu-thing
chmod +x libexec/nu-thing
```

The `# summary:` line is what `nu help` prints. Source `lib/common.sh` from it
for colours, `confirm`, `die` and `request_cd`; it expects `$NU_LIB`, which the
dispatcher exports along with `$NU_ROOT`, `$NU_LIBEXEC` and `$NU_HOOKS`. A
command worth explaining gets a `docs/<command>/` directory.

## Layout

```
bin/nu                  dispatcher
libexec/nu-<command>    one file per command; `# summary:` shows up in `nu help`
lib/common.sh           colours, prompts, cd requests, repo resolution
lib/stack_rebase.py     the stacked-rebase engine behind nu rb
shell/nu.zsh            nu() wrapper, wt alias, completion
hooks/                  per-repo post-add hooks
agents/skills/<name>/   agent skills that drive nu commands, linked into ~/.agents/skills
agents/commands/        agent slash commands, linked into ~/.agents/commands
docs/<command>/         one directory per command
tests/                  python3 -m unittest discover tests
```

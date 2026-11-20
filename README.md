# nuno-plugins

Everything personal that a coding agent or terminal picks up, in one repo:

| directory | what | consumed by |
|---|---|---|
| [`cli/`](cli/) | the `nu` CLI (`wt`, `rb`, `rv`) and the skills and commands that drive it | shell, omp, Claude Code |
| [`skills/`](skills/) | standalone skills: `gcx`, `simple-english`, `find-skills` | omp, Claude Code |
| [`herdr-app/`](herdr-app/) | Herdr desktop app: the `worktrees` dialog mod, split scripts for diff and `nu rv` | Herdr app |
| `marketplace.json` | catalog of the `nu` and `skills` plugins | `omp plugin`, `claude plugin` |

## This machine

```sh
./install.sh
```

Symlinks `cli/bin/nu` into `~/.local/bin`, every plugin's skills and commands into
`~/.agents/` (omp and Claude Code both read from there), `herdr-app/mods/*` into
`~/.config/herdr-app/mods/`, and wires `cli/shell/nu.zsh` into `~/.zshrc`. Everything is a
symlink, so edits are live; re-run after moving the repo or adding a skill, command or mod.

## Another machine

```sh
omp plugin marketplace add git@github.com:nunofaria11/nuno-plugins.git
omp plugin install nu@nuno-plugins skills@nuno-plugins
claude plugin marketplace add nunofaria11/nuno-plugins
claude plugin install nu@nuno-plugins
herdr plugin install nunofaria11/nuno-plugins/herdr
```

The `nu` plugin's skills shell out to `nu`, so the CLI still needs `install.sh` (or a clone on
`PATH`). Do not install the marketplace plugins on a machine that also ran `install.sh`: the
skills would be registered twice.

## Herdr desktop app

The app (`herdr-app` + `herdr-core`) is not the `herdr` TUI: separate server, separate config
(`~/.config/herdr-app/`, not `config.toml`), and its UI is JavaScript mods that hot-reload.

### `worktrees` mod

One dialog, `mod.open:worktrees`, for the repo of the focused terminal. It lists the repo's
checkouts (open ones marked) and then every branch without one, local or on `origin`, most
recent first, and fetches in the background. Typing filters; a name nobody has becomes a
"new branch" row. Each branch row says where its checkout would land. It needs only git.

- Enter on a checkout: shows its space, or makes a `space.new` on it; the shipped `spaces`
  sidebar then nests it under the repo's main checkout like any worktree space.
- Enter on a branch or a new name: `git worktree add` the way `nu wt add` does it (existing
  local → checked out, `origin/<name>` → tracked, new → `--no-track` off `origin/<default>`),
  then a space on it. The `nu wt` post-add hook does not run.

Where the checkout lands, in order:

1. `git config nu.worktreesRoot` in the repo, used as-is; relative to the main checkout
   (`git config nu.worktreesRoot ../flip.worktrees` keeps the sibling layout).
2. `git config --global nu.worktreesRoot`, plus `/<repo>`.
3. The app's own rule: `worktrees.directory` from `~/.config/herdr-app/settings.json`
   (default `~/.herdr/worktrees`), plus `/<repo>`, same as the native "New worktree".

Branch slashes become dashes. `nu wt` reads the same config, so `wt cd`, `wt list` and
`wt clean` agree with the dialog in cases 1 and 2.

### Keys

In `~/.config/herdr-app/keys.json`, `global` section:

```json
"cmd-alt-o": "mod.open:worktrees",
"cmd-alt-c": "layout.toggle:panel",
"cmd-alt-l": {"command": "terminal.open", "args": {"place": "right", "argv": ["lazygit"]}},
"cmd-alt-i": {"command": "terminal.open", "args": {"place": "right", "argv": ["<repo>/herdr-app/bin/diff-base"]}},
"cmd-alt-r": {"command": "terminal.open", "args": {"place": "down", "argv": ["<repo>/herdr-app/bin/review"]}}
```

`layout.toggle:panel` shows or hides the Files/Changes panel; the Changes button in the tab bar
picks which. `diff-base` shows status and diff against the stack parent `nu wt` recorded, else
`origin/<default>`; `review` runs `nu rv` and waits for Enter, since a split closes when its
program exits.

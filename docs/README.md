# nu — command reference

One directory per command. `nu <command> --help` prints the same options; these
pages explain the reasoning behind them.

| command | what it does | docs |
|---|---|---|
| `nu wt` | git worktrees — create, list, clean up | [wt/](wt/) |
| `nu rebase` | rebase a stack of dependent branches, resolving what is mechanical | [rebase/](rebase/) |

`nu help` lists whatever is in `libexec/` right now, which is the authoritative
answer to "what commands exist". Installation and development notes are in the
[repository README](../README.md).

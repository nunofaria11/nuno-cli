# nu rv — reviews

A review starts with the same chores every time: find the merge request's
target, fetch the branch, put it somewhere that does not disturb your own
work, and work out where to start reading. `nu rv` does those, and the
`branch-review` agent skill builds the review on top of them.

```sh
nu rv                     # the current branch, in place
nu rv id-4959-fix-thing   # a branch, local or on origin
nu rv !22670              # a merge request of origin (or #123 / 123 on GitHub)
nu rv https://gitlab.com/flipcorp/flipnext/flip/-/merge_requests/22670
nu rv render ~/.cache/nu/rv/flip/<branch>/review.json
```

`nu rv <target>` is short for `nu rv prep <target>`.

## prep

1. **Resolve.** A link or number is looked up with `glab` or `gh`. A bare branch
   is matched to its open merge request when there is one. A link to another
   repository is refused: run it from that repository's checkout.
2. **Base.** The merge request's target branch, so a stacked MR shows only its
   own change. Without an MR, the parent that `nu rb` recorded, else
   `origin/<default>`. `--base` overrides both.
3. **Fetch** the branch and the base, nothing else (`--no-fetch` skips it).
   MRs from forks are fetched from `refs/merge-requests/<n>/head` (GitHub:
   `refs/pull/<n>/head`) into a local `mr-<n>-<branch>`.
4. **Worktree** through `nu wt add`, so it lands in `<repo>.worktrees/` and the
   repo's post-add hook runs. A worktree that already holds the branch is
   reused and fast-forwarded when it is clean. A local branch that is ahead of
   the remote, has diverged, or has uncommitted changes is left alone and
   reviewed as it is, with a warning.
5. **Capture** into `~/.cache/nu/rv/<repo>/<branch>/` (`$XDG_CACHE_HOME`
   respected): `diff.patch` from the merge base, and `meta.json` with the MR,
   the ticket id, the commits and every file with its kind. A new prep deletes
   the previous `review.json` and `review.html`, which described an older head.

It then prints the size, the verdict and a reading order, and cds into the
worktree when the shell wrapper is loaded.

### Simple or complex

The review is complex, and the skill writes an HTML page instead of a chat
reply, past any of these. Noise files (lockfiles, translations, changelog
entries, generated files) do not count.

| limit | value |
|---|---|
| files to review | more than 8 |
| changed lines | more than 400 |
| top-level areas (`backend/`, `console/`, …) | more than 1 |

The skill also switches to HTML when it has more than 4 findings. `--html` and
`--inline`, given to the skill, override both.

### Kinds and the reading order

Each file gets one kind, from its name: `spec` (OpenAPI, proto, GraphQL),
`entry` (controllers, delegates, listeners, components, CLIs), `source`,
`outbound` (repositories, persistence, clients, stores), `migration`, `config`,
`test`, `docs`, or a noise kind. A file the repo marks `linguist-generated` or
`gitlab-generated` in `.gitattributes` is generated. The reading order groups
the kinds in that sequence and sorts each group by churn. It is a starting
point: the agent confirms the real entry point from the diff.

## render

The agent writes `review.json` next to `meta.json`. `nu rv render` checks it
against the captured diff and refuses, with exit 2 and a list of errors, when:

- a step names a file that is not in the diff, or names one twice,
- a changed file that is not noise is in no step,
- a finding lacks a field, has an unknown severity or section, or its
  `comment` does not start with a [Conventional Comments](https://conventionalcomments.org/)
  label.

The page is one self-contained file. Mermaid diagrams load mermaid from a CDN
and fall back to their source text offline.

- **Top:** title, MR link, range, verdict, what changed, the ticket's acceptance
  criteria, and every finding ordered by severity.
- **Steps:** the agent's reading order. Each step has its explanation, then the
  diffs of its files with each finding placed under the line it is about.
- **Findings:** each has a code, the severity's letter and its count among
  findings of that severity in `review.json` order (`C1`, `H1`, `H2`, `M1`), and
  `#H2` links to it. Each has a *Copy comment* button for the GitLab comment, a
  *Fix locally* button that copies a self-contained fix prompt for an agent
  (code, worktree, location, problem, why, fix), and an *Open in IDE* link.
  `j` / `k` jump between findings.
- **IDE links:** Kotlin and Java files open in IntelliJ (`idea://`). TypeScript,
  HTML and styles open in VS Code (`vscode://`). Any other file goes to whichever
  of the two its nearest build file points at: Gradle or Maven means IntelliJ,
  `package.json` means VS Code. Clicking a new-side line number opens that line.
- **Diffs:** unified. Noise files, and files with more than 800 diff lines, start
  collapsed.

The layout borrows from Trail of Bits'
[review-walkthrough](https://github.com/trailofbits/skills/tree/main/plugins/review-walkthrough)
(steps in reading order, verbatim diffs, a renderer that validates the agent's
JSON) and CodeRabbit's
[Change Stack](https://docs.coderabbit.ai/pr-reviews/coderabbit-review)
(an overview of what needs attention, then layers in explanation order).

## Cleanup

Review worktrees are ordinary worktrees. `nu wt clean` removes them once the
branch is merged or deleted upstream. The cache directory is never cleaned;
delete `~/.cache/nu/rv` whenever you like.

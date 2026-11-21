---
name: branch-review
description: Intensive code review of a git branch or merge request against its target branch. Use this skill whenever the user asks to review a branch, MR or PR — the current one ("review this", "review my branch", "review my MR/PR", "what did I change", or just "review" in a git repo), a named one ("review branch X", "code review branch pla-1234-foo"), or a link ("review https://gitlab.com/.../merge_requests/123", "review !123"). Checks the change out in its own worktree, gives reading pointers first, then a deep review focused on real bugs, unintended behavior, and project-standards violations — never cosmetic or naming-pad items. Simple reviews land in chat; complex ones become a page where the harness can publish one.
---

# Branch Review

Perform an intensive, opinionated code review of a branch against the branch it merges into. The review must do more than read the diff: understand *what was added and why*, map the *blast radius*, and check that new code matches the conventions actually in use in neighboring code — not just what is documented.

The user reviews by hand at the same time. Give them reading pointers fast, then the full review.

The mechanics are done by `nu rv` (`nuno-cli`, engine at `lib/review.py`). Do not hand-roll fetches, worktrees or base detection.

```bash
nu rv prep [target]          # fetch, worktree, meta.json + diff.patch, reading order
nu rv check <review.json>    # validate review.json, write bundle.json for presenting
```

Exit codes: `0` done, `2` review.json invalid (fix what the errors name and rerun), `20` precondition failed.

## Inputs

Split the user's arguments:

- **Target** — nothing (the current branch, reviewed in place), a branch name, `!123` / `123`, or a GitLab MR / GitHub PR URL. Passed to `nu rv prep` as is.
- **Format override** — `--page` or `--inline`. Never passed to `nu rv`; it decides the form in `Presenting`.
- **Base override** — "against X" becomes `--base X`. Otherwise `nu rv` picks the base: the MR's target branch, else the stack parent `nu rb` recorded, else `origin/<default>`. Never pick a base yourself.

Run `nu rv prep <target> --no-cd`. It prints the worktree, the diff range, the size, a `simple`/`complex` verdict, a heuristic reading order, warnings, and the path to `meta.json`. Read `meta.json`: it holds the MR title, description and author, the ticket id, the commits, every changed file with its kind (`source`, `entry`, `test`, `spec`, `migration`, `config`, `docs`, or noise: `lockfile`, `i18n`, `changelog`, `generated`), and `paths` (`patch`, `review_json`, `bundle`, `page`).

From here on, **work in the worktree** (`meta.worktree`). Every file read, grep and `git -C` uses that path, never the main checkout. The diff is `meta.paths.patch`; the range is `meta.base.merge_base..meta.head.sha`.

If `nu rv prep` fails, stop and report its message. Do not work around it with your own git commands.

Never use the `worktree` plugin skill here: it puts worktrees inside the repository, against the user's rule.

## Process

Run these phases in order. Do not skip the investigate phase even if the diff is small.

### Phase 1 — Pointers (fast)

The user starts reading now, so this comes before the deep work. Budget: `meta.json`, the patch, and at most a few targeted reads. Do not wait for sub-agents.

Use the heuristic reading order as a starting point, not the answer: it groups files by name patterns. Confirm the real entry point from the patch.

Post the pointers without ending your turn. In omp, write them to `out://`. In Claude Code, write them as a message before the next tool call.

```
**Start here** · <MR ref or branch> · <N files, +A −D> · <simple|complex>

Entry point
- `UserController.kt`, `createUser()`: the new endpoint; everything else hangs off it.

Data flow
- `CreateUserDelegate.kt` → `UserService.create()` → `UserRepository.save()`: validation moved into the service.
- `V312__add_user_flag.sql`: new non-null column with a default.

Tests
- `UserControllerIT.kt`: covers the happy path and the duplicate email.
- No test for the new flag's default.

Skip: lockfile, i18n, changelog (3 files).
```

One to three bullets per heading. Each bullet names a file, a place in it, and one reason to look. Follow `How to point at code`.

### Phase 2 — Gather and understand

- Read the commits and the MR description from `meta.json`. They are the author's statement of intent.
- **Ticket.** When `meta.ticket` is set, fetch it with the Linear MCP (`get_issue`). Pull its acceptance criteria. If the ticket has none, or Linear is unreachable, say so in one clause under `What changed` and go on. Read only: never comment on, update or move the ticket.
- Read each non-trivial changed file **in full** in the worktree, not only the hunk. A bug in a 3-line change often only makes sense in context.
- Note any tests added or changed. They often state the intended contract more clearly than the production code.

Build an internal summary (do not show it yet): what changed, the author's apparent goal, and the blast radius (which modules, services or apps could be affected).

### Phase 3 — Investigate (parallel sub-agents)

This phase makes the review intensive. Discover the conventions *actually in use* in the surrounding code, and find every place a changed symbol is consumed.

For each changed file, or each group of closely related files on a large branch, dispatch one investigation sub-agent in parallel.

**Run every sub-agent on Opus.** The default exploration agents run on Haiku, which misses the subtle convention and call-site findings this phase exists for.

- **Claude Code:** use the `Agent` tool with `subagent_type: "Explore"` and `model: "opus"`.
- **omp:** use `agent: "branch-investigator"`. This is a read-only user agent pinned to `@default` (Opus). Never use `scout`, `sonic` or `task` for this phase.

**Brief template for each investigation agent:**

> Investigate `<changed file path>` in the worktree `<meta.worktree>`, on branch `<branch>`. Run every command and read every file inside that worktree. The change is `<merge_base>..<head sha>`. Two jobs, both required:
>
> 1. **Conventions.** Find 3-5 sibling or similar files in the same area of the codebase (same directory, same layer, same kind of artefact — Angular component, Kotlin domain service, API endpoint, test spec, whatever fits the repo). Extract the conventions actually in use: state management style, change detection, dependency injection wiring, error handling pattern, test layout, naming. Report what the conventions are, and which of them the changed file violates or follows.
> 2. **Impact map.** For each new or modified exported symbol in `<changed file path>`, grep the worktree for call sites and report them. Flag any call site whose behaviour could change because of the diff.
>
> Be concrete: file paths, line numbers, short snippets. Do not pad with cosmetic observations.

Dispatch sub-agents in parallel: several `Agent` tool uses in one message in Claude Code, or one `task` call with several `tasks[]` items in omp. Cap concurrency at **6 in-flight**; if more files need investigation, run a second wave. Group very small, clearly related files under one sub-agent.

Skip a sub-agent only for files `meta.json` marks as noise, and for trivially obvious ones such as pure version bumps. Note each skip internally, so the synthesis does not claim coverage it did not have.

Treat sub-agent results as inputs to your own analysis. "No issues" from a sub-agent does not mean there are none. Re-read the diff yourself with the conventions it surfaced in mind.

### Phase 4 — Synthesize

Combine the diff, the Phase 2 understanding, the ticket, and all Phase 3 reports. Rank findings by severity:

- **Critical** — will break production, lose data, or violate a hard rule stated in the repo's `CLAUDE.md` / `AGENTS.md` (e.g., hand-editing generated translation files).
- **High** — a real bug or definite unintended behaviour, but contained.
- **Medium** — a likely bug, an impact site that probably needs attention, or a clear standards mismatch with surrounding code.
- **Low** — a smaller concern still worth raising.

A ticket acceptance criterion the change does not meet is a finding with `section: "behavior"`: High when it is missing, Medium when it is partly met.

Do not invent severities to fill space. **An empty finding list is the correct outcome when nothing was found.**

### Phase 5 — Deliver

1. Write `review.json` to `meta.paths.review_json`. The schema is in `The review file`.
2. Run `nu rv check <that path>`. Fix every error it names and rerun until it exits 0. It writes `bundle.json` to `meta.paths.bundle`: your review plus what a presenter derives from it, computed once. That is the finding codes, the diff rows of each finding (`excerpt`), a self-contained fix prompt per finding (`fix_prompt`), IDE links, the severity counts, and a catch-all step for noise files.
3. Present `bundle.json`, as described in `Presenting`.

## Presenting

`nu rv` does not render anything. You present `bundle.json` in the form your harness does best. Every form carries the same content:

- The verdict first: one sentence that names the main reason.
- Every finding, by code: its title and severity, the problem, where it is, why it is a problem, and the fix when you know one.
- What you checked and found clean (`clean`). Then an empty finding list reads as evidence, not silence.
- The ticket: one line when every criterion is met, else each criterion that is not met.
- The path of `bundle.json`.

Pick the form:

1. `--page` or `--inline` from the user wins.
2. Otherwise a page when `meta.complexity.verdict` is `complex` or there are more than 4 findings, and the harness can publish one (in Claude Code, the `Artifact` tool).
3. Otherwise inline.

**Inline.** Markdown in the chat or terminal. Answer first. Use no fixed headings and no table for a short review. Give each finding as its bold code and title with the severity, then two or three short lines: the problem, where, why, fix. Close with the clean list and the ticket line.

**Page.** Build it from `bundle.json`, write it to `meta.paths.page`, and publish it with the harness's own tool, following the harness's design guidance. The page holds, in this order:

- a header: the MR ref and link, the ticket, the range, the size
- the verdict and the severity counts
- one card per finding: code, title, severity, body, where, why, fix, its `excerpt` with the `hit` rows marked, and two copy buttons, one for `comment` and one for `fix_prompt`
- the clean list
- the ticket criteria
- the reading order: each step's title, explanation and files, with the finding codes on each file, and its diagram when it has one

Copy code into the page only from `excerpt` rows, verbatim. Never retype code from memory. Do not inline whole diffs, because the page would be huge and the reader has the worktree. Leave `ide_url` out of a hosted page, because hosted pages block `idea://` and `vscode://` links. After publishing, reply in chat with only the verdict sentence, the counts, one line per finding (code and title), and the link.

### Finding codes

Every finding has a code the user can refer to, such as "fix H2". The code is the severity's letter (`C`ritical, `H`igh, `M`edium, `L`ow) and the finding's count among the findings of that severity, in `review.json` order: `C1`, `H1`, `H2`, `M1`. `nu rv check` writes them into `bundle.json`; use those codes in every form. List the findings worst first, so the codes read in order. When the user later names a code, it means that finding.

## The review file

`review.json` holds the review as data. `nu rv check` validates it against the diff and derives everything else. You write only prose and anchors, never diff text.

```json
{
  "verdict": {"decision": "fix", "text": "Merge after the fixes below. The retry loop never stops."},
  "summary": "What changed: 2-4 short sentences: what the branch adds, why, what else it touches, and whether it meets the ticket.",
  "intent": {
    "ticket": "ID-4959",
    "url": "https://linear.app/...",
    "criteria": [{"text": "Retries stop after five tries", "status": "missing", "note": "`_retry()` has no limit."}]
  },
  "clean": ["The three call sites of `fetchAll()` handle the new null result."],
  "steps": [
    {"title": "Entry point: the new endpoint", "explanation": "50-150 words.", "files": ["path/A.kt"],
     "diagram": "flowchart LR\n  A[Controller] --> B[Service]"}
  ],
  "findings": [
    {"title": "The retry loop never stops", "severity": "critical", "section": "bugs",
     "body": "The client retries forever when the server answers with a 500 error.",
     "where": "`api_client.dart`, in `_retry()`",
     "why": "The three other clients in the same folder use `BackoffPolicy`.",
     "fix": "Use `BackoffPolicy.exponential` and stop after five tries.",
     "file": "lib/api_client.dart", "line": 142, "end_line": 150, "side": "new",
     "comment": "issue (blocking): `_retry()` never stops on a 500 error. Use `BackoffPolicy.exponential` and stop after five tries, like the other clients here."}
  ]
}
```

- `verdict.decision`: `merge`, `fix` or `block`.
- `intent`: omit when there is no ticket. `status` is `met`, `partial`, `missing` or `unclear`.
- `clean`: optional. Each entry is one thing you checked and found sound, with its evidence in one sentence. List only checks you ran, never generic reassurance.
- `steps`: the reading order. Use the user's three stages as the backbone: entry point first, then the data flow in the order data moves, then tests. Larger changes split the data flow into more steps. Each step covers one concept, puts definitions before their consumers, and keeps a contract with its implementation. Every changed file belongs to exactly one step. Noise files may be left out; `nu rv check` collects them. `diagram` is optional Mermaid; add one only when the data flow crosses three or more components.
- `findings`: `section` is `bugs` (a real or likely bug), `behavior` (unintended behaviour, including an unmet ticket criterion) or `standards` (a mismatch with the conventions in use). `severity` is `critical`, `high`, `medium` or `low`. Do not write codes; `nu rv check` derives them. `file` is the repo-relative path. `line` and `end_line` are line numbers in the new file (`side: "new"`, the default) or the old file for removed lines (`side: "old"`). When the diff shows the line, the bundle carries an `excerpt` and `placement: "diff"`. Otherwise `placement` is `file` (a changed file, a line outside the hunks) or `outside` (a call site in an unchanged file).
- Write `body`, `why` and `fix` to stand alone, because `fix_prompt` is built from them.
- Prose fields are plain text. Use backticks for identifiers. Separate paragraphs with a blank line; lines starting with `- ` are list items.

### The `comment` field

Each finding carries the comment the user pastes into GitLab. It is copied as is, so write it for the MR author:

- Start with a [Conventional Comments](https://conventionalcomments.org/) label: `issue`, `suggestion`, `question`, `todo`, `nitpick`, `thought`, `chore`, `note` or `praise`, with a decoration when it helps. Critical and High are `issue (blocking)`. Medium is `issue` or `suggestion (non-blocking)`. Low is `suggestion (non-blocking)` or `question`.
- One to three sentences: what goes wrong, then what to do. Follow `How to write the review`.
- Address the author directly. No severity words, no "the reviewer", no file path (the comment sits on the line).

## How to write the review

The reader is a tired engineer who wants to know what to do. Write plain English.

- Lead with the answer, in every section and in every finding. The problem first, the detail second.
- One idea per sentence. Keep sentences under 20 words.
- Use active voice and simple tenses, and name the actor: "the widget rebuilds on every frame", not "rebuilds are triggered".
- Take the plain word over the jargon: "use", not "utilize"; "start", not "initiate". Define a term at its first use, in under ten words.
- Say what breaks and when: "the upload fails for files larger than 5 MB", not "possible edge case in the upload path".
- Use only `can`, `will`, and `must`. Drop "should", "may", "might", "could", "it is important to note", and "consider".
- Do not splice two statements with an em-dash. Name the relation ("because", "but") or write two sentences.
- No emoji, no decorative bold, no closing summary paragraph.

## How to point at code

Name the place in words. Do not paste the diff.

- Give the file name, not the full path: `post_feed_cell.dart`, not `apps/mobile/lib/src/features/news/presentation/widgets/post_feed_cell.dart`. Add one parent folder only when two files share a name.
- Say where inside the file in human terms: "in `build()`", "in the error branch of `save()`", "in the new test for empty input".
- A line number is optional and follows the file name: "`api_client.dart`, line 142". Never write `path/to/file.dart:142`.
- Keep identifiers, flags, commands, and error text exact and in backticks.
- No code blocks, no diff hunks, no patches. Describe the code in words. One short quoted line is permitted when the exact text is the point, such as a wrong key or a wrong flag.
- Sub-agent reports are your evidence, not your output. Never copy their path lists, line dumps, or snippets into the review.

The `file` and `line` fields of `review.json` are anchors for `nu rv check`, not prose. They take the full repo-relative path.

## Hard rules

These exist to keep the review useful. Violating them silently undermines the skill's value.

1. **No padding.** Do not list naming nits, formatting opinions, "consider extracting", "could be more readable", or other cosmetic items framed as numbered issues. Real bugs, real unintended behavior, real standards mismatches — nothing else.
2. **No fabrication.** Every standards-violation finding must be backed by *an example of the convention in the actual codebase*, surfaced in Phase 3. Do not assert "the convention is X" from memory or assumption.
3. **Empty is allowed.** When nothing was found, say so and show the clean list. Do not invent a Low item to avoid an empty review.
4. **Two file writes at most.** You write `review.json` at `meta.paths.review_json`, and `page.html` at `meta.paths.page` when you present a page. Do not edit code, do not create other review files, do not commit, do not push. Fixing is a separate request outside this skill.
5. **Git writes go through `nu rv prep`.** It is the only thing that fetches, creates worktrees or moves branches. Your own git commands are read-only: `git diff`, `git log`, `git show`, `git branch`, `git status`, `git rev-parse`, `git blame`.
6. **Stick to the diff.** Do not turn the review into a request for unrelated refactoring. If the surrounding code is bad but the diff didn't touch it, leave it alone.
7. **Respect the project's own rules** when they're visible. The repo's `CLAUDE.md` / `AGENTS.md` files state hard rules (e.g., translation files are generated, do not skip the changelog, do not modify generated OpenAPI). Treat violations of those as Critical.
8. **Read, never post.** Reading the MR (through `nu rv prep`) and the Linear ticket is allowed. Never post comments, approve, change labels, or update the ticket. The user posts what they choose.
9. **Plain English, human first.** Follow `How to write the review` and `How to point at code`. A review packed with long paths, line-number lists, and pasted diff hunks fails this skill even when every finding in it is true.

## Behaviour around large branches

When the branch has many changed files (rough threshold: >20):

- Still run every phase.
- In Phase 3, group related files (same directory, same feature) under one sub-agent brief instead of one per file.
- Cap total sub-agents at ~12 across the whole review. If that is still not enough, prefer breadth over depth (one sub-agent per major area). Then say under `What changed` that the review covered breadth, not depth.

## Behaviour around trivial branches

When the diff is tiny (a few lines, one file, obvious config change):

- Phases 1, 2, 4 and 5 still run. The pointers can be a single line.
- Phase 3 can be a single sub-agent, or you can skip it when the change has no exported surface and no consumers. If you skip it, say so in one short clause under `What changed`.

## What "intensive" means here

Intensive does not mean *long*. It means:

- Read whole files, not only hunks.
- Look at neighbouring code to know the conventions, not just the documented ones.
- Trace call sites, not just inspect the changed lines.
- Check the change against what the ticket asked for.
- Then say only what is real, in the smallest number of words that convey it.

A two-line review can be intensive. A two-page review padded with style nits is not.

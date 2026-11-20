#!/usr/bin/env python3
"""review: prepare a branch or merge request for review, and render the review page.

prep    Resolve the target (current branch, branch name, GitLab MR, GitHub PR), fetch
        it, check it out in a worktree next to the repository, and write meta.json and
        diff.patch to the review directory. Prints the size, the simple/complex
        verdict and a first reading order.
render  Validate the review.json an agent wrote against the captured patch, then
        write one self-contained review.html next to it.

The diff is always taken against the merge request's target branch when there is
one, so a stacked merge request shows only its own change.

Exit codes:
    0   done
    2   review.json is invalid (every error printed on stderr)
    20  precondition failed
"""

from __future__ import annotations

import argparse
import fnmatch
import html
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import NoReturn
from urllib.parse import quote

# A review is "complex", and gets the HTML page, past any of these.
MAX_FILES = 8
MAX_LINES = 400
MAX_AREAS = 1

COLLAPSE_LINES = 800

TTY = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def paint(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if TTY else text


def die(msg: str, code: int = 20) -> NoReturn:
    print(f"nu: {msg}", file=sys.stderr)
    sys.exit(code)


def warn(msg: str) -> None:
    print(f"{paint('33', 'warn:')} {msg}", file=sys.stderr)


# --------------------------------------------------------------------------- shell


def attempt(args: list[str], cwd: Path | None = None, stdin: str | None = None) -> tuple[int, str, str]:
    try:
        p = subprocess.run(args, cwd=cwd, input=stdin, text=True, capture_output=True)
    except FileNotFoundError:
        return 127, "", f"{args[0]} not found"
    return p.returncode, p.stdout, p.stderr


def must(args: list[str], cwd: Path | None = None, stdin: str | None = None) -> str:
    rc, out, err = attempt(args, cwd, stdin)
    if rc != 0:
        die(f"`{' '.join(args)}` failed: {(err or out).strip()}")
    return out


def git(repo: Path, *args: str) -> str:
    return must(["git", "-C", str(repo), "-c", "core.quotePath=false", *args])


def git_ok(repo: Path, *args: str) -> bool:
    return attempt(["git", "-C", str(repo), *args])[0] == 0


def rev(repo: Path, ref: str) -> str | None:
    rc, out, _ = attempt(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"])
    return out.strip() if rc == 0 else None


# --------------------------------------------------------------------------- repo


REMOTE_PATTERNS = (
    re.compile(r"^(?:ssh://)?[^@/\s]+@(?P<host>[^:/]+)(?::\d+)?[:/](?P<path>.+?)(?:\.git)?/?$"),
    re.compile(r"^https?://(?:[^@/]+@)?(?P<host>[^/:]+)(?::\d+)?/(?P<path>.+?)(?:\.git)?/?$"),
)


def origin(main: Path) -> tuple[str, str, str | None]:
    """(host, project path, forge) of the origin remote; forge is gitlab, github or None."""
    rc, out, _ = attempt(["git", "-C", str(main), "remote", "get-url", "origin"])
    if rc != 0:
        die("the repository has no 'origin' remote")
    url = out.strip()
    for pattern in REMOTE_PATTERNS:
        m = pattern.match(url)
        if m:
            host, path = m["host"].lower(), m["path"]
            forge = "github" if "github" in host else "gitlab" if "gitlab" in host else None
            return host, path, forge
    return "", url, None


def main_worktree() -> Path:
    if not git_ok(Path.cwd(), "rev-parse", "--is-inside-work-tree"):
        die("not inside a git repository")
    out = git(Path.cwd(), "worktree", "list", "--porcelain")
    return Path(out.split("\n", 1)[0].removeprefix("worktree "))


def worktree_for(main: Path, branch: str) -> Path | None:
    path = None
    for line in git(main, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            path = Path(line[9:])
        elif line == f"branch refs/heads/{branch}":
            return path
    return None


def default_branch(main: Path) -> str:
    rc, out, _ = attempt(["git", "-C", str(main), "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"])
    if rc == 0:
        return out.strip().removeprefix("refs/remotes/origin/")
    for candidate in ("main", "master"):
        if rev(main, f"origin/{candidate}") or rev(main, candidate):
            return candidate
    die("cannot determine the default branch")


def clean(worktree: Path) -> bool:
    return not git(worktree, "status", "--porcelain", "--untracked-files=no").strip()


def is_ancestor(repo: Path, a: str, b: str) -> bool:
    return git_ok(repo, "merge-base", "--is-ancestor", a, b)


# --------------------------------------------------------------------------- forges


MR_URL = re.compile(r"^https?://(?P<host>[^/]+)/(?P<path>.+?)/-/merge_requests/(?P<n>\d+)")
PR_URL = re.compile(r"^https?://(?P<host>[^/]*github[^/]*)/(?P<path>[^/]+/[^/]+)/pull/(?P<n>\d+)")
NUMBER = re.compile(r"^[!#]?(?P<n>\d+)$")

GH_FIELDS = "number,title,body,url,author,isDraft,state,headRefName,baseRefName,headRefOid,isCrossRepository"


def from_gitlab(d: dict) -> dict:
    return {
        "forge": "gitlab",
        "number": d["iid"],
        "ref": f"!{d['iid']}",
        "url": d.get("web_url"),
        "title": d.get("title") or "",
        "description": d.get("description") or "",
        "author": (d.get("author") or {}).get("username"),
        "draft": bool(d.get("draft")),
        "state": d.get("state"),
        "source_branch": d["source_branch"],
        "target_branch": d["target_branch"],
        "head_sha": d.get("sha"),
        "cross_repo": d.get("source_project_id") != d.get("target_project_id"),
    }


def from_github(d: dict) -> dict:
    return {
        "forge": "github",
        "number": d["number"],
        "ref": f"#{d['number']}",
        "url": d.get("url"),
        "title": d.get("title") or "",
        "description": d.get("body") or "",
        "author": (d.get("author") or {}).get("login"),
        "draft": bool(d.get("isDraft")),
        "state": (d.get("state") or "").lower(),
        "source_branch": d["headRefName"],
        "target_branch": d["baseRefName"],
        "head_sha": d.get("headRefOid"),
        "cross_repo": bool(d.get("isCrossRepository")),
    }


def fetch_mr(forge: str, project: str, number: int) -> dict:
    if forge == "gitlab":
        out = must(["glab", "mr", "view", str(number), "-R", project, "--output", "json"])
        return from_gitlab(json.loads(out))
    out = must(["gh", "pr", "view", str(number), "-R", project, "--json", GH_FIELDS])
    return from_github(json.loads(out))


def find_mr(forge: str | None, project: str, branch: str) -> dict | None:
    """The open merge request whose source is `branch`, or None. Never fatal."""
    if forge == "gitlab":
        cmd = ["glab", "mr", "list", "--source-branch", branch, "-R", project, "--output", "json"]
    elif forge == "github":
        cmd = ["gh", "pr", "list", "--head", branch, "-R", project, "--json", GH_FIELDS, "--limit", "1"]
    else:
        return None
    rc, out, err = attempt(cmd)
    if rc != 0:
        warn(f"could not look up a merge request for {branch}: {(err or out).strip()}")
        return None
    try:
        found = json.loads(out or "[]")
    except json.JSONDecodeError:
        warn(f"unexpected output from {cmd[0]}; reviewing without a merge request")
        return None
    if not found:
        return None
    return from_gitlab(found[0]) if forge == "gitlab" else from_github(found[0])


# --------------------------------------------------------------------------- classification


LOCKFILES = {
    "package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml", "bun.lock",
    "bun.lockb", "Cargo.lock", "poetry.lock", "uv.lock", "Pipfile.lock", "Gemfile.lock",
    "composer.lock", "pubspec.lock", "go.sum", "gradle.lockfile", "verification-metadata.xml",
}

# Patterns match against "/" + path, and fnmatch's * spans directories.
NOISE = {
    "i18n": ("*/i18n/*.json", "*.arb", "*/l10n/*", "*/locales/*.json"),
    "changelog": ("/.changelog/*", "/CHANGELOG*"),
    "generated": ("*/generated/*", "*/gen/*", "*.g.dart", "*.freezed.dart", "*.gr.dart",
                  "*.pb.go", "*_pb2.py", "*/__snapshots__/*", "*.snap", "*.min.js", "*.min.css"),
}
KINDS = (
    ("test", ("*Test.kt", "*Tests.kt", "*IT.kt", "*Test.java", "*Tests.java", "*IT.java",
              "*.spec.ts", "*.test.ts", "*.spec.tsx", "*.test.tsx", "*.spec.js", "*.test.js",
              "*_test.dart", "*_test.go", "*/test_*.py", "*_test.py", "*/src/test/*",
              "*/src/integrationTest/*", "*/src/testFixtures/*", "*/e2e/*", "*/tests/*",
              "*/test/*", "*/__tests__/*")),
    ("migration", ("*/db/migration/*", "*/migrations/*", "*.sql")),
    ("spec", ("*/api/*.yaml", "*/api/*.yml", "*/api/*.json", "*openapi*.yaml", "*openapi*.yml",
              "*openapi*.json", "*.proto", "*.graphql", "*.graphqls")),
    ("config", ("*.gradle", "*.gradle.kts", "*/gradle.properties", "*/package.json", "*Makefile",
                "*Dockerfile*", "*.gitlab-ci.yml", "*/application*.yml", "*/application*.yaml",
                "*/application*.properties", "*/values*.yaml", "*/Chart.yaml", "*/tsconfig*.json",
                "*/project.json", "*/nx.json", "*/angular.json", "*.toml", "*/pubspec.yaml",
                "/.github/*", "*/install.sh")),
    ("docs", ("*.md", "*.mdx", "*.adoc", "*/docs/*")),
)
ENTRY = ("*Controller.kt", "*Controller.java", "*Delegate*.kt", "*Resource.kt", "*Handler.kt",
         "*Listener.kt", "*Consumer.kt", "*Job.kt", "*Scheduler.kt", "*/adapter/incoming/*",
         "*.component.ts", "*.page.ts", "*.routes.ts", "*routing*.ts", "*/main.ts", "*/main.dart",
         "*/main.py", "*/__main__.py", "*/bin/*", "*/libexec/*", "*/cmd/*", "*_screen.dart",
         "*_page.dart", "*/router*.dart")
OUTBOUND = ("*Repository.kt", "*Repository.java", "*/persistence/*", "*/adapter/outgoing/*",
            "*Entity.kt", "*Dao.kt", "*.store.ts", "*.reducer.ts", "*.effects.ts", "*/client/*")

READING_ORDER = (
    ("Contracts", ("spec",)),
    ("Entry points", ("entry",)),
    ("Core logic", ("source",)),
    ("Persistence and outbound", ("outbound", "migration")),
    ("Wiring and config", ("config",)),
    ("Tests", ("test",)),
    ("Docs", ("docs",)),
    ("Skim or skip", ("lockfile", "i18n", "changelog", "generated")),
)


def matches(path: str, patterns: tuple[str, ...]) -> bool:
    p = "/" + path
    return any(fnmatch.fnmatchcase(p, pat) for pat in patterns)


def classify(path: str, generated: set[str]) -> str:
    if Path(path).name in LOCKFILES:
        return "lockfile"
    if path in generated:
        return "generated"
    for kind, patterns in NOISE.items():
        if matches(path, patterns):
            return kind
    for kind, patterns in KINDS:
        if matches(path, patterns):
            return kind
    if matches(path, ENTRY):
        return "entry"
    if matches(path, OUTBOUND):
        return "outbound"
    return "source"


NOISE_KINDS = {"lockfile", "i18n", "changelog", "generated"}


def declared_generated(worktree: Path, paths: list[str]) -> set[str]:
    """Paths the repository marks generated in .gitattributes."""
    if not paths:
        return set()
    out = must(["git", "-C", str(worktree), "check-attr", "-z", "--stdin",
                "linguist-generated", "gitlab-generated"], stdin="\0".join(paths) + "\0")
    toks = out.split("\0")
    found = set()
    for i in range(0, len(toks) - 2, 3):
        if toks[i + 2] in ("set", "true"):
            found.add(toks[i])
    return found


def area(path: str) -> str | None:
    head, sep, _ = path.partition("/")
    return head if sep and not head.startswith(".") else None


# --------------------------------------------------------------------------- prep


def review_dir(repo_name: str, branch: str) -> Path:
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return cache / "nu" / "rv" / repo_name / branch.replace("/", "-")


def ticket_of(*texts: str) -> str | None:
    for text in texts:
        m = re.match(r"^([a-z]{2,10})-(\d+)(?:-|$)", text or "", re.I)
        if m:
            return f"{m[1].upper()}-{m[2]}"
    for text in texts:
        m = re.search(r"\b([A-Z]{2,10}-\d+)\b", text or "")
        if m:
            return m[1]
    return None


def parse_target(arg: str | None, host: str, project: str, forge: str | None) -> tuple[str, object]:
    """('current', None) | ('branch', name) | ('mr', (forge, number))."""
    if not arg:
        return "current", None
    for pattern, kind in ((MR_URL, "gitlab"), (PR_URL, "github")):
        m = pattern.match(arg)
        if m:
            if m["host"].lower() != host or m["path"].lower() != project.lower():
                die(f"{arg} belongs to {m['host']}/{m['path']}, but origin is {host}/{project}; "
                    "run this from a checkout of that repository")
            return "mr", (kind, int(m["n"]))
    m = NUMBER.match(arg)
    if m:
        if not forge:
            die(f"'{arg}' looks like a merge request number, but origin is not GitLab or GitHub")
        return "mr", (forge, int(m["n"]))
    return "branch", arg.removeprefix("origin/")


def fetch(main: Path, refspecs: list[str]) -> None:
    if refspecs:
        print(paint("2", f"fetching {', '.join(r.split(':')[0].removeprefix('+') for r in refspecs)}"),
              file=sys.stderr)
        git(main, "fetch", "--quiet", "--no-tags", "origin", *refspecs)


def tracking(branch: str) -> str:
    return f"+refs/heads/{branch}:refs/remotes/origin/{branch}"


def sync_branch(main: Path, branch: str, want: str | None, notes: list[str]) -> Path:
    """Put `branch` at `want` (fast-forward only) and return the worktree that holds it."""
    path = worktree_for(main, branch)
    local = rev(main, f"refs/heads/{branch}")

    if want and local and local != want:
        if is_ancestor(main, local, want):
            if path is None:
                git(main, "branch", "-f", branch, want)
            elif clean(path):
                git(path, "merge", "--ff-only", "--quiet", want)
            else:
                notes.append(f"{path.name} has uncommitted changes, so it was not updated to "
                             f"{want[:9]}; reviewing the local branch")
        elif is_ancestor(main, want, local):
            notes.append(f"local {branch} is ahead of the remote; reviewing the local branch")
        else:
            notes.append(f"local {branch} and the remote have diverged; reviewing the local branch")
    elif want and not local and not rev(main, f"refs/remotes/origin/{branch}"):
        git(main, "branch", branch, want)
    elif not local and not rev(main, f"refs/remotes/origin/{branch}"):
        die(f"no local or fetched copy of {branch}; drop --no-fetch")

    if path is None:
        libexec = os.environ.get("NU_LIBEXEC")
        if not libexec:
            die("run this through `nu rv`; it needs `nu wt` to create the worktree")
        rc, out, err = attempt([f"{libexec}/nu-wt", "add", "--no-cd", branch], cwd=main)
        # Checkout progress is tens of thousands of \r-separated updates; an agent reads all of it.
        kept = [l for l in re.split(r"[\r\n]+", out + err) if l.strip() and not l.startswith("Updating files:")]
        print("\n".join(kept), file=sys.stderr)
        if rc != 0:
            die(f"nu wt add {branch} failed")
        path = worktree_for(main, branch)
        if path is None:
            die(f"nu wt add did not produce a worktree for {branch}")
    return path


def numstat(repo: Path, base: str, head: str) -> list[dict]:
    status: dict[str, str] = {}
    toks = git(repo, "diff", "--name-status", "-z", "-M", base, head).split("\0")
    i = 0
    while i < len(toks) and toks[i]:
        letter = toks[i][0]
        if letter in "RC":
            status[toks[i + 2]] = letter
            i += 3
        else:
            status[toks[i + 1]] = letter
            i += 2

    files = []
    toks = git(repo, "diff", "--numstat", "-z", "-M", base, head).split("\0")
    i = 0
    while i < len(toks) and toks[i]:
        added, deleted, path = toks[i].split("\t", 2)
        if path:
            old = path
            i += 1
        else:
            old, path = toks[i + 1], toks[i + 2]
            i += 3
        binary = added == "-"
        files.append({
            "path": path,
            "old_path": old if old != path else None,
            "status": status.get(path, "M"),
            "added": 0 if binary else int(added),
            "deleted": 0 if binary else int(deleted),
            "binary": binary,
        })
    return files


def prep(args: argparse.Namespace) -> int:
    main = main_worktree()
    host, project, forge = origin(main)
    kind, value = parse_target(args.target, host, project, forge)
    notes: list[str] = []
    refspecs: list[str] = []

    mr = None
    want = None
    if kind == "current":
        worktree = Path(git(Path.cwd(), "rev-parse", "--show-toplevel").strip())
        branch = git(worktree, "branch", "--show-current").strip()
        if not branch:
            die("HEAD is detached; name the branch or merge request to review")
        mr = find_mr(forge, project, branch)
    elif kind == "branch":
        branch = str(value)
        mr = find_mr(forge, project, branch)
        if not (mr and mr["cross_repo"]):
            remote_has = attempt(["git", "-C", str(main), "ls-remote", "--exit-code", "--heads",
                                  "origin", branch])[0] == 0 if not args.no_fetch else False
            if remote_has:
                refspecs.append(tracking(branch))
            elif not rev(main, f"refs/heads/{branch}") and not rev(main, f"refs/remotes/origin/{branch}"):
                die(f"no branch '{branch}' locally or on origin")
    else:
        mr_forge, number = value  # type: ignore[misc]
        mr = fetch_mr(mr_forge, project, number)
        branch = mr["source_branch"]
        if mr["cross_repo"]:
            branch = f"{'mr' if mr_forge == 'gitlab' else 'pr'}-{number}-{branch}"
        else:
            refspecs.append(tracking(mr["source_branch"]))

    if args.base:
        base_ref = args.base
    elif mr:
        base_ref = f"origin/{mr['target_branch']}"
    else:
        parent = attempt(["git", "-C", str(main), "config", f"branch.{branch}.nuParent"])[1].strip()
        base_ref = parent if parent and rev(main, f"refs/heads/{parent}") else f"origin/{default_branch(main)}"
    if base_ref.startswith("origin/"):
        refspecs.append(tracking(base_ref.removeprefix("origin/")))

    if not args.no_fetch:
        fetch(main, refspecs)
        if mr and mr["cross_repo"]:
            pull_ref = (f"refs/merge-requests/{mr['number']}/head" if mr["forge"] == "gitlab"
                        else f"refs/pull/{mr['number']}/head")
            git(main, "fetch", "--quiet", "--no-tags", "origin", pull_ref)
            want = rev(main, "FETCH_HEAD")

    if kind == "current":
        if git(worktree, "status", "--porcelain", "--untracked-files=no").strip():
            notes.append("uncommitted changes are not part of the review")
    else:
        if want is None and not (mr and mr["cross_repo"]):
            want = rev(main, f"refs/remotes/origin/{mr['source_branch'] if mr else branch}")
        worktree = sync_branch(main, branch, want, notes)

    base_sha = rev(main, base_ref) or die(f"base '{base_ref}' does not resolve to a commit")
    head_sha = rev(worktree, "HEAD") or die(f"{worktree} has no HEAD")
    merge_base = git(main, "merge-base", base_sha, head_sha).strip() or die(
        f"{branch} shares no history with {base_ref}")
    if merge_base == head_sha:
        die(f"{branch} has no commits that are not in {base_ref}; nothing to review")
    if mr and mr["head_sha"] and mr["head_sha"] != head_sha:
        notes.append(f"reviewing {head_sha[:9]}, but {mr['ref']} points at {mr['head_sha'][:9]}")

    files = numstat(main, merge_base, head_sha)
    generated = declared_generated(worktree, [f["path"] for f in files])
    for f in files:
        f["kind"] = classify(f["path"], generated)
        f["noise"] = f["kind"] in NOISE_KINDS

    log = git(main, "log", "--no-merges", "--format=%H%x1f%s%x1f%b%x1e", f"{merge_base}..{head_sha}")
    commits = []
    for record in log.split("\x1e"):
        record = record.strip("\n")
        if record:
            sha, subject, body = (record.split("\x1f") + ["", ""])[:3]
            commits.append({"sha": sha, "subject": subject, "body": body.strip()})

    reviewed = [f for f in files if not f["noise"]]
    review_lines = sum(f["added"] + f["deleted"] for f in reviewed)
    areas = sorted({a for f in reviewed if f["kind"] != "docs" and (a := area(f["path"]))})
    reasons = []
    if len(reviewed) > MAX_FILES:
        reasons.append(f"{len(reviewed)} files to review (> {MAX_FILES})")
    if review_lines > MAX_LINES:
        reasons.append(f"{review_lines} changed lines (> {MAX_LINES})")
    if len(areas) > MAX_AREAS:
        reasons.append(f"{len(areas)} areas: {', '.join(areas)}")

    order = []
    for title, kinds in READING_ORDER:
        group = sorted((f for f in files if f["kind"] in kinds), key=lambda f: -(f["added"] + f["deleted"]))
        if group:
            order.append({"title": title, "files": [f["path"] for f in group]})

    out_dir = review_dir(main.name, branch)
    out_dir.mkdir(parents=True, exist_ok=True)
    for stale in ("review.json", "review.html"):
        (out_dir / stale).unlink(missing_ok=True)
    patch = git(main, "diff", "--no-color", "--no-ext-diff", "--no-textconv", "-M",
                "--src-prefix=a/", "--dst-prefix=b/", merge_base, head_sha)
    (out_dir / "diff.patch").write_text(patch)

    meta = {
        "version": 1,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "repo": {"name": main.name, "main": str(main), "host": host, "project": project, "forge": forge},
        "worktree": str(worktree),
        "in_place": kind == "current",
        "branch": branch,
        "ticket": ticket_of(mr["source_branch"] if mr else branch, mr["title"] if mr else ""),
        "mr": mr,
        "base": {"ref": base_ref, "sha": base_sha, "merge_base": merge_base},
        "head": {"sha": head_sha},
        "commits": commits,
        "files": files,
        "stats": {
            "files": len(files),
            "added": sum(f["added"] for f in files),
            "deleted": sum(f["deleted"] for f in files),
            "review_files": len(reviewed),
            "review_lines": review_lines,
            "areas": areas,
        },
        "complexity": {"verdict": "complex" if reasons else "simple", "reasons": reasons},
        "reading_order": order,
        "notes": notes,
        "paths": {
            "dir": str(out_dir),
            "meta": str(out_dir / "meta.json"),
            "patch": str(out_dir / "diff.patch"),
            "review_json": str(out_dir / "review.json"),
            "html": str(out_dir / "review.html"),
        },
    }
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")

    report(meta)
    cd_file = os.environ.get("NU_CD_FILE")
    if cd_file and not args.no_cd and kind != "current":
        Path(cd_file).write_text(str(worktree) + "\n")
    return 0


def tilde(path: str) -> str:
    home = str(Path.home())
    return "~" + path[len(home):] if path.startswith(home + "/") else path


def report(meta: dict) -> None:
    mr, s, cx = meta["mr"], meta["stats"], meta["complexity"]
    title = mr["title"] if mr else meta["branch"]
    print(f"{paint('34', '==>')} {paint('1', title)}")
    if mr:
        print(f"    {mr['ref']}  {mr['url']}" + ("  (draft)" if mr["draft"] else ""))
    rows = [
        ("worktree", tilde(meta["worktree"]) + ("  (in place)" if meta["in_place"] else "")),
        ("diff", f"{meta['base']['ref']} ({meta['base']['merge_base'][:9]}) .. {meta['head']['sha'][:9]}, "
                 f"{len(meta['commits'])} commit(s)"),
        ("size", f"{s['files']} files (+{s['added']} -{s['deleted']}); "
                 f"{s['review_files']} to review, {s['review_lines']} lines"
                 + (f"; areas: {', '.join(s['areas'])}" if s["areas"] else "")),
        ("verdict", cx["verdict"] + (f" ({'; '.join(cx['reasons'])})" if cx["reasons"] else "")),
        ("meta", tilde(meta["paths"]["meta"])),
    ]
    for key, value in rows:
        print(f"    {paint('2', f'{key:<9}')}{value}")
    by_path = {f["path"]: f for f in meta["files"]}
    print(f"\n{paint('1', 'reading order')}")
    for group in meta["reading_order"]:
        print(f"  {group['title']}")
        for path in group["files"]:
            f = by_path[path]
            churn = "binary" if f["binary"] else f"+{f['added']} -{f['deleted']}"
            print(f"    {path}  {paint('2', churn)}")
    for note in meta["notes"]:
        warn(note)


# --------------------------------------------------------------------------- patch


HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")


def parse_patch(text: str) -> dict[str, dict]:
    """Per-file diffs keyed by the path meta.json uses (the old path for deletions)."""
    files: list[dict] = []
    cur: dict | None = None
    hunk: dict | None = None
    old = new = 0
    for line in text.split("\n"):
        if line.startswith("diff --git "):
            cur = {"path": None, "old_path": None, "hunks": [], "binary": False}
            files.append(cur)
            hunk = None
            m = re.match(r"^diff --git a/(.*) b/(.*)$", line)
            if m:
                cur["old_path"], cur["path"] = m[1], m[2]
            continue
        if cur is None:
            continue
        m = HUNK_RE.match(line)
        if m:
            old, new = int(m[1]), int(m[3])
            hunk = {"header": line, "lines": []}
            cur["hunks"].append(hunk)
            continue
        if hunk is None:
            if line.startswith("rename from "):
                cur["old_path"] = line[12:]
            elif line.startswith("rename to "):
                cur["path"] = line[10:]
            elif line.startswith("--- a/"):
                cur["old_path"] = line[6:].rstrip("\t")
            elif line.startswith("+++ b/"):
                cur["path"] = line[6:].rstrip("\t")
            elif line.startswith("deleted file mode"):
                cur["deleted"] = True
            elif line.startswith("Binary files ") or line == "GIT binary patch":
                cur["binary"] = True
            continue
        if line.startswith("+"):
            hunk["lines"].append(("add", None, new, line[1:]))
            new += 1
        elif line.startswith("-"):
            hunk["lines"].append(("del", old, None, line[1:]))
            old += 1
        elif line.startswith(" "):
            hunk["lines"].append(("ctx", old, new, line[1:]))
            old += 1
            new += 1
        elif line.startswith("\\"):
            hunk["lines"].append(("note", None, None, line))
    return {(f["old_path"] if f.get("deleted") else f["path"]): f for f in files}


# --------------------------------------------------------------------------- render: validation


SEVERITIES = ("critical", "high", "medium", "low")
SECTIONS = {"bugs": "Bug", "behavior": "Unintended behavior", "standards": "Standards"}
DECISIONS = {"merge": "Safe to merge", "fix": "Merge after fixes", "block": "Do not merge"}
CRITERIA = ("met", "partial", "missing", "unclear")
COMMENT_RE = re.compile(
    r"^(praise|nitpick|suggestion|issue|todo|question|thought|chore|note)"
    r"(?: \((?:blocking|non-blocking|if-minor|[a-z-]+)(?:, ?[a-z-]+)*\))?: \S"
)


def validate(review: dict, meta: dict, diffs: dict[str, dict]) -> list[str]:
    errors: list[str] = []

    def text(obj: dict, key: str, where: str, required: bool = True) -> None:
        value = obj.get(key)
        if value is None and not required:
            return
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{where}.{key}: required non-empty string")

    verdict = review.get("verdict")
    if not isinstance(verdict, dict) or verdict.get("decision") not in DECISIONS:
        errors.append(f"verdict.decision: one of {', '.join(DECISIONS)}")
    else:
        text(verdict, "text", "verdict")
    text(review, "summary", "review")

    intent = review.get("intent")
    if intent is not None:
        if not isinstance(intent, dict) or not isinstance(intent.get("criteria"), list):
            errors.append("intent: object with a criteria list")
        else:
            for i, c in enumerate(intent["criteria"]):
                if not isinstance(c, dict) or c.get("status") not in CRITERIA:
                    errors.append(f"intent.criteria[{i}].status: one of {', '.join(CRITERIA)}")
                else:
                    text(c, "text", f"intent.criteria[{i}]")

    steps = review.get("steps")
    seen: dict[str, int] = {}
    if not isinstance(steps, list) or not steps:
        errors.append("steps: non-empty list")
        steps = []
    for i, s in enumerate(steps):
        where = f"steps[{i}]"
        if not isinstance(s, dict):
            errors.append(f"{where}: object")
            continue
        text(s, "title", where)
        text(s, "explanation", where)
        text(s, "diagram", where, required=False)
        files = s.get("files")
        if not isinstance(files, list) or not files:
            errors.append(f"{where}.files: non-empty list of changed paths")
            continue
        for path in files:
            if path not in diffs:
                errors.append(f"{where}.files: '{path}' is not a file in the diff")
            elif path in seen:
                errors.append(f"{where}.files: '{path}' is already in steps[{seen[path]}]")
            else:
                seen[path] = i
    noise = {f["path"] for f in meta["files"] if f["noise"]}
    for path in diffs:
        if path not in seen and path not in noise:
            errors.append(f"steps: '{path}' is not in any step")

    findings = review.get("findings")
    if not isinstance(findings, list):
        errors.append("findings: list (empty when nothing was found)")
        findings = []
    for i, f in enumerate(findings):
        where = f"findings[{i}]"
        if not isinstance(f, dict):
            errors.append(f"{where}: object")
            continue
        for key in ("title", "body", "where", "why", "comment"):
            text(f, key, where)
        text(f, "fix", where, required=False)
        if f.get("severity") not in SEVERITIES:
            errors.append(f"{where}.severity: one of {', '.join(SEVERITIES)}")
        if f.get("section") not in SECTIONS:
            errors.append(f"{where}.section: one of {', '.join(SECTIONS)}")
        if isinstance(f.get("comment"), str) and not COMMENT_RE.match(f["comment"]):
            errors.append(f"{where}.comment: must start with a Conventional Comments label, "
                          "e.g. 'issue (blocking): ...' or 'suggestion (non-blocking): ...'")
        if f.get("side", "new") not in ("new", "old"):
            errors.append(f"{where}.side: 'new' or 'old'")
        for key in ("line", "end_line"):
            if key in f and (not isinstance(f[key], int) or f[key] < 1):
                errors.append(f"{where}.{key}: positive integer")
        if "line" in f and "file" not in f:
            errors.append(f"{where}.line: needs file")
        if "end_line" in f and isinstance(f.get("line"), int) and isinstance(f["end_line"], int) \
                and f["end_line"] < f["line"]:
            errors.append(f"{where}.end_line: must not be before line")
        if "file" in f and f["file"] not in diffs \
                and not (Path(meta["worktree"]) / str(f["file"])).exists():
            errors.append(f"{where}.file: '{f['file']}' is neither in the diff nor in the worktree")
    return errors


# --------------------------------------------------------------------------- render: html


def esc(s: str) -> str:
    return html.escape(s, quote=True)


def inline(s: str) -> str:
    parts = s.split("`")
    if len(parts) % 2 == 0:
        return esc(s)
    return "".join(esc(p) if i % 2 == 0 else f"<code>{esc(p)}</code>" for i, p in enumerate(parts))


def prose(s: str) -> str:
    out = []
    for block in re.split(r"\n\s*\n", s.strip()):
        lines = [l for l in block.split("\n") if l.strip()]
        if lines and all(l.lstrip().startswith(("- ", "* ")) for l in lines):
            items = "".join(f"<li>{inline(l.lstrip()[2:])}</li>" for l in lines)
            out.append(f"<ul>{items}</ul>")
        else:
            out.append(f"<p>{inline(' '.join(l.strip() for l in lines))}</p>")
    return "".join(out)


JVM = {".kt", ".kts", ".java", ".gradle", ".groovy", ".scala"}
WEB = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".html", ".scss", ".css", ".vue"}


def ide_prefix(worktree: Path, path: str) -> str:
    """URL that opens `path` in the right IDE once a line number is appended."""
    full = worktree / path
    ext = full.suffix
    ide = "idea" if ext in JVM else "vscode" if ext in WEB else None
    d = full.parent
    while ide is None:
        if any((d / m).exists() for m in ("build.gradle.kts", "build.gradle", "settings.gradle.kts",
                                          "settings.gradle", "pom.xml")):
            ide = "idea"
        elif (d / "package.json").exists() or (d / "tsconfig.json").exists() or d == worktree \
                or d == d.parent:
            ide = "vscode"
        d = d.parent
    if ide == "idea":
        return f"idea://open?file={quote(str(full))}&line="
    return f"vscode://file{quote(str(full))}:"


def finding_codes(findings: list[dict]) -> list[str]:
    """C1, H1, H2, M1, …: the severity's letter and its count among the findings of that severity."""
    seen = dict.fromkeys(SEVERITIES, 0)
    codes = []
    for f in findings:
        seen[f["severity"]] += 1
        codes.append(f"{f['severity'][0].upper()}{seen[f['severity']]}")
    return codes


def fix_prompt(code: str, f: dict, meta: dict, review_path: Path) -> str:
    """A prompt that lets an agent with no other context fix this one finding."""
    where = f["where"]
    if f.get("file"):
        loc = f["file"]
        if "line" in f:
            end = f.get("end_line", f["line"])
            loc += f", line {f['line']}" if end == f["line"] else f", lines {f['line']}-{end}"
            if f.get("side") == "old":
                loc += " of the old file"
        where += f" ({loc})"
    lines = [
        f"Fix review finding {code} in the worktree {meta['worktree']} (branch {meta['branch']}).",
        "",
        f"{code}: {f['title']} [{f['severity'].title()}, {SECTIONS[f['section']]}]",
        f["body"],
        f"Where: {where}",
        f"Why: {f['why']}",
    ]
    if f.get("fix"):
        lines.append(f"Fix: {f['fix']}")
    lines += ["", f"The full review is in {review_path}. Fix only this finding, in that worktree. Do not push."]
    return "\n".join(lines)


def finding_card(code: str, f: dict, ide: str | None, prompt: str) -> str:
    sev = f["severity"]
    rows = [("Where", f["where"]), ("Why", f["why"])] + ([("Fix", f["fix"])] if f.get("fix") else [])
    dl = "".join(f"<dt>{k}</dt><dd>{inline(v)}</dd>" for k, v in rows)
    open_link = (f' <a class="btn" href="{esc(ide + str(f.get("line", 1)))}">Open in IDE</a>' if ide else "")
    return (
        f'<article class="finding sev-{sev}" id="{code}" tabindex="-1">'
        f'<header><a class="code-id" href="#{code}">{code}</a><span class="pill {sev}">{sev.title()}</span>'
        f'<span class="section">{SECTIONS[f["section"]]}</span>'
        f'<h4>{inline(f["title"])}</h4></header>'
        f'<div class="prose">{prose(f["body"])}</div><dl>{dl}</dl>'
        f'<div class="cc"><pre>{esc(f["comment"])}</pre>'
        f'<div class="actions"><button class="btn copy" type="button" data-copy="{esc(f["comment"])}">'
        f'Copy comment</button><button class="btn copy" type="button" data-copy="{esc(prompt)}">'
        f'Fix locally</button>{open_link}</div></div></article>'
    )


def anchor_of(f: dict, diff: dict | None) -> tuple[str, int] | None:
    """(side, line) of the diff row the card goes under, when that line is displayed."""
    if diff is None or "line" not in f:
        return None
    side = f.get("side", "new")
    last = f.get("end_line", f["line"])
    for h in diff["hunks"]:
        for kind, o, n, _ in h["lines"]:
            if (n if side == "new" else o) == last and (kind != "del" if side == "new" else kind != "add"):
                return side, last
    return None


def file_section(path: str, meta_file: dict, diff: dict, cards: dict, unanchored: list[str],
                 ide: str | None, highlight: dict) -> str:
    status = {"A": "added", "D": "deleted", "R": "renamed", "C": "copied"}.get(meta_file["status"], "modified")
    name = f"{esc(meta_file['old_path'])} → {esc(path)}" if meta_file.get("old_path") else esc(path)
    churn = "binary" if meta_file["binary"] else f'<span class="a">+{meta_file["added"]}</span> ' \
                                                  f'<span class="d">−{meta_file["deleted"]}</span>'
    size = sum(len(h["lines"]) for h in diff["hunks"])
    is_open = "" if meta_file["noise"] or size > COLLAPSE_LINES else " open"
    rows = []
    for h in diff["hunks"]:
        rows.append(f'<tr class="hunk"><td colspan="3">{esc(h["header"])}</td></tr>')
        for kind, o, n, content in h["lines"]:
            if kind == "note":
                rows.append(f'<tr class="note"><td></td><td></td><td>{esc(content)}</td></tr>')
                continue
            hl = " hl" if (o is not None and o in highlight.get("old", ())) or \
                          (n is not None and n in highlight.get("new", ())) else ""
            sign = {"add": "+", "del": "-", "ctx": " "}[kind]
            new_cell = f'<td class="ln new" data-l="{n}">{n}</td>' if n is not None else '<td class="ln"></td>'
            rows.append(f'<tr class="{kind}{hl}"><td class="ln">{o if o is not None else ""}</td>'
                        f'{new_cell}<td class="code">{sign}{esc(content)}</td></tr>')
            keys = ([("new", n)] if n is not None else []) + ([("old", o)] if o is not None and kind != "add" else [])
            for key in keys:
                for card in cards.pop(key, []):
                    rows.append(f'<tr class="anchor"><td colspan="3">{card}</td></tr>')
    if diff["binary"] and not diff["hunks"]:
        rows.append('<tr class="note"><td></td><td></td><td>Binary file; not shown.</td></tr>')
    data_ide = f' data-ide="{esc(ide)}"' if ide else ""
    open_link = f'<a class="btn small" href="{esc(ide)}1">Open in IDE</a>' if ide else ""
    return (
        f'<section class="file" id="{esc(file_id(path))}"{data_ide}><details{is_open}><summary>'
        f'<span class="status {status}">{status}</span><span class="path">{name}</span>'
        f'<span class="churn">{churn}</span>{open_link}</summary>'
        + "".join(unanchored)
        + f'<table class="diff"><colgroup><col class="ln"><col class="ln"><col></colgroup>'
          f'<tbody>{"".join(rows)}</tbody></table></details></section>'
    )


def file_id(path: str) -> str:
    return "file-" + re.sub(r"[^A-Za-z0-9_-]", "-", path)


def render_html(review: dict, meta: dict, diffs: dict[str, dict], review_path: Path) -> tuple[str, list[str]]:
    worktree = Path(meta["worktree"])
    files_meta = {f["path"]: f for f in meta["files"]}
    findings = review["findings"]
    notes = []

    ide_cache: dict[str, str | None] = {}

    def ide(path: str) -> str | None:
        if path not in ide_cache:
            gone = files_meta.get(path, {}).get("status") == "D" or not (worktree / path).exists()
            ide_cache[path] = None if gone else ide_prefix(worktree, path)
        return ide_cache[path]

    steps = [dict(s) for s in review["steps"]]
    covered = {p for s in steps for p in s["files"]}
    rest = [p for p in diffs if p not in covered]
    if rest:
        steps.append({"title": "Generated files, lockfiles, translations and changelog",
                      "explanation": "Listed for completeness. Nothing here was written by hand.",
                      "files": rest})
    step_of = {p: i for i, s in enumerate(steps) for p in s["files"]}

    order = sorted(range(len(findings)), key=lambda i: SEVERITIES.index(findings[i]["severity"]))
    codes = finding_codes(findings)

    anchored: dict[str, dict] = {}
    above_file: dict[str, list[str]] = {}
    general: list[str] = []
    highlight: dict[str, dict] = {}
    for i, f in enumerate(findings):
        path = f.get("file")
        card = finding_card(codes[i], f, ide(path) if path else None, fix_prompt(codes[i], f, meta, review_path))
        diff = diffs.get(path) if path else None
        anchor = anchor_of(f, diff)
        if anchor:
            anchored.setdefault(path, {}).setdefault(anchor, []).append(card)
            side, last = anchor
            highlight.setdefault(path, {}).setdefault(side, set()).update(range(f["line"], last + 1))
        elif diff is not None:
            above_file.setdefault(path, []).append(card)
            if "line" in f:
                notes.append(f"finding {codes[i]} ({path}, line {f['line']}) is not on a displayed "
                             "line; shown above the file")
        else:
            general.append(card)

    mr = meta["mr"]
    s = meta["stats"]
    title = mr["title"] if mr else meta["branch"]
    decision = review["verdict"]["decision"]
    counts = {sev: sum(1 for f in findings if f["severity"] == sev) for sev in SEVERITIES}

    header_bits = []
    if mr:
        header_bits.append(f'<a href="{esc(mr["url"])}">{esc(mr["ref"])}</a>')
        if mr.get("author"):
            header_bits.append(f"by {esc(mr['author'])}")
        if mr.get("draft"):
            header_bits.append("draft")
    header_bits.append(f"<code>{esc(meta['branch'])}</code> → <code>{esc(meta['base']['ref'])}</code>")
    header_bits.append(f"<code>{meta['base']['merge_base'][:9]}..{meta['head']['sha'][:9]}</code>")
    header_bits.append(f"{len(meta['commits'])} commit(s), {s['files']} files, "
                       f'<span class="a">+{s["added"]}</span> <span class="d">−{s["deleted"]}</span>')

    parts = [
        f'<header class="top" id="overview"><h1>{inline(title)}</h1>'
        f'<p class="meta">{" · ".join(header_bits)}</p></header>',
        f'<div class="verdict {decision}"><strong>{DECISIONS[decision]}.</strong> '
        f'{inline(review["verdict"]["text"])}</div>',
    ]
    for note in meta.get("notes", []):
        parts.append(f'<div class="warn">{inline(note)}</div>')
    parts.append(f'<section class="summary"><h2>What changed</h2>{prose(review["summary"])}</section>')

    intent = review.get("intent")
    if intent and intent["criteria"]:
        label = intent.get("ticket") or (meta.get("ticket") or "Ticket")
        link = f'<a href="{esc(intent["url"])}">{esc(label)}</a>' if intent.get("url") else esc(label)
        rows = "".join(
            f'<tr><td>{inline(c["text"])}</td><td><span class="crit {c["status"]}">{c["status"]}</span></td>'
            f'<td>{inline(c.get("note") or "")}</td></tr>' for c in intent["criteria"])
        parts.append(f'<section class="intent"><h2>Acceptance criteria · {link}</h2>'
                     f'<table><thead><tr><th>Criterion</th><th>Status</th><th>Note</th></tr></thead>'
                     f'<tbody>{rows}</tbody></table></section>')

    chips = " ".join(f'<span class="pill {sev}">{counts[sev]} {sev}</span>' for sev in SEVERITIES if counts[sev])
    if findings:
        items = "".join(
            f'<li><a href="#{codes[i]}"><span class="code-id">{codes[i]}</span> '
            f'<span class="pill {findings[i]["severity"]}">'
            f'{findings[i]["severity"].title()}</span> {inline(findings[i]["title"])}</a>'
            f'<span class="loc">{inline(findings[i]["where"])}</span></li>' for i in order)
        parts.append(f'<section class="attention" id="findings"><h2>Needs your attention {chips}</h2>'
                     f'<ol>{items}</ol><p class="hint">j / k jump between findings in the code.</p></section>')
    else:
        parts.append('<section class="attention" id="findings"><h2>Needs your attention</h2>'
                     '<p>None found.</p></section>')
    if general:
        parts.append(f'<section class="general"><h2>Findings outside the diff</h2>{"".join(general)}</section>')

    rail = ['<a href="#overview">Overview</a>', f'<a href="#findings">Findings ({len(findings)})</a>']
    for i, step in enumerate(steps):
        n_find = sum(1 for f in findings if step_of.get(f.get("file", "")) == i)
        badge = f' <span class="count">{n_find}</span>' if n_find else ""
        sub = "".join(f'<li><a href="#{esc(file_id(p))}" title="{esc(p)}">{esc(Path(p).name)}</a></li>'
                      for p in step["files"])
        rail.append(f'<a href="#step-{i + 1}">{i + 1}. {inline(step["title"])}{badge}</a><ul>{sub}</ul>')
        body = [f'<section class="step" id="step-{i + 1}"><h2>{i + 1}. {inline(step["title"])}</h2>'
                f'<div class="prose">{prose(step["explanation"])}</div>']
        if step.get("diagram"):
            body.append(f'<pre class="mermaid">{esc(step["diagram"])}</pre>')
        for path in step["files"]:
            cards = anchored.get(path, {})
            body.append(file_section(path, files_meta[path], diffs[path], cards, above_file.get(path, []),
                                     ide(path), highlight.get(path, {})))
        body.append("</section>")
        parts.append("".join(body))

    mermaid = ""
    if any(st.get("diagram") for st in steps):
        mermaid = ("<script type=\"module\">import mermaid from "
                   "'https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs';"
                   "mermaid.initialize({startOnLoad:true,theme:matchMedia('(prefers-color-scheme: dark)')"
                   ".matches?'dark':'default'});</script>")

    page = (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>Review: {esc(title)}</title><style>{CSS}</style></head><body>"
        f'<nav class="rail">{"".join(rail)}</nav><main>{"".join(parts)}'
        f'<footer>Generated {esc(time.strftime("%Y-%m-%d %H:%M"))} by nu rv from '
        f'<code>{esc(tilde(meta["worktree"]))}</code></footer></main>'
        f"<script>{JS}</script>{mermaid}</body></html>"
    )
    return page, notes


CSS = """
:root{--bg:#fff;--fg:#1f2328;--muted:#656d76;--border:#d0d7de;--surface:#f6f8fa;--accent:#0969da;
--add:#dafbe1;--del:#ffebe9;--hl:#fff8c5;--crit:#cf222e;--high:#bc4c00;--med:#9a6700;--low:#656d76;
--ok:#1a7f37}
@media (prefers-color-scheme: dark){:root{--bg:#0d1117;--fg:#e6edf3;--muted:#8d96a0;--border:#30363d;
--surface:#161b22;--accent:#4493f8;--add:#12261e;--del:#25171c;--hl:#3b2f0b;--crit:#ff7b72;--high:#ffa657;
--med:#d29922;--low:#8d96a0;--ok:#3fb950}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.6 system-ui,-apple-system,BlinkMacSystemFont,
"Segoe UI",Helvetica,Arial,sans-serif;-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility;
display:grid;grid-template-columns:300px minmax(0,1fr)}
code,pre,.diff{font-family:ui-monospace,"SF Mono",SFMono-Regular,Menlo,monospace;font-size:14px}
code{background:var(--surface);border:1px solid var(--border);border-radius:4px;padding:0 4px}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
.rail{position:sticky;top:0;height:100vh;overflow:auto;padding:16px;border-right:1px solid var(--border);
background:var(--surface);font-size:14.5px}
.rail>a{display:block;font-weight:600;margin:10px 0 4px;color:var(--fg)}
.rail ul{list-style:none;margin:0;padding-left:12px}.rail li a{color:var(--muted);display:block;
overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.count{background:var(--high);color:var(--bg);border-radius:9px;padding:0 6px;font-size:12px}
main{padding:24px 32px;max-width:1280px}
h1{font-size:26px;margin:0 0 4px}h2{font-size:20px;margin:28px 0 8px}h4{margin:0;font-size:16px}
.meta,.loc,.hint,footer{color:var(--muted)}footer{margin:40px 0 8px;font-size:13px}
.verdict{border-left:4px solid;padding:10px 14px;margin:16px 0;background:var(--surface);border-radius:4px}
.verdict.merge{border-color:var(--ok)}.verdict.fix{border-color:var(--med)}.verdict.block{border-color:var(--crit)}
.warn{border-left:4px solid var(--med);padding:6px 12px;margin:8px 0;background:var(--surface)}
.code-id{font-family:ui-monospace,"SF Mono",Menlo,monospace;font-weight:700;color:var(--fg)}
.pill{display:inline-block;border-radius:10px;padding:0 8px;font-size:12.5px;font-weight:600;color:var(--bg)}
.pill.critical{background:var(--crit)}.pill.high{background:var(--high)}.pill.medium{background:var(--med)}
.pill.low{background:var(--low)}
.attention ol{padding-left:22px}.attention li{margin:4px 0}.attention .loc{margin-left:8px;font-size:14px}
table{border-collapse:collapse}.intent table{width:100%}.intent td,.intent th{border:1px solid var(--border);
padding:6px 8px;text-align:left;vertical-align:top}
.crit{font-weight:600}.crit.met{color:var(--ok)}.crit.partial{color:var(--med)}.crit.missing{color:var(--crit)}
.crit.unclear{color:var(--muted)}
.step{border-top:1px solid var(--border);margin-top:28px}
.file{border:1px solid var(--border);border-radius:6px;margin:12px 0;overflow:hidden}
.file summary{display:flex;gap:10px;align-items:center;padding:8px 12px;background:var(--surface);
cursor:pointer;position:sticky;top:0;z-index:1}
.file .path{font-family:ui-monospace,"SF Mono",Menlo,monospace;font-size:14px;flex:1;overflow-wrap:anywhere}
.status{font-size:12px;text-transform:uppercase;color:var(--muted);min-width:64px}
.a{color:var(--ok)}.d{color:var(--crit)}
.btn{border:1px solid var(--border);background:var(--bg);color:var(--fg);border-radius:6px;padding:3px 10px;
font-size:13px;cursor:pointer}.btn.small{padding:1px 8px}
.diff{width:100%;table-layout:fixed}
.diff td{padding:0 8px;vertical-align:top;white-space:pre-wrap;overflow-wrap:anywhere}
.diff col.ln{width:56px}.diff td.ln{text-align:right;color:var(--muted);user-select:none}
.diff td.ln.new{cursor:pointer}.diff td.ln.new:hover{color:var(--accent);text-decoration:underline}
.diff tr.add td{background:var(--add)}.diff tr.del td{background:var(--del)}
.diff tr.hl td.code{background:var(--hl)}
.diff tr.hunk td{background:var(--surface);color:var(--muted);padding:4px 8px}
.diff tr.anchor td{padding:8px 12px;white-space:normal;font:16px/1.6 system-ui,-apple-system,BlinkMacSystemFont,
"Segoe UI",sans-serif}
.finding{border:1px solid var(--border);border-left:4px solid;border-radius:6px;padding:10px 14px;margin:10px 0;
background:var(--bg)}
.finding:focus{outline:2px solid var(--accent)}
.finding.sev-critical{border-left-color:var(--crit)}.finding.sev-high{border-left-color:var(--high)}
.finding.sev-medium{border-left-color:var(--med)}.finding.sev-low{border-left-color:var(--low)}
.finding header{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.finding .section{color:var(--muted);font-size:13px}
.finding dl{display:grid;grid-template-columns:auto 1fr;gap:2px 12px;margin:8px 0}
.finding dt{font-weight:600;color:var(--muted)}.finding dd{margin:0}
.cc pre{background:var(--surface);border:1px solid var(--border);border-radius:6px;padding:8px;
white-space:pre-wrap;margin:6px 0}
.actions{display:flex;gap:8px}
.prose p{margin:6px 0}
pre.mermaid{background:var(--surface);padding:12px;border-radius:6px}
@media (max-width:900px){body{grid-template-columns:1fr}.rail{display:none}}
"""

JS = """
const copyText=async t=>{try{await navigator.clipboard.writeText(t)}catch{const a=document.createElement('textarea');
a.value=t;document.body.append(a);a.select();document.execCommand('copy');a.remove()}};
document.addEventListener('click',e=>{const b=e.target.closest('button.copy');
if(b){const label=b.dataset.label??=b.textContent;copyText(b.dataset.copy).then(()=>{b.textContent='Copied';setTimeout(()=>b.textContent=label,1500)});return}
const ln=e.target.closest('td.ln.new[data-l]');if(ln){const f=ln.closest('[data-ide]');if(f)location.href=f.dataset.ide+ln.dataset.l}});
const reveal=()=>{const el=location.hash&&document.getElementById(location.hash.slice(1));
if(el){let d=el.closest('details');while(d){d.open=true;d=d.parentElement.closest('details')}el.scrollIntoView({block:'center'})}};
addEventListener('hashchange',reveal);reveal();
const cards=[...document.querySelectorAll('main .finding')];let at=-1;
document.addEventListener('keydown',e=>{if(e.metaKey||e.ctrlKey||e.altKey||e.target.closest('input,textarea'))return;
if(e.key!=='j'&&e.key!=='k')return;if(!cards.length)return;
at=e.key==='j'?Math.min(at+1,cards.length-1):Math.max(at-1,0);const c=cards[at];
let d=c.closest('details');while(d){d.open=true;d=d.parentElement.closest('details')}
c.scrollIntoView({block:'center'});c.focus({preventScroll:true})});
"""


def render(args: argparse.Namespace) -> int:
    review_path = Path(args.review).resolve()
    out_dir = review_path.parent
    meta_path, patch_path = out_dir / "meta.json", out_dir / "diff.patch"
    if not meta_path.exists() or not patch_path.exists():
        die(f"{out_dir} has no meta.json and diff.patch; run `nu rv prep` first")
    try:
        review = json.loads(review_path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        print(f"{review_path}: {e}", file=sys.stderr)
        return 2
    meta = json.loads(meta_path.read_text())
    diffs = parse_patch(patch_path.read_text())
    missing = {f["path"] for f in meta["files"]} - set(diffs)
    if missing:
        die(f"diff.patch and meta.json disagree on {', '.join(sorted(missing))}; run `nu rv prep` again")

    errors = validate(review, meta, diffs) if isinstance(review, dict) else ["review: JSON object"]
    if errors:
        print(f"{review_path} is invalid:", file=sys.stderr)
        for e in errors:
            print(f"  {e}", file=sys.stderr)
        return 2

    page, notes = render_html(review, meta, diffs, review_path)
    out = out_dir / "review.html"
    out.write_text(page)
    for note in notes:
        warn(note)
    print(out)
    if not args.no_open:
        attempt(["open" if sys.platform == "darwin" else "xdg-open", str(out)])
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="nu rv")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prep")
    p.add_argument("target", nargs="?")
    p.add_argument("--base")
    p.add_argument("--no-fetch", action="store_true")
    p.add_argument("--no-cd", action="store_true")
    r = sub.add_parser("render")
    r.add_argument("review")
    r.add_argument("--no-open", action="store_true")
    args = parser.parse_args()
    return prep(args) if args.cmd == "prep" else render(args)


if __name__ == "__main__":
    sys.exit(main())

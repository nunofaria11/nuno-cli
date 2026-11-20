// worktrees: one dialog for the repo the focused terminal is in. Rows are the
// repo's checkouts (open ones marked), then branches that have none. Enter on a
// checkout shows its space, or makes one on it. Enter on a branch, or on a name
// nobody has, creates the checkout with git the way `nu wt add` would and makes
// a space on it. Spaces are left unnamed so the sidebar nests them under the repo.

import { View, div } from "gpui-kit";
import { v_flex, h_flex, InputState, Input, Scrollbar } from "gpui-base";
import { run, props, closeOverlay, theme, spaces, panes, focused, exec, home, appHome, readTextFile, checking } from "herdr";

const FETCH_TIMEOUT = 20_000;
const ADD_TIMEOUT = 600_000;
// Drawn rows; the filter narrows a repo with a thousand remote branches.
const MAX_ROWS = 80;

const tilde = (path, userHome) => (path.startsWith(userHome) ? `~${path.slice(userHome.length)}` : path);
const ago = (s) => {
  const d = Date.now() / 1000 - s;
  return d < 3600 ? `${Math.max(1, Math.round(d / 60))}m` : d < 86400 ? `${Math.round(d / 3600)}h` : d < 86400 * 60 ? `${Math.round(d / 86400)}d` : `${Math.round(d / 86400 / 30)}mo`;
};
// `..` and `.` folded, so a root like `../flip.worktrees` yields the path git will report.
function normalize(path) {
  const out = [];
  for (const part of path.split("/")) {
    if (part === "" || part === ".") continue;
    if (part === "..") out.pop();
    else out.push(part);
  }
  return "/" + out.join("/");
}

async function git(cwd, args, timeout) {
  const r = await exec("git", args, { cwd, ...(timeout === undefined ? {} : { timeout }) });
  return { ok: r.code === 0, out: (r.stdout ?? "").trim(), err: (r.stderr ?? r.stdout ?? "").trim() };
}

// The repo a folder belongs to: its main checkout and name. null outside git.
async function repoOf(folder) {
  const common = await git(folder, ["rev-parse", "--path-format=absolute", "--git-common-dir"]);
  if (!common.ok) return null;
  const main = common.out.endsWith("/.git") ? common.out.slice(0, -5) : common.out;
  return { main, name: main.split("/").pop() };
}

// Where this repo's worktrees go, as `nu wt` reads it: `git config nu.worktreesRoot`
// set in the repo is used as-is (relative: from the main checkout), set globally it
// gets the repo's name appended. Unset: the app's own rule, settings.json
// `worktrees.directory` (default ~/.herdr/worktrees) plus the repo's name.
async function worktreesRoot(repo, userHome) {
  const absolute = (p) => (p.startsWith("/") ? p : `${repo.main}/${p}`);
  const local = await git(repo.main, ["config", "--local", "--type=path", "--get", "nu.worktreesRoot"]);
  if (local.ok && local.out) return normalize(absolute(local.out));
  const global = await git(repo.main, ["config", "--global", "--type=path", "--get", "nu.worktreesRoot"]);
  if (global.ok && global.out) return normalize(`${absolute(global.out)}/${repo.name}`);
  const raw = await readTextFile(`${appHome()}/settings.json`);
  let dir = (raw === null ? {} : JSON.parse(raw))?.worktrees?.directory ?? "~/.herdr/worktrees";
  if (typeof dir !== "string" || !dir.trim()) throw new Error("worktrees.directory must be a non-empty path.");
  if (dir === "~" || dir.startsWith("~/")) dir = userHome.replace(/\/$/, "") + dir.slice(1);
  if (!dir.startsWith("/")) throw new Error("worktrees.directory must be an absolute path or start with ~/.");
  return normalize(`${dir}/${repo.name}`);
}

// origin/<default>: origin/HEAD, else origin/main or origin/master.
async function defaultBase(main) {
  const head = await git(main, ["symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"]);
  if (head.ok && head.out) return head.out.replace(/^refs\/remotes\//, "");
  for (const b of ["origin/main", "origin/master"]) {
    if ((await git(main, ["rev-parse", "--verify", "--quiet", `refs/remotes/${b}`])).ok) return b;
  }
  throw new Error("No origin/HEAD, origin/main or origin/master to branch from.");
}

// Where a space is now: its first terminal's live folder, else where it was made.
function place(s, ps) {
  const tabs = new Set((s.tabs ?? []).map((c) => c.id));
  return ps.find((p) => tabs.has(p.tab) && p.cwd)?.cwd || s.folder || null;
}

// The local space showing each checkout: its first terminal is in it (the deepest
// checkout wins). Read once per render: spaces() and panes() copy everything.
function spacesOf(checkouts) {
  const ps = panes();
  const inside = (p, dir) => p === dir || p.startsWith(dir + "/");
  const out = new Map();
  for (const s of spaces()) {
    if (s.machine) continue;
    const at = place(s, ps);
    if (!at) continue;
    const deepest = checkouts.filter((c) => inside(at, c.path)).sort((a, b) => b.path.length - a.path.length)[0];
    if (deepest && !out.has(deepest.path)) out.set(deepest.path, s);
  }
  return out;
}

export default class Worktrees extends View {
  init(_props, cx) {
    this.userHome = home();
    this.text = "";
    this.at = 0;
    this.rows = []; // { kind: "checkout" | "branch", branch, path?, remote?, at }
    this.repo = undefined; // undefined: looking, null: not in git
    this.root = null; // where a new checkout goes, once known
    this.busy = "";
    this.error = "";
    this.fetched = false;
    this.fetching = null;
    this.input = InputState.new({ placeholder: "branch: filter, or a new name" });
    this.input.on("change", (_e, cx) => { this.text = this.input.value(); this.error = ""; this.at = 0; cx.notify(); });
    this.input.on("submit", (_e, cx) => this.go(cx));
    this.inputFocused = true;
    this.input.on("focus", (_e, cx) => { this.inputFocused = true; cx.notify(); });
    this.input.on("blur", (_e, cx) => { this.inputFocused = false; cx.notify(); });
    this.handle = cx.focus_handle();
    this.handle.focus();
    this.input.focus();
    if (checking()) return;
    cx.spawn(async (cx) => {
      try {
        const f = focused();
        const ps = panes();
        const active = spaces().find((s) => s.active);
        if (active?.machine) throw new Error("Worktrees work on this computer only.");
        this.folder = ps.find((p) => p.pane === f?.pane)?.cwd || f?.cwd || (active ? place(active, ps) : null) || this.userHome;
        this.repo = await repoOf(this.folder);
        if (!this.repo) { cx.notify(); return; }
        this.rows = await this.load();
        cx.notify();
        this.handle.focus();
        cx.timer.after(100, () => this.input.focus());
        try { this.root = await worktreesRoot(this.repo, this.userHome); }
        catch (error) { this.error = String(error).replace(/^Error: /, ""); }
        cx.notify();
        // Remote branches land after a fetch; the local view is already up.
        this.fetching = git(this.repo.main, ["fetch", "--prune", "--quiet", "origin"], FETCH_TIMEOUT);
        await this.fetching;
        this.fetched = true;
        this.rows = await this.load();
        cx.notify();
      } catch (error) { this.repo = null; this.error = String(error); cx.notify(); }
    });
  }

  // Checkouts (main first, then most recently committed), then branches without
  // one, local or on origin, most recently committed first.
  async load() {
    const main = this.repo.main;
    const list = await git(main, ["worktree", "list", "--porcelain"]);
    if (!list.ok) throw new Error(list.err || "git worktree list failed.");
    const refs = await git(main, ["for-each-ref", "--format=%(refname:short)%09%(committerdate:unix)", "refs/heads", "refs/remotes/origin"]);
    const when = new Map(refs.out.split("\n").map((l) => l.split("\t")).map(([b, at]) => [b, Number(at) || 0]));
    const checkouts = [];
    for (const block of list.out.split(/\n\n+/)) {
      const lines = block.split("\n");
      const path = lines.find((l) => l.startsWith("worktree "))?.slice(9);
      if (!path || lines.some((l) => l === "bare" || l.startsWith("prunable"))) continue;
      const branch = lines.find((l) => l.startsWith("branch "))?.slice(7).replace(/^refs\/heads\//, "") ?? null;
      checkouts.push({ kind: "checkout", path, branch, main: checkouts.length === 0, at: branch ? (when.get(branch) ?? 0) : 0 });
    }
    checkouts.sort((a, b) => b.main - a.main || b.at - a.at);
    const taken = new Set(checkouts.map((c) => c.branch));
    const branches = new Map();
    for (const [ref, at] of when) {
      const remote = ref.startsWith("origin/");
      const branch = remote ? ref.slice(7) : ref;
      if (branch === "HEAD" || taken.has(branch)) continue;
      const seen = branches.get(branch);
      if (!seen) branches.set(branch, { kind: "branch", branch, remote, at });
      else { seen.remote ||= remote; seen.at = Math.max(seen.at, at); }
    }
    return [...checkouts, ...[...branches.values()].sort((a, b) => b.at - a.at)];
  }

  close() {
    closeOverlay(props().overlay);
  }

  // Matching rows, capped; `hidden` counts the rest.
  shown() {
    const f = this.text.trim().toLowerCase();
    const all = this.rows.filter((r) => !f || `${r.branch ?? ""} ${r.path ?? ""}`.toLowerCase().includes(f));
    const name = this.text.trim();
    const rows = all.slice(0, MAX_ROWS);
    if (name && !this.rows.some((r) => r.branch === name)) rows.push({ kind: "new", branch: name });
    return Object.assign(rows, { hidden: all.length - Math.min(all.length, MAX_ROWS) });
  }

  go(cx) {
    const row = this.shown()[this.at];
    if (!row || this.busy || !this.repo) return;
    if (row.kind === "checkout") return this.open(row);
    cx.spawn(async (cx) => this.create(row.branch, cx));
  }

  open(c) {
    this.close();
    const s = spacesOf(this.rows.filter((r) => r.kind === "checkout")).get(c.path);
    if (s) run("space.select", { id: s.id });
    else run("space.new", { folder: c.path, machine: "" });
  }

  // As `nu wt add`: an existing local branch is checked out, origin/<branch> is
  // tracked, anything else is a new branch off origin/<default> with no upstream.
  async create(branch, cx) {
    this.busy = branch;
    this.error = "";
    cx.notify();
    try {
      const main = this.repo.main;
      if (this.fetching) await this.fetching;
      const root = this.root ?? (await worktreesRoot(this.repo, this.userHome));
      const path = `${root}/${branch.replace(/\//g, "-")}`;
      let add;
      if ((await git(main, ["show-ref", "--verify", "--quiet", `refs/heads/${branch}`])).ok) add = ["worktree", "add", path, branch];
      else if ((await git(main, ["show-ref", "--verify", "--quiet", `refs/remotes/origin/${branch}`])).ok) add = ["worktree", "add", "--track", "-b", branch, path, `origin/${branch}`];
      else add = ["worktree", "add", "--no-track", "-b", branch, path, await defaultBase(main)];
      const r = await git(main, add, ADD_TIMEOUT);
      if (!r.ok) throw new Error(r.err || "git worktree add failed.");
      this.close();
      run("space.new", { folder: path, machine: "" });
    } catch (error) {
      this.error = String(error).replace(/^Error: /, "");
    }
    this.busy = "";
    cx.notify();
  }

  key(e, cx) {
    if (e.key === "escape") return this.close();
    const n = this.shown().length;
    if (e.key === "enter") this.go(cx);
    else if ((e.key === "down" || e.key === "up") && n) this.at = (this.at + (e.key === "down" ? 1 : n - 1)) % n;
    else return;
    cx.stop_propagation();
    cx.notify();
  }

  row(r, i, open, t) {
    const on = i === this.at;
    const there = r.kind === "checkout" ? open.get(r.path) : null;
    const tag = r.kind === "new" ? "new branch" : r.kind === "branch" ? (r.remote ? "origin" : "local") : r.main ? "main checkout" : there ? "open" : "";
    const dest = r.kind === "checkout" ? "" : this.root ? `${tilde(this.root, this.userHome)}/${r.branch.replace(/\//g, "-")}` : "…";
    const sub = r.kind === "checkout" ? tilde(r.path, this.userHome) : r.kind === "branch" ? `no checkout yet: ↵ creates ${dest}` : `off origin/<default>: ↵ creates ${dest}`;
    return v_flex()
      .id(`row-${i}`)
      .flex_none()
      .h(44)
      .px(10)
      .justify_center()
      .rounded(7)
      .cursor_pointer()
      .when(on, (el) => el.bg(t.selected))
      .when(!on, (el) => el.hover((s) => s.bg(t.hover)))
      .on_click((_e, cx) => { this.at = i; this.go(cx); })
      .child(
        h_flex()
          .gap(8)
          .items_center()
          .child(div().flex_1().min_w(0).truncate().text_color(t.foreground).child(r.branch ?? "detached"))
          .when(Boolean(tag), (el) => el.child(div().flex_none().text_size(11).text_color(there || r.kind === "new" ? t.accent : t.dim).child(tag)))
          .when(!there && r.at > 0, (el) => el.child(div().flex_none().text_size(11).text_color(t.dim).child(ago(r.at)))),
      )
      .child(div().truncate().text_size(11.5).text_color(t.dim).child(sub));
  }

  render() {
    const t = theme();
    const r = this.repo;
    const rows = this.shown();
    const open = r ? spacesOf(this.rows.filter((x) => x.kind === "checkout")) : new Map();
    const title = `Worktrees${r ? ` of ${r.name}` : ""}`;
    const count = r ? `${this.rows.filter((x) => x.kind === "checkout").length} checkouts · ${this.rows.filter((x) => x.kind === "branch").length} branches${this.fetched ? "" : " (fetching…)"}` : "";
    return v_flex()
      .id("worktrees")
      .track_focus(this.handle)
      .on_key_down((e, cx) => this.key(e, cx))
      .size_full()
      .p(16)
      .gap(10)
      .bg(t.popover)
      .text_size(13)
      .child(h_flex().child(div().flex_1().font_semibold().text_color(t.foreground).child(title)).child(div().text_size(11).text_color(t.dim).child(count)))
      .when(r === undefined, (el) => el.child(div().text_color(t.muted).child("…")))
      .when(r === null, (el) => el.child(div().text_color(t.muted).child(this.error || `${this.folder ? tilde(this.folder, this.userHome) : "This folder"} is not in a git repository.`)))
      .when(Boolean(r), (el) =>
        el
          .child(Input.new(this.input).h(32).px(10).items_center().rounded(6).border(1).border_color(this.inputFocused ? t.accent : t.input).bg(t.background).text_color(t.foreground))
          .child(
            div()
              .relative()
              .flex_1()
              .min_h(0)
              .child(
                v_flex()
                  .id("worktrees-list")
                  .size_full()
                  .overflow_y_scroll()
                  .children(rows.map((row, i) => this.row(row, i, open, t)))
                  .when(rows.length === 0, (el) => el.child(div().p(10).text_color(t.dim).child("Nothing matches.")))
                  .when(rows.hidden > 0, (el) => el.child(div().flex_none().p(10).text_size(11).text_color(t.dim).child(`${rows.hidden} more: type to narrow`))),
              )
              .child(Scrollbar.vertical("worktrees-list").absolute().inset_0()),
          ),
      )
      .when(Boolean(r) && Boolean(this.error), (el) => el.child(div().text_color(t.blocked).child(this.error)))
      .child(
        h_flex()
          .justify_end()
          .text_size(11)
          .text_color(t.dim)
          .gap(6)
          .when(Boolean(this.busy), (el) => el.child(div().frames(["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"], 90)))
          .child(this.busy ? `git worktree add ${this.busy}…` : "↑↓ choose · ↵ open or create · esc close"),
      );
  }
}

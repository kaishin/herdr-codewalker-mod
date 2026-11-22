import { div, svg, PathBuilder } from "gpui-kit";
import { h_flex, v_flex, Button, InputState, Input, v_virtual_list, Scrollbar, VirtualListScrollHandle } from "gpui-base";
import { theme, settings } from "herdr";
import { HistoryClient, HISTORY_LIMIT } from "./history-client.js";
import { layoutLanes } from "./lanes.js";
import { ago, refLabel } from "./history-model.js";

const stop = (_event, cx) => cx.stop_propagation();
const ROW = 48, LANE = 12, FIRST = 6;
const secondary = text => div().min_w(0).truncate().text_size(11).text_color(theme().muted).child(text);

// The same native lane drawing, row geometry and branch picker as herdr-git-mod.
// Selection opens a Codewalker comparison; no Git write actions are imported.
function graphCell(row, head, width) {
  const t = theme(), colors = [t.accent, t.ansi2, t.ansi5, t.ansi3, t.ansi6, t.ansi4], paths = new Map();
  const x = lane => FIRST + lane * LANE, y = fraction => fraction * ROW;
  for (const [a, ya, b, yb, c] of row.segments) {
    const color = colors[c % colors.length];
    let path = (paths.get(color) || PathBuilder.stroke(1.5)).move_to(x(a), y(ya));
    if (a === b) path = path.line_to(x(b), y(yb));
    else { const mid = (y(ya) + y(yb)) / 2; path = path.cubic_bezier_to(x(b), y(yb), x(a), mid, x(b), mid); }
    paths.set(color, path);
  }
  const color = colors[row.lane % colors.length];
  return div().relative().flex_none().w(width).h(ROW).overflow_hidden()
    .children([...paths].map(([color, path]) => window.paint_path(path.build(), color).absolute().top(0).left(0).size_full()))
    .child(div().absolute().left(x(row.lane) - 4).top(ROW / 2 - 4).size(8).rounded(4).bg(head ? t.background : color)
      .when(head, el => el.border(2).border_color(color)));
}

export class History {
  constructor(execute, where) {
    this.execute = execute;
    this.search = InputState.new({ placeholder: "Find a base branch" });
    this.search.on("change", (_event, cx) => cx.notify());
    this.scroll = { commits: VirtualListScrollHandle.new(), branches: VirtualListScrollHandle.new() };
    this.listId = `review-history-${Math.random().toString(36).slice(2)}`;
    this.reset(where);
  }

  reset(where) {
    this.client = new HistoryClient(this.execute, where);
    this.data = this.graph = this.token = this.error = this.cursor = null;
    this.loading = this.picking = false;
    this.entry = null;
    this.tree = true;
    this.limit = 150;
    this.search.set_value("");
  }

  cancel() { this.token = null; this.loading = false; }

  refresh(host, cx, quiet = false) {
    if (!host.isHistory() || !host.folder || (quiet && this.loading)) return;
    const token = this.token = {}, client = this.client;
    this.loading = true;
    cx.spawn(async cx => {
      try {
        const data = await client.snapshot(this.limit);
        if (this.token !== token || !host.isHistory()) return;
        this.data = data;
        this.graph = layoutLanes(data.commits);
        this.error = null;
      } catch (error) { if (this.token === token) this.error = `Cannot load history. Refresh to retry. ${error.message}`; }
      finally { if (this.token === token) this.loading = false; cx.notify(); }
    });
    cx.notify();
  }

  busy(host) { return Boolean(host.saving || host.leaving || host.sending || host.viewing || host.editing?.saving); }

  branches() {
    const query = this.search.value().toLowerCase();
    return (this.data?.branches || []).filter(branch => branch.name.toLowerCase().includes(query));
  }

  activate(host, kind, item, cx) {
    if (!host.isHistory() || this.busy(host)) return;
    host.handle.focus();
    this.cursor = kind === "branch" ? item.ref : item.hash;
    host.selectHistory(kind, this.cursor, cx);
  }

  onKey(host, event, cx) {
    if (!host.isHistory() || host.mode !== "files" || host.intent || host.editing || host.editRoot || !this.tree || (this.entry && !this.picking) || this.busy(host)) return false;
    if (!["up", "down", "enter"].includes(event.key)) return false;
    const rows = this.picking ? this.branches() : this.data?.commits || [];
    if (!rows.length) return false;
    const key = item => this.picking ? item.ref : item.hash;
    const at = rows.findIndex(item => key(item) === (this.cursor || host.commitOid));
    const next = event.key === "enter" ? Math.max(0, at) : Math.max(0, Math.min(rows.length - 1, at + (event.key === "down" ? 1 : -1)));
    this.cursor = key(rows[next]);
    this.scroll[this.picking ? "branches" : "commits"].scroll_to_item(next, "center");
    if (event.key === "enter" || !this.picking) this.activate(host, this.picking ? "branch" : "commit", rows[next], cx);
    cx.notify();
    return true;
  }

  compareBranch(host, cx) {
    if (!host.isHistory() || this.busy(host)) return;
    const base = this.data?.defaultBase;
    if (base) host.selectHistory("branch", base, cx);
    else { this.entry = "base"; this.picking = false; host.baseInput.focus(); cx.notify(); }
  }

  dismiss() { this.entry = null; this.picking = false; }

  openEntry(kind, host, cx) {
    if (!host.isHistory() || this.busy(host)) return;
    this.entry = this.entry === kind ? null : kind;
    this.picking = false;
    if (this.entry) (kind === "base" ? host.baseInput : host.commitInput).focus();
    cx.notify();
  }

  openBranches(host, cx) {
    if (!host.isHistory() || this.busy(host)) return;
    this.entry = null;
    this.picking = this.tree = true;
    this.cursor = null;
    this.search.set_value("");
    this.search.focus();
    cx.notify();
  }

  controls(host) {
    const t = theme(), enabled = !this.busy(host), data = this.data;
    const base = refLabel(host.base || data?.defaultBase || "HEAD"), current = host.branch || data?.branch || "HEAD (detached)";
    const commit = host.source === "commit", oid = host.commitOid || host.commitRef;
    const parent = host.commitParents === 0 ? "Empty tree" : `First parent ${host.resolvedBase?.slice(0, 8) || "(loading…)"}`;
    const defaultName = data?.defaultBase ? refLabel(data.defaultBase) : null;
    // Secondary actions are quiet; only the comparison and a pending form lead.
    const action = (id, label, callback, selected = false, tooltip = label) => Button.new(id).h(22).px(6).rounded(4).border_1().border_color(t.border).text_size(11)
      .bg(selected ? t.selected : "#00000000").text_color(enabled ? t.muted : t.dim).disabled(!enabled)
      .when(enabled, el => el.cursor_pointer().hover(s => s.bg(t.hover).text_color(t.foreground)))
      .accessibility_label(label).tooltip(tooltip).on_mouse_down("left", stop)
      .on_click((_event, cx) => { if (enabled) callback(cx); }).child(label);
    const field = (state, label) => div().flex_1().min_w(0).h(26).px(8).rounded(6).bg(t.background).border_1().border_color(t.input).cursor_text().on_mouse_down("left", stop)
      .child(Input.new(state).h_full().text_size(12).text_color(t.foreground).accessibility_label(label));
    const close = () => host.button("history-close-entry", "", cx => { this.dismiss(); host.handle.focus(); cx.notify(); },
      { small: true, ghost: true, icon: "mods/Codewalker/icons/x.svg", tooltip: "Close without changing the comparison" });
    return v_flex().id("history-comparison").flex_none().min_w(0).px(14).pt(10).pb(6).gap(4).border_b(1).border_color(t.border)
      .child(h_flex().min_h(26).gap(8).flex_wrap().items_center()
        .child(h_flex().flex_1().min_w(0).gap(6).items_center().text_size(13).font_semibold()
          .when(commit, el => el.child(div().min_w(0).truncate().tooltip(oid).child(`Commit ${oid.slice(0, 8)}`)))
          .when(!commit, el => el
            .child(svg("mods/Codewalker/icons/git-branch.svg").size(12).flex_none().text_color(t.muted))
            .child(div().min_w(0).truncate().tooltip(current).child(current))
            .child(div().flex_none().text_size(11).font_normal().text_color(t.muted).child("vs"))
            .child(Button.new("history-branches").h(26).min_w(0).px(6).rounded(4).text_size(13).text_color(t.foreground)
              .disabled(!enabled).cursor_pointer().hover(s => s.bg(t.hover)).accessibility_label(`Choose base branch, selected ${base}`)
              .tooltip(`Choose a base branch · ${host.base || base} · does not check out`).on_mouse_down("left", stop)
              .on_click((_event, cx) => this.openBranches(host, cx))
              .child(div().min_w(0).truncate().child(base)))))
        .when(commit || Boolean(defaultName && defaultName !== base), el => el.child(host.button("history-branch-changes", defaultName ? `Branch vs ${defaultName}` : "Back to branch", cx => this.compareBranch(host, cx), {
          small: true, enabled,
          tooltip: defaultName ? `Compare ${current} against ${defaultName}${data.defaultBaseKind === "fallback" ? " · local fallback; remote default unknown" : " · default branch"}. Includes uncommitted changes; does not check out.` : "Choose a branch base before leaving this commit comparison.",
        }))))
      .child(div().text_size(11).text_color(t.muted).tooltip(commit ? `${host.resolvedBase || parent} → ${oid}` : `Merge base of ${host.base} and ${current} → working tree`)
        .child(commit ? `${parent} · no uncommitted changes` : "Since merge base · includes uncommitted changes"))
      .child(h_flex().min_h(22).gap(4).items_center()
        .when(!commit || this.entry === "base", el => el.child(action("history-edit-base", "Edit base", cx => this.openEntry("base", host, cx), this.entry === "base" || this.picking)))
        .child(action("history-go-commit", "Go to commit", cx => this.openEntry("commit", host, cx), this.entry === "commit"))
        .child(div().flex_1().min_w(0))
        .child(action("history-tree", this.tree ? "Hide tree" : "Show tree", cx => { this.tree = !this.tree; cx.notify(); })))
      .when(Boolean(this.entry), el => {
        const branch = this.entry === "base", state = branch ? host.baseInput : host.commitInput;
        return el.child(h_flex().id("history-ref-entry").min_h(34).gap(8).items_center()
          .child(field(state, branch ? "Base branch or ref" : "Commit SHA or ref").id(branch ? "history-base-input" : "history-commit-input"))
          .when(branch, el => el.child(host.button("history-browse-base", "", cx => this.openBranches(host, cx), {
            small: true, ghost: true, icon: "mods/Codewalker/icons/git-branch.svg", tooltip: "Choose a base branch from the list", enabled,
          })))
          .child(host.button(branch ? "apply-base" : "apply-commit", branch ? "Compare" : "Show", cx => host.applySource(branch ? "branch" : "commit", cx), {
            enabled: enabled && Boolean(state.value().trim()), tooltip: branch ? "Compare this base with the current branch and working tree" : "Show only this commit's changes against its first parent",
          }))
          .child(close()));
      })
      .when(this.picking, el => el.child(h_flex().min_h(34).gap(8).items_center()
        .child(field(this.search, "Find a base branch").id("history-base-search"))
        .child(close())));
  }

  list(host, kind, rows, render) {
    const name = `${this.listId}-${kind}`, data = this.data;
    const key = item => kind === "branches" ? item.ref : item.hash;
    return div().flex_1().min_h(0).min_w(0).relative()
      .child(v_virtual_list(name, rows.length, ROW, i => key(rows[i]), range => rows.slice(range.start, range.end).map((row, i) => render(row, range.start + i)))
        .track_scroll(this.scroll[kind]).size_full()
        .on_item_click((clicked, cx) => {
          const item = rows.find(row => key(row) === clicked);
          if (item && data === this.data) this.activate(host, kind === "branches" ? "branch" : "commit", item, cx);
        }))
      .child(Scrollbar.vertical(name).viewport_from_layout().absolute().inset_0());
  }

  view(host) {
    const t = theme(), data = this.data, graph = this.graph, commits = data?.commits || [], branches = this.branches();
    const graphWidth = Math.max(13, FIRST * 2 + ((graph?.width || 1) - 1) * LANE);
    return v_flex().id("review-history-tree").size_full().min_w(0).min_h(0)
      .when(Boolean(this.error), el => el.child(div().flex_none().px(14).py(8).text_size(12).text_color(t.blocked).child(this.error)))
      .when(this.picking && branches.length > 0, el => el.child(this.list(host, "branches", branches, branch => {
        const current = !branch.remote && branch.name === data.branch, selected = refLabel(host.base) === branch.name || this.cursor === branch.ref;
        return div().id(`history-branch-${branch.ref}`).flex().items_center().w_full().h(ROW).px(14).text_color(t.foreground).bg(selected ? t.selected : "#00000000")
          .hover(s => s.bg(t.hover)).cursor_pointer().accessibility_label(`Use ${branch.name} as base · does not check out`).tooltip(branch.ref)
          .child(h_flex().w_full().min_w(0).gap(8).items_center()
            .child(svg(`mods/Codewalker/icons/${current ? "check" : "git-branch"}.svg`).size(13).flex_none().text_color(current ? t.accent : t.muted))
            .child(v_flex().flex_1().min_w(0).gap(1)
              .child(div().truncate().text_size(12).line_height("16px").when(selected, el => el.font_semibold()).child(branch.name))
              .child(secondary(current ? "Current branch · use as base" : branch.remote ? "Remote base · last fetched state" : "Local base branch"))));
      })))
      .when(!this.picking && commits.length > 0, el => el.child(v_flex().id("review-history-horizontal").flex_1().min_h(0).min_w(0).overflow_x_scrollbar()
        .child(this.list(host, "commits", commits, (commit, index) => {
          const selected = host.source === "commit" && host.commitOid === commit.hash, refs = commit.refs.map(refLabel);
          return div().id(`history-commit-${commit.hash}`).flex().items_center().h(ROW).w_full().px(14).bg(selected ? t.selected : "#00000000").text_color(t.foreground)
            .hover(s => s.bg(selected ? t.selected : t.hover)).cursor_pointer().accessibility_label(`${commit.subject}, ${commit.hash.slice(0, 8)}`)
            .child(h_flex().w_full().h_full().min_w(0).items_center().gap(8)
              .child(graphCell(graph.rows[index], commit.hash === data.head, graphWidth))
              .child(v_flex().flex_1().min_w(0).gap(1)
                .child(div().truncate().text_size(12).line_height("16px").font_semibold().child(commit.subject))
                .child(h_flex().gap(6).min_w(0).items_center().line_height("14px")
                  .when(refs.length > 0, el => el.child(div().min_w(0).max_w(host.width < 560 ? 150 : 300).truncate().text_size(11).text_color(t.accent).tooltip(refs.join(" · ")).child(refs.join(" · "))))
                  .child(secondary(`${commit.author} · ${ago(commit.time)}`))))
              .when(host.width >= 560, el => el.child(div().flex_none().font_family(settings().font_family).text_size(11).text_color(t.dim).child(commit.hash.slice(0, 7)))));
        }).min_w(graphWidth + 220))))
      .when(this.picking ? !branches.length : !commits.length, el => el.child(div().px(14).py(12).text_size(12).text_color(t.muted)
        .child(this.loading ? "Reading history…" : this.picking ? "No matching branches. Try a shorter name." : this.error ? "Enter a ref manually to review changes." : "No commits yet. Your first commit will appear here.")))
      .when(!this.picking && Boolean(data?.more), el => el.child(h_flex().h(30).flex_none().px(14).items_center()
        .child(this.limit < HISTORY_LIMIT ? host.button("history-more", "Load older commits", cx => { this.limit += 150; this.refresh(host, cx); }, { small: true, enabled: !this.loading && !this.busy(host) })
          : secondary("1,500 commits shown · enter a SHA or ref for older commits"))));
  }
}

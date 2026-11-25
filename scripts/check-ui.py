#!/usr/bin/env python3
"""Check native review descriptions, including the full-height content contract.

This does not lay out or paint. A populated live Review screenshot is still required.
"""
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = '''import Codewalker from "./view.js";
import { layoutLanes } from "./lanes.js";
export default class Fixture extends Codewalker {
  init(props, cx) {
    super.init(props, cx);
    this.width = 1100; // A wide window unless a variant narrows it.
    this.folder = this.root = "/synthetic/example-checkout";
    this.reviewId = "synthetic-review"; this.snapshotId = "synthetic-snapshot"; this.revision = 4;
    this.branch = "example-feature"; this.file = "src/example.py";
    this.files = [{path:this.file,status:"M",additions:1,deletions:1,diffFingerprint:"synthetic-diff",viewed:false}];
    this.rowsFingerprint = "synthetic-diff";
    this.rows = [{kind:"@",text:"@@ -1,2 +1,2 @@"},{kind:"-",oldLine:1,newLine:null,text:"old_value = 1"},{kind:"+",oldLine:null,newLine:1,text:"new_value = 2"},{kind:" ",oldLine:2,newLine:2,text:"print(value)"}];
    const anchor = {side:"new",startLine:1,endLine:1,snippet:"new_value = 2"};
    this.threads = [{id:"synthetic-thread",path:this.file,anchor,status:"open",messages:[{id:"synthetic-human",actor:{kind:"human",id:"local",name:"You"},body:"Synthetic fixture: why change this value?",createdAt:"2026-01-01T10:00:00Z"},{id:"synthetic-agent",actor:{kind:"agent",id:"example",name:"Example agent"},body:"Synthetic fixture: it corrects the boundary condition.",createdAt:"2026-01-01T10:01:00Z"}]},{id:"synthetic-outdated",path:this.file,anchor:{...anchor,startLine:40,endLine:40,snippet:"original_code()"},outdated:true,status:"resolved",messages:[{id:"synthetic-old",actor:{kind:"human",id:"local",name:"You"},body:"Synthetic fixture: retained discussion.",createdAt:"2026-01-01T10:00:00Z"}]}];
    this.threads.push({...this.threads[0], id:"synthetic-gone", path:"src/removed.py", messages:[{...this.threads[0].messages[0], id:"synthetic-gone-human"}]});
    this.intent = {id:"synthetic-draft",path:this.file,threadId:"synthetic-thread",anchor,snapshotId:this.snapshotId,context:this.context()};
    this.composer.set_value("Synthetic reply draft");
    this.draftStatus = "Synthetic fixture · not written to disk";
    this.drafts = [{id:"synthetic-other-draft",path:"src/other.py",body:"Synthetic saved draft"}];
    const commits = [
      {hash:"a".repeat(40),parents:["b".repeat(40),"c".repeat(40)],subject:"Merge safe retries",author:"Example",time:1795212000,refs:["HEAD -> refs/heads/example-feature"]},
      {hash:"b".repeat(40),parents:["c".repeat(40)],subject:"Retry jobs once",author:"Example",time:1795211000,refs:[]},
      {hash:"c".repeat(40),parents:[],subject:"Create queue",author:"Example",time:1795210000,refs:["refs/heads/main"]},
    ];
    this.history.data = {defaultBase:"refs/heads/main",defaultBaseKind:"fallback",root:this.root,branch:this.branch,head:commits[0].hash,commits,branches:[
      {ref:"refs/heads/example-feature",name:"example-feature",remote:false},
      {ref:"refs/heads/main",name:"main",remote:false},
      {ref:"refs/remotes/origin/main",name:"origin/main",remote:true},
    ]};
    this.history.graph = layoutLanes(commits);
    this.pendingFile = PENDING;
    EXTRA
  }
}
'''


STRANDED = "a reload stranded script tasks that will never settle"


def warnings(stderr):
    """stderr without the check harness's own reload notice."""
    return "\n".join(line for line in stderr.splitlines() if STRANDED not in line)


def check_content_height(spec, stacked=False):
    # Inspect the runtime-built description, not source text. gpui-base h_flex
    # centers children by default: these two columns must instead fill its height
    # or their flexible scroll regions collapse even when review data is loaded.
    stack = []
    regions = []
    for line in spec.splitlines():
        depth = len(line) - len(line.lstrip())
        while stack and stack[-1][0] >= depth:
            stack.pop()
        if ':id[Str("review-changed-paths")]' in line or ':id[Str("review-diff-' in line:
            regions.append(stack.copy())
        stack.append((depth, line))
    assert len(regions) == 2, "Both review scroll regions must be rendered"
    hunks = [line for line in spec.splitlines() if ':id[Str("hunk-' in line]
    assert hunks and all(" .border_" not in line for line in hunks), "Hunk separators keep their background but have no borders"
    common = 0
    while common < min(map(len, regions)) and regions[0][common] == regions[1][common]:
        common += 1
    assert common, "Review scroll regions must share a content row"
    row = regions[0][common - 1][1]
    columns = [region[common][1] for region in regions]
    if stacked:
        # Native panels own the drag handle and keep their sizes across repaints.
        assert row.lstrip().startswith('v_resizable "review-stacked"'), "Every stacked comparison must have a native draggable divider"
        assert all(column.lstrip().startswith("resizable_panel ") for column in columns), "Both files and diff belong to the resize group"
        assert ':panel_size[Number(' in columns[0] and ':size_range[Number(96.0)]' in columns[0], "Files have an initial size and a usable minimum"
        assert ':size_range[Number(120.0)]' in columns[1], "The diff cannot be resized away"
        contents = [region[common + 1][1] for region in regions]
        assert all(' .h_full' in column and ' .min_h[Number(0.0)]' in column for column in contents), "Both scroll areas must fill their resized native panels"
        return
    assert row.lstrip().startswith("h_flex "), "Review columns must share a row"
    assert " .items_stretch" in row or all(" .h_full" in column for column in columns), (
        "Review content columns do not fill the row: h_flex defaults to items_center; "
        "file list and diff scroll regions can collapse to zero height"
    )


def code(spec):
    """The spec's text in order: a syntax-colored row is one piece per color."""
    return "".join(re.findall(r'^\s*text "(.*)"$', spec, re.M))


def check_walkthrough_height(spec, stacked):
    """The outline and the step scroll in two regions that fill one row or resize group."""
    lines = spec.splitlines()
    depth = lambda i: len(lines[i]) - len(lines[i].lstrip())

    def ancestors(i):
        chain, level = [], depth(i)
        for j in range(i - 1, -1, -1):
            if lines[j].strip() and depth(j) < level:
                chain.append(j)
                level = depth(j)
        return chain

    regions = [next(i for i, line in enumerate(lines) if marker in line)
               for marker in (':id[Str("walkthrough-outline")]', ':id[Str("walkthrough-content-')]
    for i in regions:
        assert ' .flex_1' in lines[i] and ' .min_h[Number(0.0)]' in lines[i] and ':overflow_y_scrollbar' in lines[i], "Both walkthrough regions scroll in the remaining height"
    common = next(j for j in ancestors(regions[0]) if j in ancestors(regions[1]))
    row = lines[common].lstrip()
    if stacked:
        assert row.startswith('v_resizable "walkthrough-stacked"'), "A narrow walkthrough stacks the outline over the step with a native divider"
    else:
        assert row.startswith("h_flex ") and " .items_stretch" in row, "A wide walkthrough keeps the outline beside the step at full height"


def check_siblings(spec, *names):
    """The named elements share one parent row, in this order."""
    lines = spec.splitlines()
    found = [next(i for i, line in enumerate(lines) if line.lstrip().startswith(name)) for name in names]
    depth = {len(lines[i]) - len(lines[i].lstrip()) for i in found}
    assert len(depth) == 1 and found == sorted(found), f"{names} must sit side by side"
    level = depth.pop()
    # Nothing between them leaves their parent.
    assert all(len(line) - len(line.lstrip()) >= level for line in lines[found[0]:found[-1]] if line.strip()), f"{names} must share one row"


def check_thread_header(spec, thread, open):
    """A thread header wraps instead of clipping; only an open card has a divider under it."""
    lines = spec.splitlines()
    at = next(i for i, line in enumerate(lines) if f':id[Str("thread-header-{thread}")]' in line)
    header, depth = lines[at], len(lines[at]) - len(lines[at].lstrip())
    groups = [line for line in lines[at + 1:at + 40] if len(line) - len(line.lstrip()) == depth + 2]
    groups = groups[:2]
    assert header.lstrip().startswith("h_flex ") and " .flex_wrap" in header, "The thread header wraps; a fixed row clips Show/Hide and Reopen in a narrow panel"
    assert " .flex_wrap" in groups[0] and " .min_w[Number(0.0)]" in groups[0] and " .flex_basis" in groups[0], "Labels wrap and shrink inside their own group"
    assert " .flex_none" in groups[1] and " .ml_auto" in groups[1], "Actions keep their size and move to their own line together"
    body = "\n".join(lines[at:at + 40])
    assert body.index(f'Button "status-{thread}"') > body.index(groups[1].strip()), "Resolve or Reopen sits in the action group"
    assert (" .border_b[" in header) == open, "Only an open card draws a divider under its header"


WALK = r"""
    const wt = {id:"synthetic-walkthrough", title:"How retries became safe", summary:"Synthetic fixture: **two** edits make retries idempotent.", status:"open",
      author:{kind:"agent",id:"example",name:"Example agent"}, createdAt:"2026-01-01T10:00:00Z", updatedAt:"2026-01-01T10:05:00Z", steps:[]};
    const agent = {kind:"agent",id:"example",name:"Example agent"}, human = {kind:"human",id:"local",name:"You"};
    const thread = (n, body, extra = {}) => ({id:`synthetic-step-thread-${n}`, path:[null, "src/example.py", "src/caller.py"][n], status:"open", awaitingReply:false, outdated:n === 2,
      anchor:{walkthroughId:wt.id, stepId:`step-${n}`, validity:n === 2 ? "stale" : "current"},
      walkthrough:{id:wt.id, title:wt.title, stepId:`step-${n}`, position:n, stepTitle:["Overview","Stable job ID","Caller"][n], stepCount:3},
      messages:[{id:`synthetic-step-body-${n}`, actor:agent, body, createdAt:"2026-01-01T10:00:00Z"}], ...extra});
    wt.steps = [
      {id:"step-0", position:0, title:"Overview", view:null, validity:"current", thread:thread(0, "Synthetic fixture: read this **first**.")},
      {id:"step-1", position:1, title:"Stable job ID", validity:"current",
        view:{kind:"diff", path:"src/example.py", oldPath:null, status:"modified", side:"new", startLine:1, endLine:1,
          hunks:[{rows:[{kind:"delete",oldLine:1,newLine:null,text:"old_value = 1"},{kind:"add",oldLine:null,newLine:1,text:"new_value = 2"}]}]},
        thread:thread(1, "Synthetic fixture: the ID now comes from the payload.", {messages:[
          {id:"synthetic-step-body-1", actor:agent, body:"Synthetic fixture: the ID now comes from the payload.", createdAt:"2026-01-01T10:00:00Z"},
          {id:"synthetic-step-reply", actor:human, body:"Synthetic fixture: what if the payload has no ID?", createdAt:"2026-01-01T10:02:00Z"}]})},
      {id:"step-2", position:2, title:"Caller", validity:"stale",
        view:{kind:"source", path:"src/caller.py", rev:"head", startLine:10, endLine:12,
          hunks:[{rows:[{kind:"context",oldLine:null,newLine:10,text:"queue.retry(job)"}]},{rows:[{kind:"context",oldLine:null,newLine:12,text:"queue.flush()"}]}]},
        thread:thread(2, "Synthetic fixture: the caller is unchanged.")},
    ];
    this.walkthroughId = wt.id; this.walkthrough = wt; this.step = 1;
    this.threads = wt.steps.map(step => step.thread); this.files = []; this.rows = []; this.drafts = [];
    this.intent = null; this.composer.set_value(""); this.history.data = this.history.graph = null;
"""
SUMMARY = 'this.walkthroughs = [{id:"synthetic-walkthrough", title:"How retries became safe", summary:"", status:"open", author:{kind:"agent",id:"example",name:"Example agent"}, createdAt:"2026-01-01T10:00:00Z", updatedAt:"2026-01-01T10:05:00Z", stepCount:3, openThreads:3, awaitingReply:1, replies:1}, {id:"synthetic-archived", title:"Old queue notes", summary:"", status:"archived", author:{kind:"agent",id:"example",name:"Example agent"}, createdAt:"2025-12-01T10:00:00Z", updatedAt:"2025-12-01T10:05:00Z", stepCount:1, openThreads:0, awaitingReply:0, replies:0}];'


def main():
    # Source is the reviewed authored module; no downloaded helpers or live app configuration.
    with tempfile.TemporaryDirectory(prefix="codewalker-native-fixture-") as temp:
        destination = Path(temp).resolve() / "mod"
        destination.mkdir()
        for name in ("main.js", "client.js", "model.js", "dock-expand.js", "pr-model.js", "pr-client.js", "pull-requests.js", "history.js", "history-client.js", "history-model.js", "lanes.js", "highlight.js", "walkthrough.js", "walkthrough-model.js", "mod.json", "review.svg"):
            shutil.copy2(ROOT / "mods/Codewalker" / name, destination / ("view.js" if name == "main.js" else name))
        shutil.copytree(ROOT / "mods/Codewalker/icons", destination / "icons")
        # Settled; a file switch in progress (the shown diff stays, dimmed); a
        # hovered row while a new two-line comment is open; docked at the
        # panel's default width with a thread awaiting a reply in thread.
        hover = 'this.hoverRow = 2; this.intent = {...this.intent, threadId:null, anchor:{side:"new",startLine:1,endLine:2}};'
        docked = 'this.width = 368; this.height = 900; this.threads[0] = {...this.threads[0], awaitingReply:true}; this.intent = {...this.intent, replyRequested:true};'
        listing = 'this.width = 368; this.height = 900; this.mode = "discussions";'
        history = listing + ' this.filter = "all"; this.intent = null;'
        clearing = history + ' this.clearAttempt = {context:this.context(), commandId:"clear-fixture", revision:this.revision};'
        uncertain = clearing + ' this.clearAttempt.uncertain = true;'
        editing = 'this.editing = {id:"synthetic-human", body:"Synthetic fixture: why change this value?", commandId:"c"}; this.editor.set_value(this.editing.body);'
        deleting = 'this.confirmDelete = "synthetic-human";'
        sending = 'this.width = 368; this.height = 900; this.mode = "send";'
        viewed = 'this.width = 368; this.height = 900; this.files[0].viewed = true;'
        hidden = viewed + ' this.hideViewed = true;'
        branch = 'this.width = 368; this.height = 900; this.source = "branch"; this.base = "main"; this.baseInput.set_value("main"); this.history.entry = "base";'
        commit = 'this.width = 368; this.height = 900; this.source = "commit"; this.commitOid = "0123456789abcdef"; this.commitParents = 2; this.resolvedBase = "b".repeat(40); this.commitInput.set_value("HEAD~1"); this.history.entry = "commit";'
        tree = branch + ' this.history.entry = null;'
        picker = tree + ' this.history.picking = true;'
        tree_wide = tree + ' this.width = 1100;'
        tree_error = tree + ' this.history.data = this.history.graph = null; this.history.error = "Cannot load history. Refresh to retry.";'
        cached = 'this.width = 368; this.height = 900; this.rememberComparison(); this.resetContext({keepComparisons:true}); this.restoreComparison(); this.reading = true;'
        # A checkout on another machine. Nothing here starts a process on it.
        remote = 'this.width = 368; this.height = 900; this.on = "synthetic-machine";'
        expandable = 'this.width = 368; this.expansionState = () => ({supported:true, expanded:false, mode:"fill"});'
        expanded = 'this.width = 1100; this.expansionState = () => ({supported:true, expanded:true, mode:"fill"});'
        # A resolved discussion outside the shown lines, opened; then none outside, with long labels.
        unfolded = 'this.width = 300; this.height = 900; this.filter = "all"; this.expanded.add("synthetic-outdated");'
        inside = 'this.width = 300; this.height = 900; this.threads = [{...this.threads[0], awaitingReply:true}];'
        # Folded, the count is the only sign of a long thread; its messages are not rendered.
        crowded = 'this.filter = "all"; this.width = 300; this.height = 900; this.threads[1] = {...this.threads[1], awaitingReply:true, messages:Array.from({length:128}, (_, i) => ({...this.threads[1].messages[0], id:`synthetic-old-${i}`}))};'
        pr = 'this.width = 368; this.height = 900; this.source = "pr"; this.prTab = "changes"; this.prBranch = "example-feature"; this.prHead = "a".repeat(40); this.prBase = "b".repeat(40); this.pullRequest = {number:7,title:"Make retries safe",url:"https://github.com/example/taskboard/pull/7",author:{login:"example"},headRefName:"example-feature",baseRefName:"main",body:"Use a stable job ID.",checks:{kind:"passed",label:"1 passed"},checksList:[{name:"Tests",state:"SUCCESS"}],reviewDecision:"APPROVED"};'
        pr_description = pr + ' this.prTab = "description";'
        pr_description_wide = pr_description + ' this.width = 1100; this.pullRequest.body = "Long PR description\\n\\n".repeat(80);'
        no_pr = 'this.width = 300; this.source = "pr"; this.pullRequest = null; this.prHead = null; this.intent = null; this.files = []; this.threads = []; this.rows = []; this.drafts = []; this.pullRequests.previous = {branch:"example-feature",pr:null};'
        detached = no_pr + ' this.pullRequests.previous.branch = null;'
        pr_loading = no_pr + ' this.pullRequests.previous = null; this.pullRequests.loading = true;'
        pr_error = no_pr + ' this.pullRequests.previous = null; this.error = "Sign in to GitHub CLI";'
        for pending, extra in (("null", tree), ("null", picker), ("null", tree_wide), ("null", tree_error), ("null", pr), ("null", pr_description), ("null", pr_description_wide), ("null", no_pr), ("null", detached), ("null", pr_loading), ("null", pr_error), ("null", expandable), ("null", expanded), ("null", cached), ("null", ""), ('"src/other.py"', ""), ("null", hover), ("null", docked), ("null", listing), ("null", history), ("null", clearing), ("null", uncertain), ("null", editing), ("null", deleting), ("null", sending), ("null", viewed), ("null", hidden), ("null", branch), ("null", commit), ("null", remote), ("null", unfolded), ("null", inside), ("null", crowded)):
            (destination / "main.js").write_text(FIXTURE.replace("PENDING", pending).replace("EXTRA", extra))
            result = subprocess.run(["herdr-app", "check", str(destination), "--print-spec"],
                                    check=True, capture_output=True, text=True)
            spec = result.stdout
            narrow = extra in (tree, picker, tree_error, pr, expandable, docked, viewed, hidden, branch, commit, cached, remote, unfolded, inside, crowded)
            if extra in (no_pr, detached, pr_loading, pr_error):
                assert 'Button "scope-pr"' in spec, "PR remains a top comparison tab"
                assert ':id[Str("review-diff-' not in spec and ':id[Str("pr-description-' not in spec, "A branch without an open PR has no comparison or metadata"
                assert 'Select a pull request' not in spec, "No repository-wide picker"
                assert ('"No open pull request"' in spec) == (extra in (no_pr, detached)), "Only a completed lookup can report an absent PR"
                assert ('"Loading pull request…"' in spec) == (extra == pr_loading), "Initial discovery has a separate loading state"
                if extra == no_pr:
                    assert 'Refresh after you open one.' in spec, "The empty state explains how to get a PR"
                if extra == detached:
                    assert 'Check out a branch' in spec, "Detached HEAD explains how to start a PR review"
                if extra == pr_error:
                    assert 'Sign in to GitHub CLI' in spec, "Lookup errors remain visible instead of claiming no PR"
                assert "WARN" not in warnings(result.stderr), result.stderr
                continue
            if extra in (pr, pr_description, pr_description_wide):
                check_siblings(spec, 'Button "pr-tab-description"', 'Button "pr-tab-changes"')
                frames = [next(line for line in spec.splitlines() if f':id[Str("{name}-segments")]' in line).split(')]', 1)[1]
                          for name in ("scope", "pr-tab")]
                assert frames[0] == frames[1], "PR and scope controls use the same segmented frame, spacing and colors"
                for name in ("pr-tab-description", "pr-tab-changes"):
                    segment = next(line for line in spec.splitlines() if f'Button "{name}"' in line)
                    assert '.h[Number(20.0)]' in segment and '.rounded[Number(5.0)]' in segment and '.border_b[' not in segment, "PR segments match the top control instead of using underline tabs"
                assert 'Button "pr-github"' in spec and 'Button "choose-pr"' not in spec, "Only the current PR is available"
                assert spec.count('"#7"') == 1 and 'Open pull request #7 on GitHub' in spec, "The PR number labels the GitHub button, without a separate trailing number"
                assert 'Hide details' not in spec, "Tabs replace the collapsible details block"
                description = extra != pr
                assert (':id[Str("pr-description-' in spec) == description, "Description and Changes are separate content areas"
                if description:
                    scroll = next(line for line in spec.splitlines() if ':id[Str("pr-description-' in line)
                    assert '.flex_1' in scroll and '.min_h[Number(0.0)]' in scroll and ':overflow_y_scrollbar' in scroll and '.max_h' not in scroll, "Description scrolls through all remaining height, without the old fixed cap"
                    assert ':id[Str("review-diff-' not in spec and ':id[Str("review-changed-paths")]' not in spec, "Description does not share height with the diff"
                    assert 'Make retries safe' in spec and 'Approved' in spec and '1 passed' in spec and 'Tests' in spec
                    assert "WARN" not in warnings(result.stderr), result.stderr
                    continue
            if extra not in (listing, history, clearing, uncertain, sending):
                outside = '"Discussions outside the shown lines"' in spec
                assert outside == (extra in (unfolded, crowded)), "Only a thread with no shown line is listed outside the shown lines"
                check_thread_header(spec, "synthetic-thread", True)
                if outside:
                    check_thread_header(spec, "synthetic-outdated", extra == unfolded)
                    assert ('"1 message"' in spec) == (extra not in (unfolded, crowded)) and ('"128 messages"' in spec) == (extra == crowded), "A folded card names its message count"
                    assert f'Button "toggle-synthetic-outdated"' in spec and ('"Hide"' in spec) == (extra == unfolded), "A resolved card offers Show, then Hide"
            lines = spec.splitlines()
            root = next(i for i, line in enumerate(lines) if ':id[Str("codewalker")]' in line)
            assert ':id[Str("review-controls")]' in lines[root + 1], "Scope and actions are the first row; no title row above them"
            check_siblings(spec, 'Button "refresh"', 'Button "toggle-discussions"', 'Button "choose-checkout"')
            assert 'Button "toggle-walkthroughs"' not in spec, "Walkthroughs are offered only when an agent wrote one"
            assert ('Button "expand-review"' in spec) == (extra in (expandable, expanded)), "Expand is offered only for supported docked views"
            if extra in (expandable, expanded):
                check_siblings(spec, 'Button "refresh"', 'Button "toggle-discussions"', 'Button "choose-checkout"', 'Button "open-send"', 'Button "expand-review"')
                assert f"icons/{'minimize-2' if extra == expanded else 'maximize-2'}.svg" in spec
                assert ("Restore panel width" if extra == expanded else "Expand to fill the workspace") in spec
            assert ("synthetic-machine · 1 file" in spec) == (extra == remote), "A remote review names its machine beside the counts"
            assert 'text "HEAD → working tree' not in spec, "Comparison explanation belongs in a tooltip, not a text row"
            assert ('Button "apply-base"' in spec) == (extra == branch), "Edit base opens only a base editor"
            assert 'Button "scope-history"' in spec and 'Button "scope-commit"' not in spec and 'Button "scope-branch"' not in spec, "One History tab replaces Branch and Commit"
            assert ('Button "apply-commit"' in spec) == (extra == commit), "Go to commit opens only a commit editor"
            assert not (':id[Str("history-base-input")]' in spec and ':id[Str("history-commit-input")]' in spec), "Independent comparisons never share a two-field form"
            assert 'Enter ref' not in spec and 'From:' not in spec and 'To:' not in spec, "The combined ref control and duplicate comparison block are removed"
            if extra in (tree, picker, tree_wide, tree_error, branch, commit):
                assert ('Button "history-branch-changes"' in spec) == (extra == commit), "Return to branch stays visible in commit review, not redundantly in the default branch comparison"
                assert ':id[Str("history-comparison")]' in spec and 'Button "history-go-commit"' in spec, "One header owns the comparison and its secondary actions"
                for name in ("history-edit-base", "history-go-commit", "history-tree"):
                    button = next((line for line in spec.splitlines() if f'Button "{name}"' in line), None)
                    assert button is None or " .border_1" in button, "Secondary History buttons retain their borders"
                assert ('Button "history-edit-base"' in spec) == (extra != commit), "A base editor is not presented as a parameter of a single commit"
                if extra != commit:
                    assert 'Button "history-branches"' in spec and '"example-feature"' in spec and '"main"' in spec, "The primary branch heading names both sides and keeps the base selectable"
                    heading = spec[spec.index(':id[Str("history-comparison")]'):]
                    assert heading.index("icons/git-branch.svg") < heading.index('"example-feature"') < heading.index('Button "history-branches"'), "One branch icon leads the whole comparison, not the base selector"
                if extra == picker:
                    assert ':id[Str("history-base-search")]' in spec and ':id[Str("history-base-input")]' not in spec, "Branch search replaces manual entry rather than adding a second field"
                assert ':id[Str("review-history-tree")]' in spec and 'v_resizable "review-history-layout"' in spec, "The commit tree and review are independently resizable"
                assert ':id[Str("review-history-horizontal")]' in spec or extra in (picker, tree_error), "Commit lanes can scroll horizontally without clipping the diff"
                assert 'does not check out' in spec or extra == picker, "Branch actions compare without switching the checkout"
                if extra == tree_error:
                    assert 'Cannot load history. Refresh to retry.' in spec and 'history-edit-base' in spec, "History failure keeps manual entry and the diff available"
            if extra == commit:
                assert 'Commit 01234567' in spec and 'First parent bbbbbbbb · no uncommitted changes' in spec and 'Branch vs main' in spec, "Commit review names the parent and target once and makes the return to branch clear"
            if extra == cached:
                check_content_height(spec, stacked=True)
                assert 'new_value = 2' in code(spec) and 'Button "file-src/example.py"' in spec, "A restored mode shows its files and diff during revalidation"
                assert 'Reading changes' not in spec and 'Loading diff' not in spec and '.opacity' not in spec, "Cached mode switches must not blank or dim the diff"
                checkbox = next(line for line in lines if 'Checkbox "file-viewed"' in line)
                assert ':disabled[Bool(true)]' in checkbox, "Cached Viewed state is read-only until revalidated"
                resolve = next(line for line in lines if 'Button "status-synthetic-thread"' in line)
                assert ':disabled[Bool(true)]' in resolve, "Cached discussions cannot be mutated before revalidation"
                assert "WARN" not in warnings(result.stderr), result.stderr
                print(warnings(result.stderr).strip())
                continue
            if extra == sending:
                # The exact text to paste, agent choice, and a Send that waits for a ready agent.
                assert ':id[Str("review-send")]' in spec and 'Button "send-to-agent"' in spec, "Send view shows its preview and Send"
                assert "thread synthetic-thread" in spec and "Synthetic fixture: why change this value?" in spec, "The preview is the exact message"
                assert "WARN" not in warnings(result.stderr), result.stderr
                print(warnings(result.stderr).strip())
                continue
            if not extra:
                assert 'Button "show-history"' in spec, "Hidden history has an explicit recovery path"
                assert 'thread-synthetic-outdated' not in spec and 'Button "retained-src/removed.py"' not in spec, "The active review excludes resolved and absent-file discussions"
                assert 'Button "open-send"'  in spec, "Unsent comments offer Send in the toolbar"
                # Comment bodies are Markdown (TextView), not plain text.
                assert 'message-body-synthetic-human' in spec, "Comment bodies render as Markdown"
            if extra in (listing, history, clearing, uncertain):
                # Active is clean; All recovers history; clearing always asks first.
                assert ':id[Str("review-discussions")]' in spec and ':id[Str("review-diff-' not in spec, "Discussions replace the file list and diff"
                assert '"Show in diff"' in spec and ".border_l[Number(2.0)]" in spec, "Each thread links to its diff; replies are indented"
                assert 'Button "group-src/example.py"' in spec, "Threads group under their file"
                assert ('discussion-synthetic-outdated' in spec) == (extra != listing), "Resolved discussions are history, not active work"
                assert ('Button "group-src/removed.py"' in spec) == (extra != listing), "Absent files appear only in All"
                assert ('Button "clear-history"' in spec) == (extra == history), "Only All offers explicit clearing"
                assert ('Button "confirm-clear-history"' in spec) == (extra in (clearing, uncertain)), "Clearing needs confirmation"
                if extra == clearing:
                    assert '"Delete discussions"' in spec and '"Cancel"' in spec and 'Drafts and viewed marks are kept.' in spec
                if extra == uncertain:
                    assert '"Retry clear"' in spec and 'not to delete newer discussions' in spec
                assert "WARN" not in warnings(result.stderr), result.stderr
                print(warnings(result.stderr).strip())
                continue
            assert ('Checkbox "file-viewed"' in spec) == (pending == "null"), "Loaded changed files offer an explicit Viewed checkbox"
            assert 'Button "hide-viewed"' in spec, "The file list offers a reversible viewed filter"
            assert ('"1 of 1 viewed"' in spec) == (extra in (viewed, hidden)), "Progress reflects the saved marks"
            assert ('"All files viewed."' in spec) == (extra == hidden), "A filtered-empty list explains how it became empty"
            assert ('Button "file-src/example.py"' in spec) == (extra != hidden), "Hide viewed removes only marked file rows"
            # The first comment asks before it takes the thread; replies of others have no Delete.
            assert ('"Delete thread"' in spec) == (extra == deleting), "Deleting the comment asks first and names the thread"
            assert "delete-synthetic-agent" not in spec, "Only your own messages offer Delete"
            if not extra:
                # Resolve is in the card header, above the messages; Reply stays below them.
                order = [spec.index(name) for name in ('Button "status-synthetic-thread"', ':id[Str("message-synthetic-human")]', 'Button "reply-synthetic-thread"')]
                assert order == sorted(order), "Resolve belongs in the thread header, Reply under the messages"
                check_siblings(spec, 'Button "reply-synthetic-thread"', 'Button "message-edit-synthetic-human"', 'Button "message-delete-synthetic-human"')
            if extra == editing:
                # The message body becomes an editor with Cancel and Save; no second Edit.
                assert '"Save"' in spec and "message-cancel-edit" in spec and "edit-synthetic-human" not in spec, "An edited message shows its editor"
            check_content_height(spec, stacked=narrow)
            dimmed = ".opacity" in spec and 'new_value = 2' in code(spec)
            assert dimmed == (pending != "null"), "A pending file switch must keep the shown diff, dimmed"
            assert ("icons/plus.svg" in spec) == (extra == hover), "Only a hovered, anchorable row shows the comment button"
            assert ('"Comment on lines 1–2"' in spec) == (extra == hover), "A range comment names its lines"
            assert '"Reply in thread"' in spec, "Every open composer can ask for a reply in the thread"
            assert ("edit-synthetic-human" in spec) == (extra not in (editing, deleting)) and "edit-synthetic-agent" not in spec, "Only your own messages offer Edit"
            assert ('"Awaiting reply"' in spec) == (extra in (docked, inside, crowded)), "A thread owed a reply says so"
            # Resolve carries a check mark; a checked reply request adds one more.
            assert spec.count("icons/check.svg") == 1 + (extra == docked) + (2 if extra == viewed else 1 if extra == hidden else 0), "Resolve and a checked request show a check mark"
            if not extra:
                assert all(f"icons/{name}.svg" in spec for name in ("reply", "pencil", "trash")), "Reply, Edit and Delete show their icons"
            assert "WARN" not in warnings(result.stderr), result.stderr
            print(warnings(result.stderr).strip())
        # Walkthroughs: the pane an agent opens, and Review's list of them.
        walk = WALK
        scenes = {
            "wide": walk,
            "narrow": walk + 'this.width = 368; this.height = 900;',
            "reply": walk + 'this.intent = {id:"synthetic-step-draft", path:"src/example.py", anchor:this.threads[1].anchor, threadId:"synthetic-step-thread-1", walkthroughId:"synthetic-walkthrough", snapshotId:null, context:this.context()}; this.composer.set_value("Synthetic reply");',
            "stale": walk + 'this.step = 2;',
            "intro": walk + 'this.step = 0;',
            "delete": walk + 'this.walkthroughConfirm = "synthetic-walkthrough";',
            "send": walk + 'this.mode = "send";',
            "loading": walk + 'this.walkthrough = null; this.threads = []; this.reading = true;',
            "list": SUMMARY + ' this.mode = "walkthroughs"; this.width = 368; this.height = 900;',
            "listed": SUMMARY,
        }
        for name, extra in scenes.items():
            (destination / "main.js").write_text(FIXTURE.replace("PENDING", "null").replace("EXTRA", extra))
            result = subprocess.run(["herdr-app", "check", str(destination), "--print-spec"], check=True, capture_output=True, text=True)
            spec = result.stdout
            assert "WARN" not in warnings(result.stderr), result.stderr
            walking = name not in ("list", "listed")
            assert (':id[Str("walkthrough-controls")]' in spec) == walking and (':id[Str("review-controls")]' in spec) != walking, f"{name}: a walkthrough replaces Review's toolbar"
            if not walking:
                toggle = next(i for i, line in enumerate(spec.splitlines()) if 'Button "toggle-walkthroughs"' in line)
                assert ('"1"' in "\n".join(spec.splitlines()[toggle:toggle + 6])) == (name == "listed"), "Review offers its open walkthroughs, counted when there is room"
                assert (':id[Str("review-walkthroughs")]' in spec) == (name == "list"), "The list replaces the files and diff"
                if name == "list":
                    assert 'Button "walkthrough-synthetic-walkthrough"' in spec and '"Archived"' in spec and '"Awaiting reply"' in spec, "Each walkthrough opens; archived ones stay listed"
                    assert spec.index("How retries became safe") < spec.index("Old queue notes"), "Open walkthroughs come before archived ones"
                continue
            assert 'Button "walkthrough-back"' in spec and "icons/arrow-left.svg" in spec, f"{name}: Back returns to Review"
            assert 'Button "scope-uncommitted"' not in spec and 'Button "choose-checkout"' not in spec, f"{name}: Review's scope and folder controls are hidden"
            if name == "loading":
                assert '"Loading walkthrough…"' in spec, "A walkthrough loads before it shows"
                continue
            if name == "send":
                assert ':id[Str("review-send")]' in spec and 'Walkthrough \\"How retries became safe\\", step 2 of 3' in spec, "Send names the walkthrough step"
                assert "is not repeated above" in spec and "what if the payload has no ID?" in spec, "Send pastes the replies, not the agent's own step"
                continue
            check_walkthrough_height(spec, stacked=name == "narrow")
            assert all(f'Button "walkthrough-step-{i}"' in spec for i in range(3)), f"{name}: every step is in the outline"
            assert 'walkthrough-summary-synthetic-walkthrough' in spec, f"{name}: the summary renders as Markdown"
            step = {"stale": 2, "intro": 0}.get(name, 1)
            assert f'"Step {step + 1} of 3"' in spec and f'walkthrough-body-synthetic-step-body-{step}' in spec, f"{name}: the shown step and its Markdown body"
            assert (':disabled[Bool(true)]' in next(line for line in spec.splitlines() if 'Button "walkthrough-previous"' in line)) == (step == 0), "Previous stops at the first step"
            assert (':disabled[Bool(true)]' in next(line for line in spec.splitlines() if 'Button "walkthrough-next"' in line)) == (step == 2), "Next stops at the last step"
            assert (':id[Str("walkthrough-excerpt-' in spec) == (step != 0), f"{name}: only a step with a view shows code"
            if step == 1:
                assert 'new_value = 2' in code(spec) and 'old_value = 1' in code(spec) and '"Line 1"' in spec, "A diff step shows the replaced lines and names its range"
                assert re.search(r'text_color\[Str\("[^"]+"\)\]\n\s*text "2"', spec), "A step's code has syntax colors"
                assert "icons/plus.svg" not in spec, "Frozen rows take no new comments"
                assert 'step-synthetic-step-reply' in spec and 'Button "step-edit-synthetic-step-reply"' in spec, "Your reply can be edited"
                assert 'step-edit-synthetic-step-body-1' not in spec and 'step-delete-synthetic-step-body-1' not in spec, "The agent's step cannot be changed here"
                assert 'Button "open-send"' in spec, "An unsent reply offers Send"
            if step == 2:
                assert '"New version · lines 10–12"' in spec and '"⋯ line 12"' in spec, "A source step names its version and marks gaps"
                assert '"Code changed since"' in spec and "lines as they were" in spec, "A stale step says the code changed"
            if step == 0:
                assert "No replies yet" in spec, "An empty discussion says how to start one"
            assert ('"Reply · step 2"' in spec and '"Reply in thread"' in spec) == (name == "reply"), "A step reply uses Review's composer"
            assert ('Button "step-reply-synthetic-step-thread-1"' in spec) == (name not in ("reply", "stale", "intro")), "Reply hides while its composer is open"
            assert ('Button "walkthrough-confirm-delete"' in spec) == (name == "delete"), "Deleting a walkthrough asks first"
        print("Review full-height, stacked, pending-switch, hover/range, reply-request, discussions and walkthrough contracts passed (description only; not paint)")


if __name__ == "__main__":
    main()

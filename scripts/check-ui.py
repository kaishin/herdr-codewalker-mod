#!/usr/bin/env python3
"""Check native review descriptions, including the full-height content contract.

This does not lay out or paint. A populated live Review screenshot is still required.
"""
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = '''import Codewalker from "./view.js";
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
    this.pendingFile = PENDING;
    EXTRA
  }
}
'''


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


def main():
    # Source is the reviewed authored module; no downloaded helpers or live app configuration.
    with tempfile.TemporaryDirectory(prefix="codewalker-native-fixture-") as temp:
        destination = Path(temp).resolve() / "mod"
        destination.mkdir()
        for name in ("main.js", "client.js", "model.js", "dock-expand.js", "pr-model.js", "pr-client.js", "pull-requests.js", "mod.json", "review.svg"):
            shutil.copy2(ROOT / "mods/Codewalker" / name, destination / ("view.js" if name == "main.js" else name))
        shutil.copytree(ROOT / "mods/Codewalker/icons", destination / "icons")
        # Settled; a file switch in progress (the shown diff stays, dimmed); a
        # hovered row while a new two-line comment is open; docked at the
        # panel's default width with a thread awaiting a short reply.
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
        branch = 'this.width = 368; this.height = 900; this.source = "branch"; this.base = "main"; this.baseInput.set_value("main");'
        commit = 'this.width = 368; this.height = 900; this.source = "commit"; this.commitOid = "0123456789abcdef"; this.commitParents = 2; this.commitInput.set_value("HEAD~1");'
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
        for pending, extra in (("null", pr), ("null", pr_description), ("null", pr_description_wide), ("null", no_pr), ("null", detached), ("null", pr_loading), ("null", pr_error), ("null", expandable), ("null", expanded), ("null", cached), ("null", ""), ('"src/other.py"', ""), ("null", hover), ("null", docked), ("null", listing), ("null", history), ("null", clearing), ("null", uncertain), ("null", editing), ("null", deleting), ("null", sending), ("null", viewed), ("null", hidden), ("null", branch), ("null", commit), ("null", remote), ("null", unfolded), ("null", inside), ("null", crowded)):
            (destination / "main.js").write_text(FIXTURE.replace("PENDING", pending).replace("EXTRA", extra))
            result = subprocess.run(["herdr-app", "check", str(destination), "--print-spec"],
                                    check=True, capture_output=True, text=True)
            spec = result.stdout
            narrow = extra in (pr, expandable, docked, viewed, hidden, branch, commit, cached, remote, unfolded, inside, crowded)
            if extra in (no_pr, detached, pr_loading, pr_error):
                assert 'Button "scope-pr"' in spec, "PR is the fourth comparison tab"
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
                assert "WARN" not in result.stderr, result.stderr
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
                    assert "WARN" not in result.stderr, result.stderr
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
            assert ('Button "expand-review"' in spec) == (extra in (expandable, expanded)), "Expand is offered only for supported docked views"
            if extra in (expandable, expanded):
                check_siblings(spec, 'Button "refresh"', 'Button "toggle-discussions"', 'Button "choose-checkout"', 'Button "open-send"', 'Button "expand-review"')
                assert f"icons/{'minimize-2' if extra == expanded else 'maximize-2'}.svg" in spec
                assert ("Restore panel width" if extra == expanded else "Expand to fill the workspace") in spec
            assert ("synthetic-machine · 1 file" in spec) == (extra == remote), "A remote review names its machine beside the counts"
            assert 'text "HEAD → working tree' not in spec, "Comparison explanation belongs in a tooltip, not a text row"
            assert ('Button "apply-base"' in spec) == (extra == branch), "Branch comparison keeps an explicit base editor"
            assert 'Button "scope-commit"' in spec, "Commit is the third scope"
            assert ('Button "apply-commit"' in spec) == (extra == commit), "Commit mode keeps a SHA/ref input and Show"
            if extra == commit:
                assert '"01234567"' in spec and 'first parent' in spec, "Commit mode identifies the resolved SHA and comparison"
            if extra == cached:
                check_content_height(spec, stacked=True)
                assert 'new_value = 2' in spec and 'Button "file-src/example.py"' in spec, "A restored mode shows its files and diff during revalidation"
                assert 'Reading changes' not in spec and 'Loading diff' not in spec and '.opacity' not in spec, "Cached mode switches must not blank or dim the diff"
                checkbox = next(line for line in lines if 'Checkbox "file-viewed"' in line)
                assert ':disabled[Bool(true)]' in checkbox, "Cached Viewed state is read-only until revalidated"
                resolve = next(line for line in lines if 'Button "status-synthetic-thread"' in line)
                assert ':disabled[Bool(true)]' in resolve, "Cached discussions cannot be mutated before revalidation"
                assert "WARN" not in result.stderr, result.stderr
                print(result.stderr.strip())
                continue
            if extra == sending:
                # The exact text to paste, agent choice, and a Send that waits for a ready agent.
                assert ':id[Str("review-send")]' in spec and 'Button "send-to-agent"' in spec, "Send view shows its preview and Send"
                assert "thread synthetic-thread" in spec and "Synthetic fixture: why change this value?" in spec, "The preview is the exact message"
                assert "WARN" not in result.stderr, result.stderr
                print(result.stderr.strip())
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
                assert "WARN" not in result.stderr, result.stderr
                print(result.stderr.strip())
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
            dimmed = ".opacity" in spec and 'new_value = 2' in spec
            assert dimmed == (pending != "null"), "A pending file switch must keep the shown diff, dimmed"
            assert ("icons/plus.svg" in spec) == (extra == hover), "Only a hovered, anchorable row shows the comment button"
            assert ('"Comment on lines 1–2"' in spec) == (extra == hover), "A range comment names its lines"
            assert '"Ask for a short reply"' in spec, "Every open composer offers a short-reply request"
            assert ("edit-synthetic-human" in spec) == (extra not in (editing, deleting)) and "edit-synthetic-agent" not in spec, "Only your own messages offer Edit"
            assert ('"Awaiting reply"' in spec) == (extra in (docked, inside, crowded)), "A thread owed a reply says so"
            # Resolve carries a check mark; a checked reply request adds one more.
            assert spec.count("icons/check.svg") == 1 + (extra == docked) + (2 if extra == viewed else 1 if extra == hidden else 0), "Resolve and a checked request show a check mark"
            if not extra:
                assert all(f"icons/{name}.svg" in spec for name in ("reply", "pencil", "trash")), "Reply, Edit and Delete show their icons"
            assert "WARN" not in result.stderr, result.stderr
            print(result.stderr.strip())
        print("Review full-height, stacked, pending-switch, hover/range, reply-request and discussions contracts passed (description only; not paint)")


if __name__ == "__main__":
    main()

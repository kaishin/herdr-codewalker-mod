// Synthetic demo data, rendered by the real Codewalker view. No repository,
// database, terminal-send, or network operations are available in this fixture.
import Codewalker from "./view.js";
import { layoutLanes } from "./lanes.js";

export default class Preview extends Codewalker {
  refresh() {}
  read() {}
  follow() {}
  watch() {}
  close() {}
  persistDraft() {}
  submit() {}
  discard() {}
  saveEdit() {}
  deleteMessage() {}
  clearHistory() {}
  setStatus() {}
  setViewed() {}
  send() {}
  markArrived() {}
  changeRoot() {}
  applySource() {}
  readWalkthrough() {}
  setWalkthroughStatus() {}
  deleteWalkthrough() {}
  candidates() { return []; }

  init(p, cx) {
    super.init(p, cx);
    this.fixed = this.framed = true;
    this.on = null;
    this.width = 900;
    this.height = 620;
    this.folder = this.root = "/demo/taskboard";
    this.client = { saveDraft: async () => ({}), deleteDraft: async () => ({}) };
    this.history.refresh = () => {};
    this.reviewId = "demo-review";
    this.snapshotId = this.rowsSnapshot = "demo-snapshot";
    this.revision = 3;
    this.branch = "fix/retry-safe-jobs";
    this.source = "commit";
    this.commitRef = this.commitOid = "8e4c2a1f0123456789abcdef0123456789abcdef01";
    this.commitParents = 1;
    this.resolvedBase = "b".repeat(40);
    this.commitInput.set_value("8e4c2a1f");
    this.file = "src/jobs/queue.ts";
    this.rowsFingerprint = "demo-diff";
    this.files = [
      { path: this.file, status: "M", additions: 3, deletions: 1, diffFingerprint: "demo-diff", viewed: false },
      { path: "src/jobs/schema.ts", status: "M", additions: 2, deletions: 1, diffFingerprint: "demo-schema", viewed: true },
      { path: "tests/jobs.test.ts", status: "M", additions: 18, deletions: 0, diffFingerprint: "demo-tests", viewed: false },
    ];
    this.rows = [
      { kind: "@", text: "@@ -12,4 +12,6 @@", hunkId: "demo-hunk" },
      { kind: " ", oldLine: 12, newLine: 12, text: "export async function enqueue(job: Job) {" },
      { kind: "-", oldLine: 13, newLine: null, text: "  await db.insert(jobs).values(job);" },
      { kind: "+", oldLine: null, newLine: 13, text: "  await db.insert(jobs)" },
      { kind: "+", oldLine: null, newLine: 14, text: "    .values(job)" },
      { kind: "+", oldLine: null, newLine: 15, text: "    .onConflictDoNothing({ target: jobs.id });" },
      { kind: " ", oldLine: 14, newLine: 16, text: "  return job.id;" },
      { kind: " ", oldLine: 15, newLine: 17, text: "}" },
    ];
    this.threads = [{
      id: "demo-thread", path: this.file, status: "open", outdated: false,
      anchor: { path: this.file, side: "new", startLine: 15, endLine: 15, commitOid: this.commitOid },
      messages: [
        { id: "demo-human", actor: { kind: "human", id: "local", name: "You" },
          body: "Please make retries safe. Two workers can receive the same job.",
          createdAt: "2026-01-01T10:00:00Z", delivery: { state: "sent", target: "demo-agent" } },
        { id: "demo-agent", actor: { kind: "agent", id: "demo", name: "Agent" },
          body: "Done. The unique job ID now prevents duplicate inserts.",
          createdAt: "2026-01-01T10:01:00Z" },
      ],
    }];
    this.intent = null;
    this.drafts = [];
    this.draftsLoaded = true;
    this.error = this.note = null;
    this.loading = this.reading = false;
    const commits = [
      {hash:this.commitOid, parents:["b".repeat(40), "c".repeat(40)], subject:"Make job retries safe", refs:[`HEAD -> refs/heads/${this.branch}`]},
      {hash:"c".repeat(40), parents:["d".repeat(40)], subject:"Check concurrent retries", refs:["refs/heads/test/retries"]},
      {hash:"b".repeat(40), parents:["d".repeat(40)], subject:"Keep one ID per job", refs:[]},
      {hash:"d".repeat(40), parents:[], subject:"Create job queue", refs:["refs/heads/main", "refs/remotes/origin/main"]},
    ].map((commit, i) => ({...commit, author:"Example", time:1795212000 - i * 3600}));
    this.history.data = {defaultBase:"refs/heads/main", defaultBaseKind:"fallback", root:this.root, branch:this.branch, head:this.commitOid, commits, branches:[
      {ref:`refs/heads/${this.branch}`, name:this.branch, hash:this.commitOid, remote:false},
      {ref:"refs/heads/main", name:"main", hash:"d".repeat(40), remote:false},
      {ref:"refs/remotes/origin/main", name:"origin/main", hash:"d".repeat(40), remote:true},
    ]};
    this.history.graph = layoutLanes(commits);
    this.base = "main";
    this.baseInput.set_value("main");
    const scene = "commit"; // scripts/preview.py selects the synthetic scene.
    if (scene === "history-branch") this.source = "branch";
    if (scene === "history-branches") this.history.picking = true;
    if (scene === "history-edit-base") { this.source = "branch"; this.history.entry = "base"; }
    if (scene === "history-go-commit") this.history.entry = "commit";
    if (scene === "walkthrough") {
      // An agent's tour of the same change: one intro, one diff step, one unchanged caller.
      const agent = { kind: "agent", id: "demo", name: "Agent" }, you = { kind: "human", id: "local", name: "You" };
      const id = "demo-walkthrough", titles = ["Why retries were unsafe", "Insert once per job ID", "Where retries come from"];
      const thread = (n, messages, extra = {}) => ({ id: `demo-step-thread-${n}`, path: [null, this.file, "src/jobs/worker.ts"][n], status: "open", awaitingReply: false, outdated: false,
        anchor: { walkthroughId: id, stepId: `demo-step-${n}`, validity: "current" },
        walkthrough: { id, title: "Safe job retries", stepId: `demo-step-${n}`, position: n, stepTitle: titles[n], stepCount: 3 }, messages, ...extra });
      const steps = [
        { view: null, body: "Two workers can receive the same job after a timeout. Both insert it, so the job runs **twice**.\n\nThis walkthrough shows the one-line fix and the caller that retries." },
        { view: { kind: "diff", path: this.file, oldPath: null, status: "modified", side: "new", startLine: 13, endLine: 15, hunks: [{ rows: [
            { kind: "delete", oldLine: 13, newLine: null, text: "  await db.insert(jobs).values(job);" },
            { kind: "add", oldLine: null, newLine: 13, text: "  await db.insert(jobs)" },
            { kind: "add", oldLine: null, newLine: 14, text: "    .values(job)" },
            { kind: "add", oldLine: null, newLine: 15, text: "    .onConflictDoNothing({ target: jobs.id });" }] }] },
          body: "The insert now ignores a second row with the same `jobs.id`. The ID comes from the producer, so a retry reuses it.\n\n- No lock is needed.\n- The first insert wins." },
        { view: { kind: "source", path: "src/jobs/worker.ts", rev: "head", startLine: 41, endLine: 44, hunks: [{ rows: [
            { kind: "context", oldLine: null, newLine: 41, text: "} catch (error) {" },
            { kind: "context", oldLine: null, newLine: 42, text: "  if (isTimeout(error)) return enqueue(job);" },
            { kind: "context", oldLine: null, newLine: 43, text: "  throw error;" },
            { kind: "context", oldLine: null, newLine: 44, text: "}" }] }] },
          body: "The worker retries with the **same** job object. This file did not change; it is here so you can see where the duplicate came from." },
      ].map((step, n) => ({ id: `demo-step-${n}`, position: n, title: titles[n], view: step.view, validity: "current",
        thread: thread(n, [{ id: `demo-step-body-${n}`, actor: agent, body: step.body, createdAt: "2026-01-01T10:00:00Z" },
          ...(n === 1 ? [{ id: "demo-step-question", actor: you, body: "What if the producer sends no ID?", createdAt: "2026-01-01T10:04:00Z", delivery: { state: "sent", target: "demo-agent" } },
            { id: "demo-step-answer", actor: agent, body: "Then the schema rejects the job: `jobs.id` is required since the previous commit.", createdAt: "2026-01-01T10:05:00Z" }] : [])]) }));
      this.walkthroughId = id;
      this.walkthrough = { id, title: "Safe job retries", summary: "How the queue stops running a retried job twice.", status: "open", author: agent, createdAt: "2026-01-01T10:00:00Z", steps };
      this.step = 1;
      this.threads = steps.map(step => step.thread);
    }
    if (["pr", "pr-description", "pr-long-description", "no-pr"].includes(scene)) {
      this.source = "pr";
      this.prTab = scene === "pr" ? "changes" : "description";
      this.prBranch = this.branch;
      this.prHead = this.commitOid;
      this.prBase = "7b3d1a0f0123456789abcdef0123456789abcdef01";
      if (scene === "no-pr") {
        this.pullRequests.previous = {branch: this.branch, pr: null};
        this.files = this.rows = this.threads = [];
        this.prHead = null;
      }
      this.pullRequest = scene === "no-pr" ? null : {
        number: 42, title: "Make job retries safe", url: "https://github.com/example/taskboard/pull/42",
        author: {login: "example"}, headRefName: this.branch, baseRefName: "main", state: "OPEN",
        body: "Keep one job per unique ID, even when two workers retry at the same time.",
        reviewDecision: "APPROVED", checks: {kind: "passed", label: "2 passed"},
        checksList: [{name: "Unit tests", state: "SUCCESS"}, {name: "Type check", state: "SUCCESS"}],
      };
      if (scene === "pr-long-description") this.pullRequest.body += "\n\n## Review notes\n\n" + Array.from({length: 24}, (_, i) => `### Case ${i + 1}\n\nA retried job keeps its original ID. Workers must not insert it twice. Check the unique-key path and the failure case.`).join("\n\n");
    }
  }
}

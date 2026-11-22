import { parseLog, parseStatus, parseBranches, sameHead } from "./history-model.js";

const READ = ["--no-optional-locks", "-c", "color.ui=false"];
export const HISTORY_LIMIT = 1500;

// Git's history reader without checkout, fetch, sync or other mutation methods.
// Every command runs on the review's machine, including remote checkouts.
export class HistoryClient {
  constructor(execute, where) { this.execute = execute; this.where = { ...where }; }

  async git(args) {
    let result;
    try { result = await this.execute("git", [...READ, ...args], this.where); }
    catch (error) { throw new Error(`Git history could not finish: ${error.message || error}`); }
    if (result.code !== 0) throw new Error((result.stderr || result.stdout || `Git exited ${result.code}`).trim().slice(0, 1800));
    return result.stdout;
  }

  async status() { return parseStatus(await this.git(["status", "--porcelain=v2", "--branch", "-z", "--untracked-files=no"])); }

  async snapshot(limit = 150) {
    limit = Math.min(HISTORY_LIMIT, Math.max(150, limit));
    const root = (await this.git(["rev-parse", "--show-toplevel"])).trim();
    const before = await this.status();
    const [log, branches] = await Promise.all([
      before.head ? this.git(["log", "--topo-order", "--decorate=full", `--max-count=${limit + 1}`, "--format=%H%x1f%P%x1f%an%x1f%at%x1f%D%x1f%s%x00", "--branches", "--remotes", "--tags", "HEAD", "--"]) : "",
      this.git(["for-each-ref", "--sort=-committerdate", "--format=%(refname)%00%(objectname)%00%(upstream:short)%00%(worktreepath)%00%(symref)", "refs/heads", "refs/remotes"]),
    ]);
    const after = await this.status();
    if (!sameHead(before, after)) throw new Error("The checkout changed while loading history. Refresh to read it again.");
    const commits = parseLog(log), parsed = parseBranches(branches);
    // Read the last-fetched default, without asking a server or changing refs.
    const heads = branches.split("\n").map(line => line.split("\0")).filter(([ref, , , , symref]) => ref?.startsWith("refs/remotes/") && ref.endsWith("/HEAD") && symref);
    const remote = heads.find(([ref]) => ref === "refs/remotes/origin/HEAD") || (heads.length === 1 ? heads[0] : null);
    const fallback = ["refs/heads/main", "refs/heads/master"].find(ref => parsed.some(branch => branch.ref === ref));
    const defaultBase = remote?.[4] || fallback || null;
    return { ...after, root, commits: commits.slice(0, limit), more: commits.length > limit, branches: parsed,
      defaultBase, defaultBaseKind: remote ? "remote" : fallback ? "fallback" : null };
  }
}

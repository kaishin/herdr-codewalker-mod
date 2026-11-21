import { PR_FIELDS, slimPR, imagesAsText, prLocation, prProblem } from "./pr-model.js";

// Read-only GitHub access, on the checkout's machine. Never checks out or fetches.
export class PullRequestClient {
  constructor(execute, where) { this.execute = execute; this.where = { ...where }; }

  async command(program, args) {
    let result;
    try { result = await this.execute(program, args, this.where); }
    catch (error) { throw new Error(`${program} could not finish: ${error.message || error}`); }
    if (result.code !== 0) throw new Error((result.stderr || result.stdout || `${program} exited ${result.code}`).trim().slice(0, 1800));
    return result.stdout;
  }

  async branch() {
    // Detached HEAD has no current branch and therefore no current PR.
    const result = await this.execute("git", ["--no-optional-locks", "symbolic-ref", "--quiet", "--short", "HEAD"], this.where);
    if (result.code === 1) return null;
    if (result.code !== 0) throw new Error((result.stderr || "Cannot read the current branch.").trim());
    return result.stdout.trim();
  }

  async current(previous, refresh = false) {
    const branch = await this.branch();
    if (!branch) return { branch, pr: null };
    // Check the branch on watch/timer events; network reads only on entry,
    // explicit Refresh, or a branch change. A missing PR is cached as well.
    if (!refresh && previous?.branch === branch) return previous;
    let remotes;
    try {
      remotes = (await this.command("git", ["--no-optional-locks", "remote"])).trim().split("\n").filter(Boolean);
      let pr = null;
      if (remotes.length) {
        try {
          // No argument: gh uses the current branch and its tracking remote.
          pr = JSON.parse(await this.command("gh", ["pr", "view", "--json", `${PR_FIELDS},state,body,mergeable,mergeStateStatus,additions,deletions,changedFiles`]));
        } catch (error) {
          if (!/no pull requests? (?:found|associated)|no pull request for branch/i.test(error.message)) throw error;
        }
      }
      if (await this.branch() !== branch) throw new Error("The branch changed while reading its pull request. Refresh to try again.");
      if (!pr || pr.state !== "OPEN") return { branch, pr: null };
      if (!prLocation(pr.url) || ![pr.baseRefOid, pr.headRefOid].every(oid => /^(?:[a-f0-9]{40}|[a-f0-9]{64})$/.test(oid || ""))) throw new Error("GitHub did not return a PR address and full base/head commit IDs.");
      const body = imagesAsText(pr.body);
      return { branch, pr: { ...slimPR(pr), body: body.slice(0, 12000), bodyTruncated: body.length > 12000,
        checksList: (pr.statusCheckRollup || []).map(c => ({ name: c.name || c.context || "Check", state: c.conclusion || c.state || c.status || "UNKNOWN", url: c.detailsUrl || c.targetUrl })) } };
    } catch (error) {
      error.problem = remotes ? prProblem(error.message, remotes, this.where.on) : { title: "Could not read this checkout", detail: error.message };
      throw error;
    }
  }
}

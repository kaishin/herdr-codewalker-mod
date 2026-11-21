// Read-only GitHub models adapted from herdr-git-mod (MIT, Reda Lemeden).
export const PR_FIELDS = "number,title,url,author,headRefName,headRefOid,baseRefName,baseRefOid,isDraft,isCrossRepository,reviewDecision,updatedAt,statusCheckRollup";
export function checksSummary(checks = []) {
  const counts = { passed: 0, failed: 0, pending: 0, skipped: 0, unknown: 0 };
  for (const c of checks || []) {
    const state = String(c.conclusion || c.state || c.status || "").toUpperCase();
    if (state === "SUCCESS") counts.passed++;
    else if (["FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE", "STALE"].includes(state)) counts.failed++;
    else if (["NEUTRAL", "SKIPPED"].includes(state)) counts.skipped++;
    else if (["PENDING", "EXPECTED", "QUEUED", "IN_PROGRESS", "WAITING", "REQUESTED"].includes(state)) counts.pending++;
    else counts.unknown++;
  }
  const total = Object.values(counts).reduce((a, b) => a + b, 0);
  const kind = !total ? "none" : counts.failed ? "failed" : counts.pending ? "pending" : counts.unknown ? "unknown" : counts.passed ? "passed" : "skipped";
  const label = kind === "none" ? "No checks" : kind === "failed" ? `${counts.failed} failed` : kind === "pending" ? `${counts.pending} running` : kind === "unknown" ? "Checks unknown" : kind === "skipped" ? "Checks skipped" : `${counts.passed} passed`;
  return { ...counts, total, kind, label };
}

export function slimPR(pr) {
  const { statusCheckRollup, ...rest } = pr;
  return { ...rest, checks: checksSummary(statusCheckRollup) };
}
export function reviewLabel(pr) {
  return pr.isDraft ? "Draft" : ({ APPROVED: "Approved", CHANGES_REQUESTED: "Changes requested", REVIEW_REQUIRED: "Review required" }[pr.reviewDecision] || "Not reviewed");
}

export function prProblem(message, remotes, machine) {
  const text = String(message || "").trim(), where = machine ? `on ${machine}` : "on this computer";
  if (!remotes?.length || /no git remotes found/i.test(text)) return { kind: "no-remote", title: "This checkout has no remote", detail: "Pull requests come from GitHub. Add a GitHub remote, then refresh.", command: "git remote add origin <GitHub URL>", copy: "git remote add origin " };
  if (/none of the git remotes|known GitHub host/i.test(text)) return { kind: "not-github", title: "No GitHub remote", detail: `Remotes: ${remotes.join(", ")}. None points to GitHub, so there are no pull requests to show.` };
  if (/set-default|determine base repo|multiple remotes/i.test(text)) return { kind: "no-default", title: "Choose the repository for pull requests", detail: "This checkout has more than one GitHub remote. Set a default, then retry.", command: "gh repo set-default", retry: true };
  if (/gh auth login|not logged in|authentication required|HTTP 401|bad credentials/i.test(text)) return { kind: "auth", title: "Sign in to GitHub CLI", detail: `GitHub CLI has no session ${where}. Sign in, then retry.`, command: "gh auth login", retry: true };
  if (/^gh could not finish:.*(not found|ENOENT|no such file|cannot find)/i.test(text)) return { kind: "no-gh", title: "GitHub CLI is not installed", detail: `Pull requests are read with gh. Install it ${where}, sign in with gh auth login, then retry.`, retry: true };
  return { kind: "other", title: "Couldn’t load pull requests", detail: "GitHub CLI returned this error:", raw: text || "No details.", retry: true };
}


// Validate GitHub-provided PR addresses before opening or building fetch commands.
export function prLocation(url) {
  const m = /^https:\/\/([A-Za-z0-9.-]+)\/([A-Za-z0-9_.-]+)\/([A-Za-z0-9_.-]+)\/pull\/([1-9]\d*)(?:[/?#]|$)/.exec(String(url || ""));
  return m && Number.isSafeInteger(Number(m[4])) ? { host: m[1].toLowerCase(), owner: m[2], name: m[3], number: Number(m[4]) } : null;
}
const attr = (tag, name) => (new RegExp(`\\s${name}\\s*=\\s*(?:"([^"]*)"|'([^']*)')`, "i").exec(tag) || []).slice(1).find(v => v !== undefined) || "";
const linkText = alt => (String(alt).replace(/\s+/g, " ").trim() || "image").replace(/[[\]\\]/g, "\\$&");
const linkTo = (alt, url) => /^https?:\/\//i.test(url) ? `[${linkText(alt)}](<${url.replace(/[<>\s]/g, encodeURIComponent)}>)` : linkText(alt);
export function imagesAsText(markdown) {
  return String(markdown || "").split(/(^ {0,3}(?:```|~~~)[^\n]*\n[\s\S]*?(?:^ {0,3}(?:```|~~~)[ \t]*$|(?![\s\S])))/m).map((part, i) => i % 2 ? part : part
    .replace(/<a\s[^>]*>\s*(<img\b[^>]*>)\s*<\/a>/gi, (link, img) => linkTo(attr(img, "alt"), attr(link, "href") || attr(img, "src")))
    .replace(/<img\b[^>]*>/gi, img => linkTo(attr(img, "alt"), attr(img, "src")))
    .replace(/\[!\[([^\]]*)\]\(\s*<?[^)\s>]*>?(?:\s+"[^"]*")?\s*\)\]\(\s*<?([^)\s>]*)>?(?:\s+"[^"]*")?\s*\)/g, (_m, alt, href) => linkTo(alt, href))
    .replace(/!\[([^\]]*)\]\(\s*<?([^)\s>]*)>?(?:\s+"[^"]*")?\s*\)/g, (_m, alt, src) => linkTo(alt, src))).join("");
}

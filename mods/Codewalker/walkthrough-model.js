// Pure walkthrough presentation helpers. The Python helper owns the data and
// copies each step's rows when the agent writes it; nothing here reads Git.
const KIND = { add: "+", delete: "-", context: " ", meta: "" };

// A step's frozen hunks as Review diff rows. Hunks that are not adjacent get a
// separator row, so a gap in the code is never shown as continuous lines. Built
// once per view, so the rows keep their syntax colors across renders.
const built = new WeakMap();
export function excerptRows(view) {
  if (view && built.has(view)) return built.get(view);
  const rows = [];
  (view?.hunks ?? []).forEach((hunk, index) => {
    const first = hunk.rows.find(row => row.oldLine || row.newLine);
    if (index > 0) rows.push({ kind: "@", text: first ? `⋯ ${first.newLine ? `line ${first.newLine}` : `old line ${first.oldLine}`}` : "⋯" });
    for (const row of hunk.rows) rows.push({ ...row, kind: KIND[row.kind] ?? row.kind });
  });
  if (view) built.set(view, rows);
  return rows;
}

// What a step shows, in words: the file and which lines of which version.
export function viewLabel(view) {
  if (!view) return null;
  const range = view.startLine === view.endLine ? `line ${view.startLine}` : `lines ${view.startLine}–${view.endLine}`;
  if (view.kind === "source") return { path: view.path, detail: `${view.rev === "base" ? "Old version" : "New version"} · ${range}` };
  if (!Number.isInteger(view.startLine)) return { path: view.path, detail: "Whole diff" };
  return { path: view.path, detail: view.side === "old" ? `Removed ${range}` : range[0].toUpperCase() + range.slice(1) };
}

export function stepIndex(walkthrough, stepId, fallback = 0) {
  const steps = walkthrough?.steps ?? [];
  const found = steps.findIndex(step => step.id === stepId);
  return found >= 0 ? found : Math.max(0, Math.min(fallback, steps.length - 1));
}

// Replies after the step body, and whether the person or the agent owes an answer.
export function stepActivity(thread) {
  const replies = Math.max(0, (thread?.messages?.length ?? 1) - 1);
  return { replies, awaiting: thread?.awaitingReply === true, resolved: thread?.status === "resolved" };
}

// Open walkthroughs first, newest first; archived ones after.
export function sortWalkthroughs(list) {
  return [...(list ?? [])].sort((a, b) => (a.status === "archived") - (b.status === "archived")
    || String(b.updatedAt ?? "").localeCompare(String(a.updatedAt ?? "")));
}

// Read-only history parsing and labels, adapted from herdr-git-mod.
export function parseLog(text) {
  // NUL separates records; a subject can contain tabs or our unit separator.
  return text.split("\0").map(s => s.replace(/^\n/, "")).filter(Boolean).map(line => {
    const [hash, parents, author, time, refs, ...subject] = line.split("\x1f");
    return { hash, parents: parents ? parents.split(" ") : [], author, time: Number(time), refs: (refs || "").split(", ").filter(Boolean), subject: subject.join("\x1f") };
  }).filter(c => /^[a-f0-9]{40,64}$/.test(c.hash));
}

export function parseStatus(text) {
  const out = { head: null, branch: null, upstream: null, ahead: 0, behind: 0, dirty: 0, conflicts: 0 };
  const fields = text.split("\0");
  for (let i = 0; i < fields.length; i++) {
    const line = fields[i];
    if (line.startsWith("# branch.oid ")) out.head = line.slice(13) === "(initial)" ? null : line.slice(13);
    else if (line.startsWith("# branch.head ")) out.branch = line.slice(14) === "(detached)" ? null : line.slice(14);
    else if (line.startsWith("# branch.upstream ")) out.upstream = line.slice(18);
    else if (line.startsWith("# branch.ab ")) {
      const m = /\+(\d+) -(\d+)/.exec(line); if (m) { out.ahead = +m[1]; out.behind = +m[2]; }
    } else if (/^[12u?] /.test(line)) { out.dirty++; if (line[0] === "u") out.conflicts++; if (line[0] === "2") i++; }
  }
  return out;
}

export function parseBranches(text) {
  return text.trim().split("\n").filter(Boolean).map(line => {
    const [ref, hash, upstream, worktree, symref] = line.split("\0");
    const remote = ref.startsWith("refs/remotes/");
    return { ref, hash, upstream, worktree, symref, remote, name: ref.replace(/^refs\/(heads|remotes)\//, "") };
  }).filter(b => !b.symref);
}

export function ago(time) {
  const seconds = Math.max(0, Date.now() / 1000 - (typeof time === "number" ? time : Date.parse(time) / 1000));
  if (!Number.isFinite(seconds)) return "";
  return seconds < 60 ? "now" : seconds < 3600 ? `${Math.floor(seconds / 60)}m` : seconds < 86400 ? `${Math.floor(seconds / 3600)}h` : `${Math.floor(seconds / 86400)}d`;
}

export function refLabel(ref) { return ref.replace(/^HEAD -> /, "").replace(/refs\/(heads|remotes|tags)\//g, ""); }

export function sameHead(a, b) { return a.head === b.head && a.branch === b.branch && a.upstream === b.upstream; }

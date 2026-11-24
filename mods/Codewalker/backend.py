#!/usr/bin/env python3
"""Read-only Git review and transactional, local human/agent annotations."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import uuid

VERSION = 1
# Helper build, reported by `hello`. A Review on another machine checks it
# before it uses this copy. Raise it when the UI needs newer helper behavior.
HELPER_VERSION = 3
# Optional protocol features a caller may use only when `hello` lists them.
# "data-parts": repeated --data entries are joined into one mutation JSON.
# "walkthroughs": agent-written, ordered code walkthroughs with step threads.
CAPABILITIES = ("data-parts", "history-clear", "pr-comparison", "walkthroughs")
# Store schema; 2 adds messages.reply_requested, 3 adds messages.edited_at and
# message_edits (earlier bodies), 4 adds deliveries (what was sent to an agent).
# 5 adds file_views (explicit, per-comparison viewed-file marks).
# 6 adds walkthroughs and walkthrough_steps (each step owns one thread).
# Older stores migrate in place.
STORE_VERSION = 6
DELIVERY_STATES = ("uncertain", "sent")
MAX_FILE_BYTES = 2_000_000
MAX_LINES = 50_000
MAX_FILES = 1_000
MAX_TOTAL_BYTES = 32_000_000
MAX_OUTPUT = 7_000_000
MAX_BODY_BYTES = 65_536
MAX_TITLE_BYTES = 512
MAX_STEPS = 100
MAX_STEP_ROWS = 400
WALKTHROUGH_STATES = ("open", "archived")


class Failure(Exception):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code, self.message, self.details = code, message, details


def fail(code, message, **details):
    raise Failure(code, message, **details)


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    raw = value if isinstance(value, bytes) else canonical_json(value).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds")


def identifier(prefix):
    return prefix + "_" + uuid.uuid4().hex


def source_lines(raw):
    """Git counts LF-delimited lines, not Unicode separators or lone CRs."""
    if not raw:
        return []
    lines = raw.decode("utf-8").split("\n")
    return lines[:-1] if lines[-1] == "" else lines


def text(value, field, maximum=256, blank=False):
    if not isinstance(value, str) or (not blank and not value.strip()):
        fail("invalid_request", f"{field} must be a nonempty string")
    if len(value.encode("utf-8")) > maximum or "\x00" in value:
        fail("invalid_request", f"{field} is too long or contains NUL")
    return value


def positive(value, field, minimum=1):
    if type(value) is not int or value < minimum:
        fail("invalid_request", f"{field} must be an integer >= {minimum}")
    return value


def relative_path(value):
    value = text(value, "path", 4096)
    parts = PurePosixPath(value).parts
    if (value.startswith("/") or "\\" in value or re.match(r"^[A-Za-z]:", value)
            or any(p in ("..", ".git") for p in parts)
            or not parts or str(PurePosixPath(value)) != value or value == "."):
        fail("invalid_request", "path must be a normalized relative worktree file", path=value)
    return value


def actor_data(value):
    if not isinstance(value, dict) or set(value) != {"kind", "id", "name"}:
        fail("invalid_request", "actor requires kind, id and name")
    if value["kind"] not in ("human", "agent"):
        fail("invalid_request", "actor.kind must be human or agent")
    return {"kind": value["kind"], "id": text(value["id"], "actor.id", 128),
            "name": text(value["name"], "actor.name", 128)}


class Git:
    PREFIX = ["git", "--no-optional-locks", "-c", "core.fsmonitor=false",
              "-c", "diff.autoRefreshIndex=false", "-c", "core.hooksPath=/dev/null",
              "-c", "diff.suppressBlankEmpty=false", "-c", "diff.interHunkContext=0",
              "-c", "diff.outputIndicatorNew=+", "-c", "diff.outputIndicatorOld=-",
              "-c", "diff.outputIndicatorContext= "]
    DIFF = ["diff", "--no-ext-diff", "--no-textconv", "--no-color", "--no-relative",
            "--find-renames", "--ignore-submodules=dirty", "--submodule=short"]

    def __init__(self, folder):
        if not folder or not Path(folder).is_absolute():
            fail("invalid_request", "--repo must be an explicit absolute folder")
        path = Path(folder).resolve()
        if not path.is_dir():
            fail("not_repository", "repository folder does not exist")
        self.root = path
        self.reader = None
        self.prefix = list(self.PREFIX)
        out = self.run(["rev-parse", "--show-toplevel"], check=False)
        if out.returncode:
            fail("not_repository", "folder is not inside a Git worktree")
        self.root = Path(os.fsdecode(out.stdout).removesuffix("\n")).resolve()
        self.git_dir = os.fsdecode(self.run(["rev-parse", "--absolute-git-dir"]).stdout).removesuffix("\n")
        # Git diff can invoke clean/filter-process drivers even with no-textconv.
        # Disable every configured driver before any command reads worktree content.
        filters = self.run(["config", "--null", "--name-only", "--get-regexp",
                            r"^filter\..*\.(clean|process|required)$"], check=False)
        for key in filters.stdout.split(b"\0"):
            if key:
                driver = os.fsdecode(key).rsplit(".", 1)[0]
                for setting, value in (("clean", ""), ("process", ""), ("required", "false")):
                    self.prefix.extend(["-c", f"{driver}.{setting}={value}"])
        self.branch = self.line(["symbolic-ref", "--quiet", "--short", "HEAD"], optional=True)
        self.head = self.line(["rev-parse", "--verify", "--quiet", "HEAD^{commit}"], optional=True)
        self.review_id = "review_" + digest([str(self.root), self.git_dir,
                                            self.branch or "detached:" + str(self.head)])[:32]

    @staticmethod
    def environment():
        # A caller's GIT_DIR/INDEX_FILE/config-injection environment must not redirect the repo.
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update({"GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"})
        return env

    def run(self, args, check=True):
        env = self.environment()
        try:
            with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
                process = subprocess.run(self.prefix + args, cwd=self.root, env=env,
                                         stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, timeout=20)
                if stdout.tell() > MAX_OUTPUT:
                    fail("too_large", "Git output exceeds review limit")
                stdout.seek(0)
                stderr.seek(0)
                result = subprocess.CompletedProcess(process.args, process.returncode,
                                                     stdout.read(MAX_OUTPUT + 1), stderr.read(4096))
        except (OSError, subprocess.TimeoutExpired) as exc:
            fail("git_error", f"Git unavailable or timed out: {exc}")
        if len(result.stdout) > MAX_OUTPUT:
            fail("too_large", "Git output exceeds review limit")
        if check and result.returncode:
            fail("git_error", result.stderr.decode("utf-8", "replace").strip()[:1000] or "Git failed")
        return result

    def line(self, args, optional=False):
        result = self.run(args, check=not optional)
        if result.returncode:
            return None
        return result.stdout.decode("utf-8", "replace").strip() or None

    def endpoints(self, scope, base):
        if scope not in ("uncommitted", "branch"):
            fail("invalid_request", "scope must be uncommitted or branch")
        if scope == "uncommitted":
            if self.head:
                return "HEAD", self.head
            # Do not assume SHA-1; compute the empty tree identity without writing an object.
            empty = self.line(["hash-object", "-t", "tree", "--stdin"])
            return "HEAD", empty
        if not self.head:
            fail("invalid_base", "branch scope requires a HEAD commit")
        candidates = [base] if base else [self.line(["symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"], optional=True), "main", "master"]
        for candidate in candidates:
            if not candidate:
                continue
            text(candidate, "base", 1024)
            if candidate.startswith("-"):
                fail("invalid_base", "base must be a Git revision, not an option")
            oid = self.line(["rev-parse", "--verify", "--quiet", "--end-of-options", candidate + "^{commit}"], optional=True)
            if oid:
                merge_base = self.line(["merge-base", oid, self.head], optional=True)
                if merge_base:
                    return candidate, merge_base
        fail("invalid_base", "base does not resolve to a commit with a common ancestor", base=base)

    def commit_endpoints(self, commit):
        ref = text(commit, "commit", 1024)
        if ref.startswith("-"):
            fail("invalid_commit", "commit must be a Git revision, not an option")
        oid = self.line(["rev-parse", "--verify", "--quiet", "--end-of-options", ref + "^{commit}"], optional=True)
        if not oid:
            fail("invalid_commit", "commit does not resolve to a local commit", commit=ref)
        # Read the actual parent headers: rev-list hides parents at shallow
        # boundaries, which must not be mistaken for root commits.
        header = self.run(["cat-file", "-p", oid]).stdout.split(b"\n\n", 1)[0]
        parents = [line.split()[1].decode("ascii") for line in header.split(b"\n") if line.startswith(b"parent ")]
        if parents:
            parent = parents[0]
            if self.run(["cat-file", "-e", parent + "^{commit}"], check=False).returncode:
                fail("invalid_commit", "the commit's first parent is unavailable locally; fetch its history first", commit=oid)
        else:
            parent = self.line(["hash-object", "-t", "tree", "--stdin"])
        return oid, parent, len(parents)

    def pr_endpoints(self, base, head):
        # GitHub supplies full object IDs, never local branch names. A PR compares
        # its merge base to its head, regardless of the checked-out branch or edits.
        for name, oid in (("base", base), ("head", head)):
            if not isinstance(oid, str) or not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", oid):
                fail("invalid_request", f"PR {name} requires a full commit ID")
            if not self.line(["rev-parse", "--verify", "--quiet", oid + "^{commit}"], optional=True):
                fail("pr_objects_missing", f"PR {name} commit is not available locally. Fetch the PR history, then refresh. Codewalker never fetches or checks out automatically.", commit=oid)
        merge_base = self.line(["merge-base", base, head], optional=True)
        if not merge_base:
            fail("invalid_base", "PR commits have no local common ancestor. Fetch their history, then refresh.")
        return head, merge_base

    def changes(self, base_oid, target_oid=None):
        revisions = [base_oid] + ([target_oid] if target_oid else [])
        tokens = self.run(self.DIFF + ["--raw", "-z", "--no-abbrev", *revisions, "--"]).stdout.split(b"\0")
        entries = []
        i = 0
        statuses = {"A": "added", "M": "modified", "D": "deleted", "R": "renamed",
                    "C": "copied", "T": "typechanged", "U": "typechanged"}
        while i < len(tokens) and tokens[i]:
            header = tokens[i].decode("ascii").split()
            i += 1
            if len(header) != 5 or not header[0].startswith(":") or i >= len(tokens):
                fail("git_error", "invalid Git raw change record")
            mode_old, mode_new, old_oid, new_oid, status_code = header
            first = os.fsdecode(tokens[i])
            i += 1
            old_path = None
            if status_code[0] in ("R", "C"):
                old_path, first = first, os.fsdecode(tokens[i])
                i += 1
            entries.append({"path": first, "oldPath": old_path,
                            "status": statuses.get(status_code[0], "typechanged"),
                            "oldMode": mode_old[1:], "newMode": mode_new, "oldOid": old_oid, "newOid": new_oid})
        untracked = [] if target_oid else self.run(["ls-files", "--others", "--exclude-standard", "-z"]).stdout.split(b"\0")
        known = {e["path"] for e in entries}
        for path in untracked:
            if path and os.fsdecode(path) not in known:
                entries.append({"path": os.fsdecode(path), "oldPath": None, "status": "untracked",
                                "oldMode": "000000", "newMode": "100644", "oldOid": ""})
        if len(entries) > MAX_FILES:
            fail("too_large", f"review exceeds {MAX_FILES} changed files")
        counts = self.run(self.DIFF + ["--numstat", "-z", *revisions, "--"]).stdout.split(b"\0")
        by_path = {}
        i = 0
        while i < len(counts) and counts[i]:
            fields = counts[i].split(b"\t", 2)
            i += 1
            if len(fields) != 3:
                fail("git_error", "invalid Git numstat record")
            add, delete, path = fields
            if not path:
                i += 1  # rename source
                path = counts[i]
                i += 1
            binary = add == b"-" or delete == b"-"
            by_path[os.fsdecode(path)] = (0 if binary else int(add), 0 if binary else int(delete), binary)
        for entry in entries:
            add, delete, binary = by_path.get(entry["path"], (0, 0, False))
            entry.update(additions=add, deletions=delete, binary=binary, tooLarge=False)
        # --raw can report racy cached-stat dirtiness even when numstat finds no
        # content change. Keep real mode changes/renames, not phantom M rows.
        entries = [entry for entry in entries if not (
            entry["status"] == "modified" and entry["oldMode"] == entry["newMode"]
            and entry["path"] not in by_path)]
        return sorted(entries, key=lambda e: e["path"])

    def blob(self, oid):
        # One streaming cat-file process per helper call, not two processes per
        # changed file. Objects are still read one at a time, so peak memory and
        # the aggregate budget are unchanged.
        if self.reader is None:
            self.reader = BlobReader(self)
        return self.reader.read(oid)

    def close(self):
        if self.reader is not None:
            self.reader.close()
            self.reader = None

    def disk(self, path):
        """Read only regular files, opening each directory without following symlinks."""
        relative_path(path)
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            components = PurePosixPath(path).parts
            for component in components[:-1]:
                next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = next_fd
            child = os.open(components[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            try:
                info = os.fstat(child)
                if not stat.S_ISREG(info.st_mode):
                    return None, ["special", info.st_mode]
                if info.st_size > MAX_FILE_BYTES:
                    return None, ["large", info.st_size, info.st_mtime_ns, info.st_ino]
                with os.fdopen(child, "rb", closefd=False) as stream:
                    raw = stream.read(MAX_FILE_BYTES + 1)
                if len(raw) > MAX_FILE_BYTES:
                    return None, ["large", len(raw), info.st_mtime_ns, info.st_ino]
                return raw, digest(raw)
            finally:
                os.close(child)
        except OSError as exc:
            # A symlink, missing parent, or special file is metadata, never an external read.
            return None, ["unreadable", exc.errno]
        finally:
            os.close(fd)

    def file_at(self, oid, path):
        """One regular file at a commit, or in the worktree when oid is None."""
        relative_path(path)
        if oid is None:
            raw, mark = self.disk(path)
            if raw is None:
                if isinstance(mark, list) and mark[0] == "large":
                    fail("too_large", "file exceeds the review size limit", path=path)
                fail("not_found", "file is not a readable regular file in the worktree", path=path)
            return raw
        record = self.run(["--literal-pathspecs", "ls-tree", "-z", "--full-tree", oid, "--", path]).stdout.split(b"\0")[0]
        meta, _, name = record.partition(b"\t")
        fields = meta.decode("ascii", "replace").split()
        if not record or os.fsdecode(name) != path or len(fields) != 3:
            fail("not_found", "file does not exist in that revision", path=path)
        mode, kind, blob = fields
        if kind != "blob" or mode not in ("100644", "100755"):
            fail("invalid_request", "path is not a regular file in that revision", path=path)
        raw = self.blob(blob)
        if raw is None:
            fail("too_large", "file exceeds the review size limit", path=path)
        return raw

    def snapshot(self, scope="uncommitted", base=None, selected=None, select_first=False, commit=None):
        try:
            return self._snapshot(scope, base, selected, select_first, commit)
        finally:
            self.close()

    def _snapshot(self, scope, base, selected, select_first, commit):
        target_oid = None
        parent_count = None
        if scope == "pr":
            target_oid, base_oid = self.pr_endpoints(base, commit)
            base_ref = base
        elif scope == "commit":
            if base is not None:
                fail("invalid_request", "commit scope uses --commit, not --base")
            target_oid, base_oid, parent_count = self.commit_endpoints(commit)
            base_ref = base_oid if parent_count else "(empty tree)"
        else:
            if commit is not None:
                fail("invalid_request", "--commit requires commit or pr scope")
            base_ref, base_oid = self.endpoints(scope, base)
        entries = self.changes(base_oid, target_oid)
        contents = {}
        consumed = 0
        no_op_paths = set()
        for entry in entries:
            path = entry["path"]
            try:
                relative_path(path)
                if entry["oldPath"]:
                    relative_path(entry["oldPath"])
            except Failure:
                entry.update(binary=True, unsupported=True)
                contents[path] = (None, None, None, None)
                continue
            old = b""
            if entry["oldMode"] != "000000":
                if entry["oldMode"] not in ("100644", "100755"):
                    old = None
                else:
                    old = self.blob(entry["oldOid"])
            if entry["status"] == "deleted":
                new, new_mark = b"", digest(b"")
            elif target_oid:
                new = self.blob(entry["newOid"]) if entry["newMode"] in ("100644", "100755") else None
                new_mark = digest(new) if new is not None else entry["newOid"]
            else:
                new, new_mark = self.disk(path)
            old_mark = digest(old) if old is not None else entry["oldOid"]
            # Confirm no-op evidence before releasing any over-budget buffers.
            if (entry["status"] == "modified" and entry["oldMode"] == entry["newMode"]
                    and old is not None and old == new):
                no_op_paths.add(path)
                continue
            consumed += len(old or b"") + len(new or b"")
            too_large = (old is None and entry["oldMode"] in ("100644", "100755")) or (
                new is None and ((target_oid and entry["newMode"] in ("100644", "100755"))
                                 or (isinstance(new_mark, list) and new_mark[0] == "large")))
            aggregate_limit = consumed > MAX_TOTAL_BYTES
            if aggregate_limit:
                too_large = True
                entry["aggregateLimit"] = True
            binary = entry["binary"] or any(b"\0" in raw for raw in (old, new) if raw is not None)
            unsupported = (old is None and not too_large) or (new is None and not too_large)
            # Invalid UTF-8 is not safe to anchor after lossy decoding.
            if not binary and not too_large and not unsupported:
                try:
                    old_text, new_text = old.decode("utf-8"), new.decode("utf-8")
                except UnicodeDecodeError:
                    binary = True
                else:
                    if max(len(source_lines(old)), len(source_lines(new))) > MAX_LINES:
                        too_large = True
            entry.update(binary=binary, tooLarge=bool(too_large))
            if unsupported:
                entry["unsupported"] = True
            if entry["status"] == "untracked" and new is not None:
                entry["additions"] = len(new.split(b"\n")) - int(new.endswith(b"\n")) if not binary and new else 0
            if aggregate_limit:
                # Retain stable fingerprints/metadata, not every hydrated body.
                # Peak hydration is the budget plus one bounded file pair.
                old = new = None
            contents[path] = (old, new, old_mark, new_mark)
        entries = [entry for entry in entries if entry["path"] not in no_op_paths]
        for entry in entries:
            old_mark, new_mark = contents[entry["path"]][2:]
            # A file mark is independent of unrelated files and commit IDs. Both
            # sides, rename identity and modes matter. Do not attest to unreadable
            # or oversized worktree bytes using only stat/error metadata.
            entry["diffFingerprint"] = ("diff_" + digest([
                entry["path"], entry["oldPath"], entry["status"],
                entry["oldMode"], entry["newMode"], old_mark, new_mark,
            ]) if isinstance(old_mark, str) and isinstance(new_mark, str)
                and not entry.get("unsupported") else None)
        snapshot_id = "snapshot_" + digest([scope, base_oid, target_oid or self.head, [
            [e["path"], e["oldPath"], e["status"], e["oldMode"], e["newMode"],
             contents[e["path"]][2], contents[e["path"]][3]] for e in entries]])
        result = {"scope": scope, "baseRef": base_ref, "baseOid": base_oid,
                  "headOid": target_oid or self.head, "snapshotId": snapshot_id, "files": entries,
                  "file": None,
                  "warnings": ([f"Aggregate text budget ({MAX_TOTAL_BYTES:,} bytes) reached; remaining changes are metadata-only and existing discussion evidence is retained."]
                               if any(e.get("aggregateLimit") for e in entries) else [])}
        if target_oid:
            result.update(commitOid=target_oid, parentCount=parent_count)
        if selected is None and select_first and entries:
            # One call opens a review: the list plus the first changed file's hunks.
            selected = entries[0]["path"]
        if selected is not None:
            relative_path(selected)
            entry = next((e for e in entries if e["path"] == selected), None)
            if entry is None:
                fail("not_found", "file is not in the selected diff", path=selected)
            result["file"] = self.file_diff(entry, base_oid, contents[selected], snapshot_id, target_oid)
        return result, contents

    def file_diff(self, entry, base_oid, content, snapshot_id, target_oid=None):
        result = {"path": entry["path"], "diffFingerprint": entry["diffFingerprint"], "binary": entry["binary"],
                  "tooLarge": entry["tooLarge"], "unsupported": entry.get("unsupported", False), "hunks": []}
        if result["binary"] or result["tooLarge"] or result["unsupported"]:
            return result
        old, new, _, _ = content
        if entry["status"] == "untracked":
            lines = source_lines(new)
            if lines:
                result["hunks"] = [{"id": "hunk_" + digest([snapshot_id, entry["path"], 0])[:24],
                                    "oldStart": 0, "oldLines": 0, "newStart": 1, "newLines": len(lines),
                                    "rows": [{"kind": "add", "oldLine": None, "newLine": i + 1, "text": line}
                                             for i, line in enumerate(lines)]}]
            return result
        paths = ([entry["oldPath"]] if entry["oldPath"] else []) + [entry["path"]]
        # Literal pathspecs prevent filenames containing [], *, ? from matching other files.
        raw = self.run(["--literal-pathspecs"] + self.DIFF + ["--unified=3", base_oid] + ([target_oid] if target_oid else []) + ["--"] + paths).stdout
        hunk = None
        old_line = new_line = 0
        for line in raw.decode("utf-8", "replace").split("\n"):
            match = re.match(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
            if match:
                old_line, old_count, new_line, new_count = match.groups()
                old_line, new_line = int(old_line), int(new_line)
                hunk = {"id": "hunk_" + digest([snapshot_id, entry["path"], len(result["hunks"])])[:24],
                        "oldStart": old_line, "oldLines": int(old_count or 1),
                        "newStart": new_line, "newLines": int(new_count or 1), "rows": []}
                result["hunks"].append(hunk)
            elif line.startswith("diff --git "):
                hunk = None
            elif hunk is not None and line[:1] in (" ", "+", "-"):
                kind = {" ": "context", "+": "add", "-": "delete"}[line[0]]
                hunk["rows"].append({"kind": kind, "oldLine": old_line if kind != "add" else None,
                                      "newLine": new_line if kind != "delete" else None, "text": line[1:]})
                old_line += kind != "add"
                new_line += kind != "delete"
            elif hunk is not None and line.startswith("\\"):
                hunk["rows"].append({"kind": "meta", "oldLine": None, "newLine": None, "text": line})
        return result


class BlobReader:
    """Interactive `git cat-file --batch-command`: size first, contents only when bounded."""

    TIMEOUT = 20

    def __init__(self, git):
        try:
            self.process = subprocess.Popen(git.prefix + ["cat-file", "--batch-command"], cwd=git.root,
                                            env=git.environment(), stdin=subprocess.PIPE,
                                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError as exc:
            fail("git_error", f"Git unavailable: {exc}")
        # Same bound as other Git calls: a stuck process is killed, and the blocked read sees EOF.
        self.watchdog = threading.Timer(self.TIMEOUT, self.process.kill)
        self.watchdog.daemon = True
        self.watchdog.start()

    def command(self, verb, oid):
        if not re.fullmatch(r"[0-9a-f]{40,64}", oid or ""):
            fail("git_error", "invalid Git object id")
        try:
            self.process.stdin.write(f"{verb} {oid}\n".encode("ascii"))
            self.process.stdin.flush()
        except OSError:
            fail("git_error", "Git object reader stopped or timed out")
        header = self.process.stdout.readline().split()
        if len(header) != 3 or header[0].decode("ascii", "replace") != oid or header[1] != b"blob":
            fail("git_error", "Git object is missing or not a file blob")
        return int(header[2])

    def read(self, oid):
        if self.command("info", oid) > MAX_FILE_BYTES:
            return None
        size = self.command("contents", oid)
        raw = self.process.stdout.read(size + 1)
        if len(raw) != size + 1 or raw[-1:] != b"\n":
            fail("git_error", "Git object reader stopped or timed out")
        return raw[:-1]

    def close(self):
        self.watchdog.cancel()
        try:
            self.process.stdin.close()
            self.process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            self.process.kill()
            self.process.wait()
        finally:
            self.process.stdout.close()


def default_store():
    if sys.platform == "darwin":
        home = Path.home() / "Library" / "Application Support"
    else:
        home = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share")))
    return home / "herdr-codewalker" / "reviews.sqlite3"


def comparison_key(source):
    if source["scope"] == "pr":
        return source["baseRef"] + ":" + source["commitOid"]
    return source.get("commitOid") or source["baseRef"]


class Store:
    def __init__(self, git, path=None):
        self.git = git
        self.path = Path(path or default_store()).expanduser().resolve()
        if self.path == git.root or git.root in self.path.parents:
            fail("invalid_request", "review database must live outside the worktree")
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path), timeout=15, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        if stat.S_IMODE(os.stat(self.path).st_mode) != 0o600:
            os.chmod(self.path, 0o600)
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA busy_timeout=15000")
        self.db.execute("PRAGMA synchronous=FULL")
        # Reads must not write: an initialized store with this review needs no
        # write lock or fsync, and an unchanged file does not wake store watchers.
        try:
            if (self.db.execute("PRAGMA user_version").fetchone()[0] == STORE_VERSION
                    and self.db.execute("SELECT 1 FROM reviews WHERE id=?", (git.review_id,)).fetchone()):
                return
        except sqlite3.OperationalError:
            pass  # Missing tables: initialize below under the write lock.
        self.db.execute("BEGIN IMMEDIATE")
        try:
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2, 3, 4, 5, STORE_VERSION):
                fail("storage_error", "unsupported review database version", version=version)
            # Additive: existing messages keep their text, get no request and are not edited.
            if version == 1:
                self.db.execute("ALTER TABLE messages ADD COLUMN reply_requested INTEGER NOT NULL DEFAULT 0")
            if version in (1, 2):
                self.db.execute("ALTER TABLE messages ADD COLUMN edited_at TEXT")
            for sql in [
                "CREATE TABLE IF NOT EXISTS reviews (id TEXT PRIMARY KEY, root TEXT NOT NULL, git_dir TEXT NOT NULL, branch TEXT, revision INTEGER NOT NULL DEFAULT 0)",
                "CREATE TABLE IF NOT EXISTS threads (id TEXT PRIMARY KEY, review_id TEXT NOT NULL REFERENCES reviews(id), status TEXT NOT NULL, anchor TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS messages (sequence INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE, thread_id TEXT NOT NULL REFERENCES threads(id), actor TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL, reply_requested INTEGER NOT NULL DEFAULT 0, edited_at TEXT)",
                # An edit keeps the body it replaced.
                "CREATE TABLE IF NOT EXISTS message_edits (message_id TEXT NOT NULL REFERENCES messages(id), body TEXT NOT NULL, replaced_at TEXT NOT NULL)",
                # "uncertain" is written before a send, "sent" after it lands.
                "CREATE TABLE IF NOT EXISTS deliveries (message_id TEXT PRIMARY KEY REFERENCES messages(id), state TEXT NOT NULL, target TEXT NOT NULL, updated_at TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS drafts (review_id TEXT NOT NULL REFERENCES reviews(id), id TEXT NOT NULL, actor TEXT NOT NULL, body TEXT NOT NULL, target TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(review_id,id))",
                "CREATE TABLE IF NOT EXISTS commands (review_id TEXT NOT NULL REFERENCES reviews(id), id TEXT NOT NULL, fingerprint TEXT NOT NULL, result TEXT NOT NULL, PRIMARY KEY(review_id,id))",
                "CREATE TABLE IF NOT EXISTS file_views (review_id TEXT NOT NULL REFERENCES reviews(id), scope TEXT NOT NULL, base_ref TEXT NOT NULL, path TEXT NOT NULL, fingerprint TEXT NOT NULL, PRIMARY KEY(review_id,scope,base_ref,path))",
                # A walkthrough is an ordered list of steps. Each step owns one thread:
                # its first message is the step body, later messages are replies.
                "CREATE TABLE IF NOT EXISTS walkthroughs (id TEXT PRIMARY KEY, review_id TEXT NOT NULL REFERENCES reviews(id), title TEXT NOT NULL, summary TEXT NOT NULL, author TEXT NOT NULL, comparison TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS walkthrough_steps (id TEXT PRIMARY KEY, walkthrough_id TEXT NOT NULL REFERENCES walkthroughs(id), position INTEGER NOT NULL, thread_id TEXT NOT NULL UNIQUE REFERENCES threads(id), title TEXT NOT NULL, view TEXT)",
                "CREATE INDEX IF NOT EXISTS walkthroughs_review ON walkthroughs(review_id)",
                "CREATE INDEX IF NOT EXISTS walkthrough_steps_order ON walkthrough_steps(walkthrough_id,position)",
                "CREATE INDEX IF NOT EXISTS threads_review ON threads(review_id)",
                "CREATE INDEX IF NOT EXISTS messages_thread ON messages(thread_id,sequence)",
            ]:
                self.db.execute(sql)
            self.db.execute(f"PRAGMA user_version={STORE_VERSION}")
            self.db.execute("INSERT OR IGNORE INTO reviews(id,root,git_dir,branch) VALUES(?,?,?,?)",
                            (git.review_id, str(git.root), git.git_dir, git.branch))
            self.db.commit()
        except BaseException:
            self.db.rollback()
            self.db.close()
            raise

    def close(self):
        self.db.close()

    def revision(self):
        return self.db.execute("SELECT revision FROM reviews WHERE id=?", (self.git.review_id,)).fetchone()[0]

    def context(self):
        return {"repoRoot": str(self.git.root), "gitDir": self.git.git_dir, "branch": self.git.branch,
                "headOid": self.git.head, "reviewId": self.git.review_id, "revision": self.revision(),
                "storePath": str(self.path)}

    def thread(self, thread_id, source=None, contents=None):
        row = self.db.execute("SELECT * FROM threads WHERE id=? AND review_id=?", (thread_id, self.git.review_id)).fetchone()
        if row is None:
            fail("not_found", "thread not found", threadId=thread_id)
        anchor = json.loads(row["anchor"])
        step = self.db.execute("SELECT s.id, s.position, s.title, s.view, w.id AS walkthrough_id, w.title AS walkthrough_title, "
                               "(SELECT count(*) FROM walkthrough_steps c WHERE c.walkthrough_id=w.id) AS steps "
                               "FROM walkthrough_steps s JOIN walkthroughs w ON w.id=s.walkthrough_id WHERE s.thread_id=?", (thread_id,)).fetchone()
        current = False
        if step is not None:
            current = self.step_current(json.loads(step["view"]) if step["view"] else None, anchor, source, contents)
        elif source is not None:
            entry = next((e for e in source["files"] if e["path"] == anchor["path"]), None)
            if (entry and source["scope"] == anchor["scope"] and source["baseOid"] == anchor["baseOid"]
                    and source.get("commitOid") == anchor.get("commitOid")
                    and contents and anchor["path"] in contents):
                raw = contents[anchor["path"]][0 if anchor["side"] == "old" else 1]
                current = (raw is not None and digest(raw) == anchor["fingerprint"]
                           and digest(contents[anchor["path"]][2:]) == anchor.get("fileFingerprint")
                           and entry["oldPath"] == anchor.get("oldPath"))
        anchor["validity"] = "current" if current else "stale"
        messages = [{"id": msg["id"], "actor": json.loads(msg["actor"]), "body": msg["body"], "createdAt": msg["created_at"],
                     "replyRequested": bool(msg["reply_requested"]), "editedAt": msg["edited_at"],
                     "delivery": {"state": msg["state"], "target": msg["target"], "at": msg["updated_at"]} if msg["state"] else None}
                    for msg in self.db.execute("SELECT m.*, d.state, d.target, d.updated_at FROM messages m LEFT JOIN deliveries d ON d.message_id=m.id "
                                               "WHERE m.thread_id=? ORDER BY m.sequence", (thread_id,))]
        # An answer in the thread is owed while the newest message asks for one and the thread is open.
        awaiting = row["status"] == "open" and bool(messages) and messages[-1]["replyRequested"]
        walkthrough = {"id": step["walkthrough_id"], "title": step["walkthrough_title"], "stepId": step["id"],
                       "position": step["position"], "stepTitle": step["title"], "stepCount": step["steps"]} if step else None
        return {"id": row["id"], "status": row["status"], "createdAt": row["created_at"],
                "updatedAt": row["updated_at"], "anchor": anchor, "messages": messages, "awaitingReply": awaiting,
                "walkthrough": walkthrough}

    def draft(self, row):
        return {"id": row["id"], "actor": json.loads(row["actor"]), "body": row["body"],
                "target": json.loads(row["target"]), "updatedAt": row["updated_at"]}

    def listing(self, source, contents):
        # One read transaction keeps revision and all child rows mutually consistent.
        self.db.execute("BEGIN")
        try:
            result = {"reviewId": self.git.review_id, "revision": self.revision(),
                      # Step threads belong to their walkthrough, not to Review's discussions.
                      "threads": [self.thread(row[0], source, contents) for row in self.db.execute(
                          "SELECT id FROM threads WHERE review_id=? AND id NOT IN (SELECT thread_id FROM walkthrough_steps) "
                          "ORDER BY created_at,id", (self.git.review_id,))],
                      "walkthroughs": self.walkthrough_summaries(),
                      "viewedFiles": {row["path"]: row["fingerprint"] for row in self.db.execute(
                          "SELECT path,fingerprint FROM file_views WHERE review_id=? AND scope=? AND base_ref=?",
                          (self.git.review_id, source["scope"], comparison_key(source)))},
                      "drafts": [self.draft(row) for row in self.db.execute(
                          "SELECT * FROM drafts WHERE review_id=? ORDER BY updated_at,id", (self.git.review_id,))]}
            current = {entry["path"]: entry["diffFingerprint"] for entry in source["files"]}
            result["viewedFiles"] = {path: mark for path, mark in result["viewedFiles"].items()
                                     if current.get(path) == mark}
            self.db.commit()
            return result
        except BaseException:
            self.db.rollback()
            raise

    # Walkthroughs --------------------------------------------------------

    def step_current(self, view, anchor, source, contents):
        """A step is current while its frozen excerpt still matches its comparison."""
        if view is None:
            return True
        if (source is None or source["scope"] != anchor["scope"] or source["baseOid"] != anchor["baseOid"]
                or source.get("commitOid") != anchor.get("commitOid")):
            return False
        if view["kind"] == "source":
            # Commit objects never change; only a worktree side can.
            if view["rev"] == "base" or anchor.get("commitOid"):
                return True
            raw, _ = self.git.disk(view["path"])
            return raw is not None and digest(raw) == view["fingerprint"]
        entry = next((e for e in source["files"] if e["path"] == view["path"]), None)
        return bool(entry and contents and view["path"] in contents and entry["oldPath"] == view["oldPath"]
                    and digest(contents[view["path"]][2:]) == view["fileFingerprint"])

    def step_view(self, given, source, contents, diffs):
        """Validate a step's view and freeze the rows it shows. The caller never supplies rows."""
        if given is None:
            return None
        if not isinstance(given, dict):
            fail("invalid_request", "view must be an object or null")
        kind = given.get("kind")
        allowed = {"diff": {"kind", "path", "side", "startLine", "endLine"},
                   "source": {"kind", "path", "rev", "startLine", "endLine"}}.get(kind)
        if allowed is None:
            fail("invalid_request", "view.kind must be diff or source")
        if set(given) - allowed:
            fail("invalid_request", "unknown view fields", fields=sorted(set(given) - allowed))
        path = relative_path(given.get("path"))
        ranged = "startLine" in given or "endLine" in given
        if ranged or kind == "source":
            start = positive(given.get("startLine"), "view.startLine")
            end = positive(given.get("endLine"), "view.endLine")
            if end < start:
                fail("invalid_request", "view.endLine must not precede view.startLine")
            if end - start + 1 > MAX_STEP_ROWS:
                fail("invalid_request", f"a step shows at most {MAX_STEP_ROWS} lines; choose a smaller range")
        if kind == "source":
            rev = given.get("rev")
            if rev not in ("base", "head"):
                fail("invalid_request", "view.rev must be base or head")
            # head without a commit is the worktree, as in uncommitted and branch scope.
            raw = self.git.file_at(source["baseOid"] if rev == "base" else source.get("commitOid"), path)
            if b"\0" in raw:
                fail("invalid_request", "binary files have no text lines", path=path)
            try:
                lines = source_lines(raw)
            except UnicodeDecodeError:
                fail("invalid_request", "file is not UTF-8 text", path=path)
            if end > len(lines):
                fail("invalid_request", "view lines are past the end of the file", path=path, lines=len(lines))
            side = "oldLine" if rev == "base" else "newLine"
            rows = [{"kind": "context", "oldLine": None, "newLine": None, "text": lines[n - 1], side: n}
                    for n in range(start, end + 1)]
            return {"kind": "source", "path": path, "rev": rev, "startLine": start, "endLine": end,
                    "fingerprint": digest(raw), "hunks": [{"rows": rows}]}
        entry = next((e for e in source["files"] if e["path"] == path), None)
        if entry is None:
            fail("not_found", "file is not in the selected diff; use a source view for unchanged files", path=path)
        if entry["binary"] or entry["tooLarge"] or entry.get("unsupported"):
            fail("invalid_request", "this change has no text lines to show", path=path)
        if path not in diffs:
            diffs[path] = self.git.file_diff(entry, source["baseOid"], contents[path], source["snapshotId"], source.get("commitOid"))
        hunks = []
        side = None
        if ranged:
            side = given.get("side")
            if side not in ("old", "new"):
                fail("invalid_request", "view.side must be old or new")
            coordinate = "oldLine" if side == "old" else "newLine"
            available = {r[coordinate] for h in diffs[path]["hunks"] for r in h["rows"] if r[coordinate] is not None}
            if not all(n in available for n in range(start, end + 1)):
                fail("invalid_request", "view lines are not rendered on this side of the selected diff", path=path)
            for hunk in diffs[path]["hunks"]:
                rows = hunk["rows"]
                found = [i for i, r in enumerate(rows) if r[coordinate] is not None and start <= r[coordinate] <= end]
                if not found:
                    continue
                low, high = found[0], found[-1]
                # Keep the other side of a replacement that touches the range.
                while low > 0 and rows[low - 1][coordinate] is None and rows[low - 1]["kind"] != "meta":
                    low -= 1
                while high + 1 < len(rows) and rows[high + 1][coordinate] is None:
                    high += 1
                hunks.append({"rows": rows[low:high + 1]})
        else:
            start = end = None
            hunks = [{"rows": hunk["rows"]} for hunk in diffs[path]["hunks"]]
        if not hunks:
            fail("invalid_request", "this change has no text lines to show", path=path)
        if sum(len(h["rows"]) for h in hunks) > MAX_STEP_ROWS:
            fail("invalid_request", f"a step shows at most {MAX_STEP_ROWS} diff rows; choose a line range", path=path)
        return {"kind": "diff", "path": path, "oldPath": entry["oldPath"], "status": entry["status"],
                "side": side, "startLine": start, "endLine": end,
                "fileFingerprint": digest(contents[path][2:]), "hunks": hunks}

    def add_steps(self, walkthrough_id, steps, source, contents, actor, timestamp, position):
        if not isinstance(steps, list) or not steps:
            fail("invalid_request", "steps must be a nonempty list")
        count = self.db.execute("SELECT count(*) FROM walkthrough_steps WHERE walkthrough_id=?", (walkthrough_id,)).fetchone()[0]
        if count + len(steps) > MAX_STEPS:
            fail("invalid_request", f"a walkthrough has at most {MAX_STEPS} steps")
        self.db.execute("UPDATE walkthrough_steps SET position=position+? WHERE walkthrough_id=? AND position>=?",
                        (len(steps), walkthrough_id, position))
        evidence = {key: source[key] for key in ("scope", "baseRef", "baseOid", "headOid", "snapshotId")}
        if source.get("commitOid"):
            evidence["commitOid"] = source["commitOid"]
        diffs = {}
        try:
            for index, step in enumerate(steps):
                try:
                    if not isinstance(step, dict) or set(step) - {"title", "body", "view"}:
                        fail("invalid_request", "a step has title, body and an optional view")
                    title = text(step.get("title"), "title", MAX_TITLE_BYTES)
                    body = text(step.get("body"), "body", MAX_BODY_BYTES)
                    view = self.step_view(step.get("view"), source, contents, diffs)
                except Failure as exc:
                    exc.details.setdefault("step", index)
                    raise
                step_id, thread_id = identifier("step"), identifier("thread")
                # The anchor pins the comparison, so replies recheck the same source.
                anchor = dict(evidence, walkthroughId=walkthrough_id, stepId=step_id, path=view["path"] if view else None)
                self.db.execute("INSERT INTO threads VALUES(?,?,?,?,?,?)",
                                (thread_id, self.git.review_id, "open", canonical_json(anchor), timestamp, timestamp))
                self.add_message(thread_id, actor, body, timestamp)
                self.db.execute("INSERT INTO walkthrough_steps VALUES(?,?,?,?,?,?)",
                                (step_id, walkthrough_id, position + index, thread_id, title, canonical_json(view) if view else None))
        finally:
            self.git.close()

    def walkthrough_row(self, walkthrough_id):
        walkthrough_id = text(walkthrough_id, "walkthroughId")
        row = self.db.execute("SELECT * FROM walkthroughs WHERE id=? AND review_id=?", (walkthrough_id, self.git.review_id)).fetchone()
        if row is None:
            fail("not_found", "walkthrough not found in this review", walkthroughId=walkthrough_id)
        return row

    def comparison_source(self, comparison):
        """Snapshot of a walkthrough's comparison, or None when it is gone (for example a deleted base)."""
        try:
            return self.git.snapshot(comparison["scope"], None if comparison["scope"] == "commit" else comparison["baseRef"],
                                     commit=comparison.get("commitOid"))
        except Failure:
            return None, None

    def walkthrough_summary(self, row):
        threads = [self.thread(thread_id) for (thread_id,) in self.db.execute(
            "SELECT thread_id FROM walkthrough_steps WHERE walkthrough_id=? ORDER BY position", (row["id"],))]
        return {"id": row["id"], "title": row["title"], "summary": row["summary"], "status": row["status"],
                "author": json.loads(row["author"]), "comparison": json.loads(row["comparison"]),
                "createdAt": row["created_at"], "updatedAt": row["updated_at"], "stepCount": len(threads),
                "openThreads": sum(t["status"] == "open" for t in threads),
                "awaitingReply": sum(t["awaitingReply"] for t in threads),
                "replies": sum(len(t["messages"]) - 1 for t in threads)}

    def walkthrough_summaries(self):
        return [self.walkthrough_summary(row) for row in self.db.execute(
            "SELECT * FROM walkthroughs WHERE review_id=? ORDER BY created_at,id", (self.git.review_id,)).fetchall()]

    def walkthrough(self, walkthrough_id, source=None, contents=None):
        result = self.walkthrough_summary(self.walkthrough_row(walkthrough_id))
        result["steps"] = []
        for row in self.db.execute("SELECT * FROM walkthrough_steps WHERE walkthrough_id=? ORDER BY position",
                                   (walkthrough_id,)).fetchall():
            thread = self.thread(row["thread_id"], source, contents)
            result["steps"].append({"id": row["id"], "position": row["position"], "title": row["title"],
                                    "view": json.loads(row["view"]) if row["view"] else None,
                                    "validity": thread["anchor"]["validity"], "thread": thread})
        result["validity"] = "current" if all(s["validity"] == "current" for s in result["steps"]) else "stale"
        return result

    def read_walkthrough(self, walkthrough_id):
        source, contents = self.comparison_source(json.loads(self.walkthrough_row(walkthrough_id)["comparison"]))
        self.db.execute("BEGIN")
        try:
            # context gives the UI the store to watch; drafts restore an unsent step reply.
            result = {"reviewId": self.git.review_id, "revision": self.revision(), "context": self.context(),
                      "walkthrough": self.walkthrough(walkthrough_id, source, contents),
                      "drafts": [self.draft(row) for row in self.db.execute(
                          "SELECT * FROM drafts WHERE review_id=? ORDER BY updated_at,id", (self.git.review_id,))]}
            self.db.commit()
            return result
        except BaseException:
            self.db.rollback()
            raise

    def read_walkthroughs(self):
        self.db.execute("BEGIN")
        try:
            result = {"reviewId": self.git.review_id, "revision": self.revision(), "context": self.context(),
                      "walkthroughs": self.walkthrough_summaries()}
            self.db.commit()
            return result
        except BaseException:
            self.db.rollback()
            raise

    def mutate(self, operation, data):
        if not isinstance(data, dict):
            fail("invalid_request", "--data must be one JSON object")
        allowed = {"commandId", "reviewId", "expectedRevision", "actor"} | {
            "create": {"scope", "base", "commit", "anchor", "body", "replyRequested"}, "reply": {"threadId", "body", "replyRequested"},
            "resolve": {"threadId", "resolved"}, "edit": {"messageId", "body"}, "delete": {"messageId"}, "deliver": {"messageIds", "state", "target"}, "draft-save": {"draftId", "body", "target"},
            "draft-delete": {"draftId"}, "history-clear": {"confirmed"},
            "file-view": {"scope", "base", "commit", "path", "diffFingerprint", "viewed"},
            "walkthrough-create": {"scope", "base", "commit", "snapshotId", "title", "summary", "steps"},
            "walkthrough-append": {"walkthroughId", "snapshotId", "afterStepId", "steps"},
            "walkthrough-update": {"walkthroughId", "title", "summary", "status"},
            "walkthrough-delete": {"walkthroughId", "confirmed"}}[operation]
        if set(data) - allowed:
            fail("invalid_request", "unknown mutation fields", fields=sorted(set(data) - allowed))
        command_id = text(data.get("commandId"), "commandId", 256)
        review_id = text(data.get("reviewId"), "reviewId", 256)
        if review_id != self.git.review_id:
            fail("review_conflict", "worktree or branch changed; reload the pinned review", expectedReviewId=review_id, currentReviewId=self.git.review_id)
        expected = positive(data.get("expectedRevision"), "expectedRevision", minimum=0)
        actor = actor_data(data.get("actor"))
        if operation == "history-clear" and (actor["kind"] != "human" or data.get("confirmed") is not True):
            fail("invalid_request", "clearing history requires a human actor and confirmed:true")
        requested = data.get("replyRequested", False)
        if type(requested) is not bool:
            fail("invalid_request", "replyRequested must be true or false")
        fingerprint = digest([operation, data])
        self.db.execute("BEGIN IMMEDIATE")
        try:
            previous = self.db.execute("SELECT * FROM commands WHERE review_id=? AND id=?", (self.git.review_id, command_id)).fetchone()
            if previous:
                if previous["fingerprint"] != fingerprint:
                    fail("idempotency_conflict", "commandId was used with different data")
                self.db.commit()
                return json.loads(previous["result"])
            revision = self.revision()
            if expected != revision:
                fail("revision_conflict", "review changed; reload before writing", expectedRevision=expected, currentRevision=revision)
            timestamp = now()
            result = {"reviewId": self.git.review_id, "revision": revision + 1}
            if operation == "create":
                body = text(data.get("body"), "body", MAX_BODY_BYTES)
                given = data.get("anchor")
                if not isinstance(given, dict) or set(given) != {"path", "side", "startLine", "endLine", "snapshotId"}:
                    fail("invalid_request", "anchor requires path, side, startLine, endLine and snapshotId")
                path = relative_path(given["path"])
                side = given["side"]
                if side not in ("old", "new"):
                    fail("invalid_request", "anchor.side must be old or new")
                start = positive(given["startLine"], "startLine")
                end = positive(given["endLine"], "endLine")
                if end < start:
                    fail("invalid_request", "endLine must not precede startLine")
                source, contents = self.git.snapshot(data.get("scope", "uncommitted"), data.get("base"), path, commit=data.get("commit"))
                if given["snapshotId"] != source["snapshotId"]:
                    fail("snapshot_conflict", "diff changed; refresh before creating an annotation", snapshotId=source["snapshotId"])
                if source["file"]["binary"] or source["file"]["tooLarge"] or source["file"]["unsupported"]:
                    fail("invalid_request", "this change has no annotatable text lines")
                coordinate = "oldLine" if side == "old" else "newLine"
                available = {r[coordinate] for h in source["file"]["hunks"] for r in h["rows"] if r[coordinate] is not None}
                if end - start > MAX_LINES or not all(n in available for n in range(start, end + 1)):
                    fail("invalid_request", "anchor lines are not rendered on this side of the selected diff")
                entry = next(e for e in source["files"] if e["path"] == path)
                raw = contents[path][0 if side == "old" else 1]
                lines = source_lines(raw)
                anchor = dict(given, oldPath=entry["oldPath"], scope=source["scope"], baseRef=source["baseRef"],
                              baseOid=source["baseOid"], headOid=source["headOid"], fingerprint=digest(raw),
                              fileFingerprint=digest(contents[path][2:]),
                              snippet="\n".join(lines[start - 1:end]))
                if source.get("commitOid"):
                    anchor["commitOid"] = source["commitOid"]
                thread_id = identifier("thread")
                self.db.execute("INSERT INTO threads VALUES(?,?,?,?,?,?)", (thread_id, self.git.review_id, "open", canonical_json(anchor), timestamp, timestamp))
                self.add_message(thread_id, actor, body, timestamp, requested)
                result["thread"] = self.thread(thread_id, source, contents)
            elif operation == "file-view":
                path = relative_path(data.get("path"))
                if type(data.get("viewed")) is not bool:
                    fail("invalid_request", "viewed must be true or false")
                given = text(data.get("diffFingerprint"), "diffFingerprint")
                source, _ = self.git.snapshot(data.get("scope", "uncommitted"), data.get("base"), commit=data.get("commit"))
                entry = next((e for e in source["files"] if e["path"] == path), None)
                if entry is None:
                    fail("not_found", "file is not in the selected diff", path=path)
                if not entry["diffFingerprint"]:
                    fail("invalid_request", "this file has no reliable content fingerprint")
                if given != entry["diffFingerprint"]:
                    fail("snapshot_conflict", "this file changed; refresh before marking it viewed")
                key = (self.git.review_id, source["scope"], comparison_key(source), path)
                if data["viewed"]:
                    self.db.execute("INSERT OR REPLACE INTO file_views VALUES(?,?,?,?,?)", (*key, given))
                else:
                    self.db.execute("DELETE FROM file_views WHERE review_id=? AND scope=? AND base_ref=? AND path=?", key)
                result["fileView"] = {"path": path, "diffFingerprint": given, "viewed": data["viewed"],
                                      "scope": source["scope"], "baseRef": source["baseRef"],
                                      **({"commitOid": source["commitOid"]} if source.get("commitOid") else {})}
            elif operation in ("walkthrough-create", "walkthrough-append"):
                # Only agents write walkthroughs; people read, reply, resolve and delete.
                if actor["kind"] != "agent":
                    fail("invalid_request", "only an agent writes walkthrough steps")
                if operation == "walkthrough-create":
                    title = text(data.get("title"), "title", MAX_TITLE_BYTES)
                    summary = text(data.get("summary", ""), "summary", MAX_BODY_BYTES, blank=True)
                    scope, base, commit = data.get("scope", "uncommitted"), data.get("base"), data.get("commit")
                else:
                    walkthrough = self.walkthrough_row(data.get("walkthroughId"))
                    if (json.loads(walkthrough["author"])["kind"], json.loads(walkthrough["author"])["id"]) != (actor["kind"], actor["id"]):
                        fail("invalid_request", "only the author adds steps to a walkthrough")
                    comparison = json.loads(walkthrough["comparison"])
                    scope, commit = comparison["scope"], comparison.get("commitOid")
                    base = None if scope == "commit" else comparison["baseRef"]
                snapshot_id = text(data.get("snapshotId"), "snapshotId")
                source, contents = self.git.snapshot(scope, base, commit=commit)
                if snapshot_id != source["snapshotId"]:
                    fail("snapshot_conflict", "diff changed; read it again before writing steps", snapshotId=source["snapshotId"])
                if operation == "walkthrough-create":
                    walkthrough_id = identifier("walkthrough")
                    comparison = {"scope": source["scope"], "baseRef": source["baseRef"]}
                    if source.get("commitOid"):
                        comparison["commitOid"] = source["commitOid"]
                    self.db.execute("INSERT INTO walkthroughs VALUES(?,?,?,?,?,?,?,?,?)",
                                    (walkthrough_id, self.git.review_id, title, summary, canonical_json(actor),
                                     canonical_json(comparison), "open", timestamp, timestamp))
                    position = 0
                else:
                    walkthrough_id = walkthrough["id"]
                    after = data.get("afterStepId")
                    if after is None:
                        position = self.db.execute("SELECT count(*) FROM walkthrough_steps WHERE walkthrough_id=?", (walkthrough_id,)).fetchone()[0]
                    else:
                        found = self.db.execute("SELECT position FROM walkthrough_steps WHERE id=? AND walkthrough_id=?",
                                                (text(after, "afterStepId"), walkthrough_id)).fetchone()
                        if found is None:
                            fail("not_found", "step not found in this walkthrough", stepId=after)
                        position = found[0] + 1
                    self.db.execute("UPDATE walkthroughs SET updated_at=? WHERE id=?", (timestamp, walkthrough_id))
                self.add_steps(walkthrough_id, data.get("steps"), source, contents, actor, timestamp, position)
                result["walkthrough"] = self.walkthrough(walkthrough_id, source, contents)
            elif operation == "walkthrough-update":
                walkthrough = self.walkthrough_row(data.get("walkthroughId"))
                author = json.loads(walkthrough["author"])
                changes = {}
                if "title" in data:
                    changes["title"] = text(data["title"], "title", MAX_TITLE_BYTES)
                if "summary" in data:
                    changes["summary"] = text(data["summary"], "summary", MAX_BODY_BYTES, blank=True)
                if changes and (author["kind"], author["id"]) != (actor["kind"], actor["id"]):
                    fail("invalid_request", "only the author changes a walkthrough's title or summary")
                if "status" in data:
                    if data["status"] not in WALKTHROUGH_STATES:
                        fail("invalid_request", "status must be open or archived")
                    changes["status"] = data["status"]
                if not changes:
                    fail("invalid_request", "walkthrough-update needs title, summary or status")
                for column, value in changes.items():
                    self.db.execute(f"UPDATE walkthroughs SET {column}=? WHERE id=?", (value, walkthrough["id"]))
                self.db.execute("UPDATE walkthroughs SET updated_at=? WHERE id=?", (timestamp, walkthrough["id"]))
                source, contents = self.comparison_source(json.loads(walkthrough["comparison"]))
                result["walkthrough"] = self.walkthrough(walkthrough["id"], source, contents)
            elif operation == "walkthrough-delete":
                walkthrough = self.walkthrough_row(data.get("walkthroughId"))
                author = json.loads(walkthrough["author"])
                if not ((author["kind"], author["id"]) == (actor["kind"], actor["id"])
                        or (actor["kind"] == "human" and data.get("confirmed") is True)):
                    fail("invalid_request", "only the author, or a person with confirmed:true, deletes a walkthrough")
                threads = [row[0] for row in self.db.execute("SELECT thread_id FROM walkthrough_steps WHERE walkthrough_id=?", (walkthrough["id"],))]
                self.db.execute("DELETE FROM walkthrough_steps WHERE walkthrough_id=?", (walkthrough["id"],))
                for thread_id in threads:
                    for table in ("message_edits", "deliveries"):
                        self.db.execute(f"DELETE FROM {table} WHERE message_id IN (SELECT id FROM messages WHERE thread_id=?)", (thread_id,))
                    self.db.execute("DELETE FROM messages WHERE thread_id=?", (thread_id,))
                    self.db.execute("DELETE FROM threads WHERE id=?", (thread_id,))
                self.db.execute("DELETE FROM walkthroughs WHERE id=?", (walkthrough["id"],))
                result["deleted"] = {"walkthroughId": walkthrough["id"], "steps": len(threads)}
            elif operation == "deliver":
                ids = data.get("messageIds")
                if not isinstance(ids, list) or not ids or len(ids) > 1000 or len(set(map(str, ids))) != len(ids):
                    fail("invalid_request", "messageIds must be 1 to 1000 distinct message IDs")
                state = data.get("state")
                if state not in (*DELIVERY_STATES, "none"):
                    fail("invalid_request", "state must be uncertain, sent or none")
                target = text(data.get("target", ""), "target", 256, blank=True)
                for message_id in ids:
                    message_id = text(message_id, "messageIds")
                    found = self.db.execute("SELECT 1 FROM messages m JOIN threads t ON t.id=m.thread_id WHERE m.id=? AND t.review_id=?",
                                            (message_id, self.git.review_id)).fetchone()
                    if found is None:
                        fail("not_found", "message not found", messageId=message_id)
                    if state == "none":
                        self.db.execute("DELETE FROM deliveries WHERE message_id=?", (message_id,))
                    else:
                        self.db.execute("INSERT OR REPLACE INTO deliveries VALUES(?,?,?,?)", (message_id, state, target, timestamp))
                result["delivered"] = {"messageIds": ids, "state": state}
            elif operation == "history-clear":
                # Explicitly clear this review only. Keep drafts, viewed marks and
                # command receipts so retries cannot delete later discussions.
                # Walkthrough step threads stay: walkthrough-delete removes them.
                threads = "SELECT id FROM threads WHERE review_id=? AND id NOT IN (SELECT thread_id FROM walkthrough_steps)"
                count = self.db.execute(f"SELECT count(*) FROM ({threads})", (self.git.review_id,)).fetchone()[0]
                messages = f"SELECT id FROM messages WHERE thread_id IN ({threads})"
                for table in ("message_edits", "deliveries"):
                    self.db.execute(f"DELETE FROM {table} WHERE message_id IN ({messages})", (self.git.review_id,))
                self.db.execute(f"DELETE FROM messages WHERE thread_id IN ({threads})", (self.git.review_id,))
                self.db.execute(f"DELETE FROM threads WHERE id IN ({threads})", (self.git.review_id,))
                result["cleared"] = {"threads": count}
            elif operation == "delete":
                message = self.own_message(data, actor)
                thread_id = message["thread_id"]
                first = self.db.execute("SELECT id FROM messages WHERE thread_id=? ORDER BY sequence LIMIT 1", (thread_id,)).fetchone()[0]
                if first == message["id"] and self.db.execute("SELECT 1 FROM walkthrough_steps WHERE thread_id=?", (thread_id,)).fetchone():
                    fail("invalid_request", "the first message is the walkthrough step; use walkthrough-delete")
                # The first comment is the thread: deleting it removes every reply too.
                doomed = [row[0] for row in self.db.execute("SELECT id FROM messages WHERE thread_id=?", (thread_id,))] if first == message["id"] else [message["id"]]
                self.db.executemany("DELETE FROM message_edits WHERE message_id=?", [(item,) for item in doomed])
                self.db.executemany("DELETE FROM deliveries WHERE message_id=?", [(item,) for item in doomed])
                self.db.executemany("DELETE FROM messages WHERE id=?", [(item,) for item in doomed])
                result["deleted"] = {"messageId": message["id"], "threadId": thread_id, "thread": first == message["id"]}
                if first == message["id"]:
                    self.db.execute("DELETE FROM threads WHERE id=?", (thread_id,))
                else:
                    self.db.execute("UPDATE threads SET updated_at=? WHERE id=?", (timestamp, thread_id))
                    result["thread"] = self.thread(thread_id)
            elif operation in ("reply", "resolve", "edit"):
                if operation == "edit":
                    message = self.own_message(data, actor)
                    message_id = message["id"]
                    thread_id = message["thread_id"]
                else:
                    thread_id = text(data.get("threadId"), "threadId")
                thread = self.thread(thread_id)
                if operation == "edit":
                    body = text(data.get("body"), "body", MAX_BODY_BYTES)
                    if body != message["body"]:
                        self.db.execute("INSERT INTO message_edits VALUES(?,?,?)", (message_id, message["body"], timestamp))
                        self.db.execute("UPDATE messages SET body=?, edited_at=? WHERE id=?", (body, timestamp, message_id))
                elif operation == "reply":
                    body = text(data.get("body"), "body", MAX_BODY_BYTES)
                    self.add_message(thread_id, actor, body, timestamp, requested)
                else:
                    if type(data.get("resolved")) is not bool:
                        fail("invalid_request", "resolved must be true or false")
                    self.db.execute("UPDATE threads SET status=? WHERE id=?", ("resolved" if data["resolved"] else "open", thread_id))
                self.db.execute("UPDATE threads SET updated_at=? WHERE id=?", (timestamp, thread_id))
                source = contents = None
                try:
                    source, contents = self.git.snapshot(thread["anchor"]["scope"],
                        None if thread["anchor"]["scope"] == "commit" else thread["anchor"]["baseRef"],
                        commit=thread["anchor"].get("commitOid"))
                except Failure:
                    pass  # A missing old base cannot prevent a reply to its durable discussion.
                result["thread"] = self.thread(thread_id, source, contents)
            else:
                draft_id = text(data.get("draftId"), "draftId")
                previous_draft = self.db.execute("SELECT * FROM drafts WHERE review_id=? AND id=?", (self.git.review_id, draft_id)).fetchone()
                if previous_draft:
                    owner = json.loads(previous_draft["actor"])
                    if (owner["kind"], owner["id"]) != (actor["kind"], actor["id"]):
                        fail("invalid_request", "draft belongs to a different actor")
                if operation == "draft-save":
                    body = text(data.get("body"), "body", MAX_BODY_BYTES, blank=True)
                    target = data.get("target")
                    if not isinstance(target, dict) or len(canonical_json(target).encode("utf-8")) > 524_288:
                        fail("invalid_request", "draft target must be a JSON object of at most 512 KiB")
                    self.db.execute("INSERT OR REPLACE INTO drafts VALUES(?,?,?,?,?,?)",
                                    (self.git.review_id, draft_id, canonical_json(actor), body, canonical_json(target), timestamp))
                    result["draft"] = {"id": draft_id, "actor": actor, "body": body, "target": target, "updatedAt": timestamp}
                else:
                    self.db.execute("DELETE FROM drafts WHERE review_id=? AND id=?", (self.git.review_id, draft_id))
                    result["draftId"] = draft_id
            current_branch = self.git.line(["symbolic-ref", "--quiet", "--short", "HEAD"], optional=True)
            current_head = self.git.line(["rev-parse", "--verify", "--quiet", "HEAD^{commit}"], optional=True) if not current_branch else None
            if current_branch != self.git.branch or (not current_branch and current_head != self.git.head):
                fail("review_conflict", "branch identity changed during mutation; reload the pinned review")
            if len(canonical_json(result).encode("utf-8")) > MAX_OUTPUT - 1024:
                fail("too_large", "mutation result exceeds response limit; export/archive this discussion first")
            self.db.execute("UPDATE reviews SET revision=revision+1 WHERE id=?", (self.git.review_id,))
            self.db.execute("INSERT INTO commands VALUES(?,?,?,?)", (self.git.review_id, command_id, fingerprint, canonical_json(result)))
            self.db.commit()
            return result
        except BaseException:
            self.db.rollback()
            raise

    def own_message(self, data, actor):
        message_id = text(data.get("messageId"), "messageId")
        message = self.db.execute("SELECT m.* FROM messages m JOIN threads t ON t.id=m.thread_id WHERE m.id=? AND t.review_id=?",
                                  (message_id, self.git.review_id)).fetchone()
        if message is None:
            fail("not_found", "message not found", messageId=message_id)
        author = json.loads(message["actor"])
        if (author["kind"], author["id"]) != (actor["kind"], actor["id"]):
            fail("invalid_request", "only the author can change a message")
        return message

    def add_message(self, thread_id, actor, body, timestamp, requested=False):
        self.db.execute("INSERT INTO messages(id,thread_id,actor,body,created_at,reply_requested) VALUES(?,?,?,?,?,?)",
                        (identifier("message"), thread_id, canonical_json(actor), body, timestamp, int(requested)))


def export_text(listing):
    blocks = []
    for thread in listing["threads"]:
        anchor = thread["anchor"]
        awaiting = "; reply in thread required" if thread["awaitingReply"] else ""
        blocks.append(f"{anchor['path']}:{anchor['startLine']}-{anchor['endLine']} ({anchor['side']}) [{thread['status']}; {anchor['validity']}{awaiting}] {thread['id']}\n{anchor['snippet']}")
        if anchor.get("commitOid"):
            blocks.append(f"Historical commit {anchor['commitOid']}; inspect the current code before making changes.")
        blocks.extend(f"{message['actor']['name']} ({message['actor']['kind']}):\n{message['body']}" for message in thread["messages"])
    for walkthrough in listing.get("walkthroughs", []):
        awaiting = f"; {walkthrough['awaitingReply']} short replies requested" if walkthrough["awaitingReply"] else ""
        blocks.append(f"Walkthrough \"{walkthrough['title']}\" [{walkthrough['status']}; {walkthrough['stepCount']} steps; "
                      f"{walkthrough['replies']} replies{awaiting}] {walkthrough['id']}")
    return "\n\n".join(blocks)


# Printed by `skill`: what an agent needs to answer review threads. Send to agent
# pastes only the command that prints it, so the recipes live in one place.
SKILL = """# Codewalker: answer review feedback

The reviewer uses Codewalker, a review panel in Herdr. Each comment is a thread on
lines of this checkout's diff. A reply in the thread shows in their panel; an
answer only in chat does not.

Helper: python3 {helper}
Checkout: {repo}
Each command prints one JSON value: {{"ok": true, "data": ...}}, or
{{"ok": false, "error": {{"code": ..., "message": ...}}}} with exit 1.

## Rules

- A thread marked "reply in thread required" must get your answer in that
  thread, also when you answer in chat. Answer the question or say what you
  will change, then continue your work.
- A thread marked "code changed since" points at lines that changed after the
  comment. Its code block is the code as it was then. Read the current file
  before you act.
- For other threads, act on the comment. You can answer in chat. Reply in the
  thread when you have a question, you disagree, or you cannot do it.
- Historical commit comments refer to that commit, not necessarily the current
  working tree. Inspect the current code before making changes.
- Reply by thread ID. Do not open a new thread to answer.
- The reviewer resolves threads. Resolve one only when they ask you to.
- Change only your own messages. Never edit the SQLite store.
- A thread with a "walkthrough" field is a step of a walkthrough you wrote. Its
  first message is the step itself. Reply in it the same way; do not delete
  that first message. `skill --topic walkthrough` explains walkthroughs.

## Read the threads

```sh
python3 {helper_q} --repo {repo_q} list --json
```

`data.threads[]` has `id`, `status` (open or resolved), `awaitingReply`,
`anchor` (`path`, `side`, `startLine`, `endLine`, `snippet`) and `messages[]`
(`id`, `actor`, `body`). Messages with `actor.kind` "human" are the reviewer's.

## Reply, edit or delete

Set the four values at the top, then run it as is. The quoted heredoc keeps
the shell away from the text, so any characters are safe in BODY.

```sh
python3 - <<'PY'
ACTION = "reply"                 # "reply", "edit" or "delete"
TARGET = "<thread id>"           # reply: a thread ID; edit or delete: your message ID
BODY = \"\"\"<your reply>\"\"\"  # not used by delete
NAME = "<your agent name>"

import json, subprocess, uuid
cw = ["python3", {helper_r}, "--repo", {repo_r}]
def run(*args):
    return json.loads(subprocess.run(cw + list(args) + ["--json"], capture_output=True, text=True).stdout)
for _ in range(3):
    state = run("list")
    if not state["ok"]:
        break
    request = {{"commandId": uuid.uuid4().hex, "reviewId": state["data"]["reviewId"],
               "expectedRevision": state["data"]["revision"],
               "actor": {{"kind": "agent", "id": NAME.lower(), "name": NAME}},
               "threadId" if ACTION == "reply" else "messageId": TARGET}}
    if ACTION != "delete":
        request["body"] = BODY
    state = run(ACTION, "--data", json.dumps(request))
    if state["ok"] or state["error"]["code"] != "revision_conflict":
        break
print(json.dumps(state, indent=2))
PY
```

Use the same NAME every time: only the author can edit or delete a message.
Deleting the first message of a thread deletes the whole thread.

## Errors

- `revision_conflict`: another write came first. The recipe reads the threads
  again and tries up to three times. If it still fails, read the new replies
  before you try again.
- No output, or the process stopped: list the threads and look for your reply
  before you send it again. Each run makes a new command ID.
- `invalid_request` on edit or delete: the message is not yours, or the ID is wrong.
- `storage_error`: stop and tell the reviewer. Do not delete or recreate the store.

To start a thread yourself, or for all fields and errors, see okf/agent-workflow.md
and okf/protocol.md in the herdr-codewalker repository.
"""


# Printed by `skill --topic walkthrough`: how an agent writes a walkthrough.
WALKTHROUGH_SKILL = """# Codewalker: write a code walkthrough

A walkthrough is an ordered list of steps that you write for the reviewer.
Each step has a title, a Markdown body and an optional view: a range of the
diff, or lines of a file. Codewalker copies the lines when you create the
walkthrough, so it stays readable after the code changes. Each step is also
a thread: the reviewer replies in it, and you answer there.

Helper: python3 {helper}
Checkout: {repo}
Each command prints one JSON value: {{"ok": true, "data": ...}}, or
{{"ok": false, "error": {{"code": ..., "message": ..., "details": ...}}}} with exit 1.

## 1. Read the diff and the line numbers

```sh
python3 {helper_q} --repo {repo_q} review --scope uncommitted --json
python3 {helper_q} --repo {repo_q} review --scope uncommitted --file <path> --json
```

Use `--scope branch --base main` for a branch, or `--scope commit --commit <sha>`
for one commit. `data.file.hunks[].rows[]` has `oldLine` and `newLine`. Use
these numbers; never count lines yourself.

## 2. Write the steps

A view is one of:

- `{{"kind": "diff", "path": P, "side": "new", "startLine": A, "endLine": B}}`:
  rows of the diff. `side` is "new" (added or kept lines) or "old" (removed
  lines). Every line from A to B must be in the diff on that side. Removed
  lines next to the range are shown too. Leave out side, startLine and endLine
  to show the whole file diff.
- `{{"kind": "source", "path": P, "rev": "head", "startLine": A, "endLine": B}}`:
  lines of any file, also unchanged ones (for example a caller). "head" is the
  new side (the worktree in uncommitted and branch scope), "base" the old side.
- `null`: Markdown only, for an introduction or a summary.

A step shows at most 400 lines. A walkthrough has at most 100 steps. Write the
steps in reading order. Explain why, not only what: the reviewer sees the code
next to your text.

## 3. Create it

Set the values at the top, then run it as is.

```sh
python3 - <<'PY'
NAME = "<your agent name>"
SCOPE = {{"scope": "uncommitted"}}   # or {{"scope": "branch", "base": "main"}} or {{"scope": "commit", "commit": "<sha>"}}
TITLE = \"\"\"<title>\"\"\"
SUMMARY = \"\"\"<one paragraph: what this change does>\"\"\"
STEPS = [
    {{"title": "<title>", "body": \"\"\"<markdown>\"\"\",
     "view": {{"kind": "diff", "path": "<path>", "side": "new", "startLine": 1, "endLine": 10}}}},
    {{"title": "Summary", "body": \"\"\"<markdown>\"\"\", "view": None}},
]

import json, subprocess, uuid
cw = ["python3", {helper_r}, "--repo", {repo_r}]
def run(*args):
    return json.loads(subprocess.run(cw + list(args) + ["--json"], capture_output=True, text=True).stdout)
flags = ["--scope", SCOPE["scope"]] + sum((["--" + k, v] for k, v in SCOPE.items() if k != "scope"), [])
for _ in range(3):
    state = run("review", *flags)
    if not state["ok"]:
        break
    data = json.dumps({{"commandId": uuid.uuid4().hex, "reviewId": state["data"]["context"]["reviewId"],
                       "expectedRevision": state["data"]["context"]["revision"],
                       "actor": {{"kind": "agent", "id": NAME.lower(), "name": NAME}},
                       **SCOPE, "snapshotId": state["data"]["snapshotId"],
                       "title": TITLE, "summary": SUMMARY, "steps": STEPS}})
    parts = sum((["--data", data[i:i + 30000]] for i in range(0, len(data), 30000)), [])
    state = run("walkthrough-create", *parts)
    if state["ok"] or state["error"]["code"] != "revision_conflict":
        break
if state["ok"]:
    state = {{"ok": True, "walkthroughId": state["data"]["walkthrough"]["id"],
             "steps": [s["id"] for s in state["data"]["walkthrough"]["steps"]]}}
print(json.dumps(state, indent=2))
PY
```

## 4. Show it to the reviewer

```sh
python3 {helper_q} --repo {repo_q} open --walkthrough <walkthrough id> --json
```

This opens a pane right of your terminal. Run it only in a Herdr terminal.

## 5. Answer replies

```sh
python3 {helper_q} --repo {repo_q} walkthrough --walkthrough <walkthrough id> --json
```

`data.walkthrough.steps[].thread` is a normal thread: reply with its `id` as
`skill` describes. `awaitingReply` means the reviewer wants your answer in that thread.
To fix a step's text, `edit` its first message. `walkthroughs` lists all
walkthroughs of this checkout and branch.

Other commands use the same fields as reply (commandId, reviewId,
expectedRevision, actor):

- `walkthrough-append`: `walkthroughId`, `snapshotId` (from a fresh review of
  the same comparison), `steps`, optional `afterStepId`.
- `walkthrough-update`: `walkthroughId` and `title`, `summary` or `status`
  ("open" or "archived").
- `walkthrough-delete`: `walkthroughId`. Deletes all steps and replies.

## Errors

- `invalid_request` with `details.step`: that step (0-based) is wrong, for
  example a line that is not in the diff. Nothing was written.
- `snapshot_conflict`: the code changed after you read it. Read the diff
  again and check the line numbers before you retry.
- `revision_conflict`: another write came first. The recipe tries three times.
- `storage_error`: stop and tell the reviewer. Do not delete or recreate the store.
"""


def hello():
    """What a Review on another machine checks before it uses this copy. No repository, no store access."""
    return {"helper": os.path.abspath(__file__), "helperVersion": HELPER_VERSION,
            "capabilities": list(CAPABILITIES),
            "python": ".".join(str(part) for part in sys.version_info[:3]), "platform": sys.platform,
            "git": shutil.which("git"), "home": str(Path.home()), "storePath": str(default_store())}


def skill_text(repo, topic="review"):
    helper = os.path.abspath(__file__)
    shown = os.path.abspath(repo) if repo else "<checkout>"
    quote = lambda value: "'" + value.replace("'", "'\\\\''") + "'"
    return (WALKTHROUGH_SKILL if topic == "walkthrough" else SKILL).format(helper=helper, repo=shown, helper_q=quote(helper), repo_q=quote(shown),
                        helper_r=repr(helper), repo_r=repr(shown))


def open_review(repo, scope="uncommitted", base=None, commit=None, walkthrough=None, store_path=None):
    """Open Review (or one walkthrough) as a pane right of this terminal's pane, for its folder."""
    terminal = os.environ.get("HC_TERMINAL_ID")
    if not terminal:
        fail("not_in_herdr", "Run open in a Herdr terminal (HC_TERMINAL_ID is not set)")
    if "/" in terminal:
        fail("invalid_request", "open supports local Herdr terminals only")
    folder = os.path.abspath(repo or os.getcwd())
    if scope not in ("commit", "pr") and commit is not None:
        fail("invalid_request", "--commit requires commit scope")
    review_props = {"framed": True, "folder": folder, "scope": scope}
    if walkthrough is not None:
        if base is not None or commit is not None:
            fail("invalid_request", "open --walkthrough takes no --base or --commit; the walkthrough pins its comparison")
        # It must exist in this checkout's review before the layout changes.
        git = Git(folder)
        store = Store(git, store_path)
        try:
            comparison = json.loads(store.walkthrough_row(walkthrough)["comparison"])
        finally:
            store.close()
        folder = str(git.root)
        review_props = {"framed": True, "folder": folder, "walkthrough": walkthrough}
    elif scope == "commit":
        if base is not None:
            fail("invalid_request", "commit scope uses --commit, not --base")
        # Resolve before opening a pane. A bad ref never changes the layout.
        git = Git(folder)
        commit, _, _ = git.commit_endpoints(commit)
        folder = str(git.root)
        review_props.update(folder=folder, commit=commit)
    elif scope == "pr":
        if base is not None or commit is not None:
            fail("invalid_request", "open --scope pr discovers the current branch's PR; do not pass --base or --commit")
        folder = str(Git(folder).root)
        review_props.update(folder=folder)
    elif scope == "branch":
        git = Git(folder)
        base, _ = git.endpoints(scope, base)
        folder = str(git.root)
        review_props.update(folder=folder, base=base)

    def herdr(*argv):
        try:
            done = subprocess.run(["herdr-app", *argv], capture_output=True, text=True, timeout=10)
        except FileNotFoundError:
            fail("not_in_herdr", "herdr-app is not on PATH")
        except subprocess.TimeoutExpired:
            fail("herdr_error", f"herdr-app {argv[0]} did not answer")
        try:
            result = json.loads(done.stdout)
        except ValueError:
            fail("herdr_error", (done.stderr or done.stdout).strip() or f"herdr-app {argv[0]} failed")
        if done.returncode or (isinstance(result, dict) and result.get("error")):
            fail("herdr_error", str(result.get("error") if isinstance(result, dict) else result))
        return result

    panes = [t.get("pane") for space in herdr("ls").get("spaces", []) for tab in space.get("tabs", [])
             for t in tab.get("terminals", []) if t.get("terminal") == terminal]
    if not panes or not panes[0]:
        fail("herdr_error", f"terminal {terminal} has no pane; show it first")
    split = {"direction": "right", "pane": panes[0], "mod": "Codewalker", "props": review_props}
    opened = herdr("do", "pane.split", json.dumps(split))
    if walkthrough is not None:
        return {"pane": opened.get("pane"), "folder": folder, "walkthroughId": walkthrough, "scope": comparison["scope"]}
    return {"pane": opened.get("pane"), "folder": folder, "scope": scope,
            **({"commitOid": commit} if scope == "commit" else {})}


class Parser(argparse.ArgumentParser):
    def error(self, message):
        fail("invalid_request", message)


def parse_args(argv):
    parser = Parser(description=__doc__)
    parser.add_argument("--repo")
    parser.add_argument("--store")
    parser.add_argument("command", choices=["hello", "skill", "open", "context", "review", "list", "create", "reply", "resolve", "edit", "delete", "deliver", "file-view", "export", "draft-save", "draft-delete", "history-clear",
                                            "walkthroughs", "walkthrough", "walkthrough-create", "walkthrough-append", "walkthrough-update", "walkthrough-delete"])
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--scope", default="uncommitted", choices=["uncommitted", "branch", "commit", "pr"])
    parser.add_argument("--base")
    parser.add_argument("--commit")
    parser.add_argument("--file")
    parser.add_argument("--select-first", action="store_true")
    parser.add_argument("--walkthrough")
    parser.add_argument("--topic", default="review", choices=["review", "walkthrough"])
    # Repeated --data entries are joined: one argument can be limited to 128 KiB.
    parser.add_argument("--data", action="append")
    return parser.parse_args(argv)


def execute(args):
    if args.repo is None:
        fail("invalid_request", "--repo is required")
    git = Git(args.repo)
    store = Store(git, args.store)
    try:
        if args.command == "context":
            return store.context()
        if args.command == "walkthroughs":
            return store.read_walkthroughs()
        if args.command == "walkthrough":
            if not args.walkthrough:
                fail("invalid_request", "walkthrough requires --walkthrough <id>")
            return store.read_walkthrough(args.walkthrough)
        if args.command in ("review", "list", "export"):
            if args.select_first and (args.command != "review" or args.file is not None):
                fail("invalid_request", "--select-first is only for review without --file")
            source, contents = git.snapshot(args.scope, args.base, args.file if args.command == "review" else None,
                                            args.select_first, args.commit)
            listing = store.listing(source, contents)
            if args.command == "review":
                context = store.context()
                context["revision"] = listing["revision"]
                # Internal old blob/mode fields are not protocol surface.
                files = [dict({k: v for k, v in entry.items() if k not in ("oldMode", "newMode", "oldOid", "newOid")},
                              viewed=bool(entry["diffFingerprint"] and listing["viewedFiles"].get(entry["path"]) == entry["diffFingerprint"]))
                         for entry in source["files"]]
                return dict(source, context=context, files=files, threads=listing["threads"], drafts=listing["drafts"],
                            walkthroughs=listing["walkthroughs"])
            if args.command == "export":
                listing["text"] = export_text(listing)
            return listing
        if args.data is None:
            fail("invalid_request", "mutation requires --data JSON")
        encoded = "".join(args.data)
        if len(encoded.encode("utf-8")) > 1_048_576:
            fail("too_large", "mutation JSON exceeds 1 MiB")
        try:
            data = json.loads(encoded, parse_constant=lambda value: fail("invalid_request", f"non-JSON constant: {value}"))
        except (ValueError, RecursionError):
            fail("invalid_request", "--data is not valid JSON")
        return store.mutate(args.command, data)
    finally:
        store.close()


def main(argv=None):
    try:
        args = parse_args(sys.argv[1:] if argv is None else argv)
        if args.command == "skill":
            # Plain Markdown for an agent to read, not a JSON result.
            print(skill_text(args.repo, args.topic), end="")
            return 0
        if sys.version_info < (3, 10):
            fail("unsupported_python", "Codewalker needs Python 3.10 or newer", python=sys.version.split()[0])
        data = (hello() if args.command == "hello"
                else open_review(args.repo, args.scope, args.base, args.commit, args.walkthrough, args.store)
                if args.command == "open" else execute(args))
        result = {"ok": True, "protocolVersion": VERSION, "data": data}
        encoded = json.dumps(result, ensure_ascii=False)
        if len(encoded.encode("utf-8")) > MAX_OUTPUT:
            fail("too_large", "review result exceeds 7 MiB; select a smaller changeset")
        print(encoded)
        return 0
    except Failure as exc:
        error = {"code": exc.code, "message": exc.message}
        if exc.details:
            error["details"] = exc.details
    except (sqlite3.Error, OSError) as exc:
        error = {"code": "storage_error", "message": str(exc)}
    except (UnicodeError, ValueError, TypeError, RecursionError) as exc:
        error = {"code": "invalid_request", "message": f"invalid input: {exc}"}
    print(json.dumps({"ok": False, "protocolVersion": VERSION, "error": error}, ensure_ascii=False))
    return 1


if __name__ == "__main__":
    sys.exit(main())

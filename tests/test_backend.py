"""Behavior contracts at the real CLI/Git/SQLite boundary; no mocked persistence."""
import concurrent.futures
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

HELPER = Path(__file__).resolve().parents[1] / "backend" / "codewalker.py"
HUMAN = {"kind": "human", "id": "user", "name": "You"}
AGENT = {"kind": "agent", "id": "pi", "name": "Pi"}


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "repo"
        self.root.mkdir()
        self.store = Path(self.temp.name) / "state" / "reviews.sqlite3"
        self.git("init", "-b", "main")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Test")
        (self.root / "a.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
        (self.root / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-qm", "base")

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.root, text=True, capture_output=True, check=True).stdout.strip()

    def cli(self, command, *args, data=None, error=None):
        argv = [sys.executable, str(HELPER), "--repo", str(self.root), "--store", str(self.store), command, "--json", *args]
        if data is not None:
            data.setdefault("reviewId", self.cli("context")["reviewId"])
            argv.extend(["--data", json.dumps(data)])
        process = subprocess.run(argv, capture_output=True, text=True, timeout=30)
        response = json.loads(process.stdout)
        if error:
            self.assertEqual(process.returncode, 1, process.stderr)
            self.assertFalse(response["ok"])
            self.assertEqual(response["error"]["code"], error, response)
            return response
        self.assertEqual(process.returncode, 0, (response, process.stderr))
        self.assertTrue(response["ok"])
        self.assertEqual(response["protocolVersion"], 1)
        return response["data"]

    def changed(self):
        (self.root / "a.txt").write_text("one\nTWO\nthree\n", encoding="utf-8")
        return self.cli("review", "--file", "a.txt")

    def create(self, review=None, command="create1", actor=HUMAN, body="Why?\n\nKeep the blank line."):
        review = review or self.changed()
        data = {"commandId": command, "expectedRevision": review["context"]["revision"], "actor": actor,
                "scope": review["scope"], "anchor": {"path": "a.txt", "side": "new", "startLine": 2,
                "endLine": 2, "snapshotId": review["snapshotId"]}, "body": body}
        if review["scope"] in ("commit", "pr"):
            data["commit"] = review["commitOid"]
        if review["scope"] == "pr":
            data["base"] = review["baseRef"]
        return self.cli("create", data=data), data

    def mark_file(self, review, path="a.txt", viewed=True, command="view1"):
        file = next(f for f in review["files"] if f["path"] == path)
        data = {"commandId": command, "expectedRevision": review["context"]["revision"], "actor": HUMAN,
                "scope": review["scope"], "base": review["baseRef"], "path": path,
                "diffFingerprint": file["diffFingerprint"], "viewed": viewed}
        if review["scope"] in ("commit", "pr"):
            if review["scope"] == "commit":
                data.pop("base")
            data["commit"] = review["commitOid"]
        return self.cli("file-view", data=data), data

    def test_pr_compares_merge_base_to_pinned_head_and_reuses_review_mutations(self):
        ancestor = self.git("rev-parse", "HEAD")
        self.git("switch", "-c", "feature")
        self.changed()
        self.git("add", ".")
        self.git("commit", "-qm", "first PR commit")
        (self.root / "feature.txt").write_text("second PR commit\n")
        self.git("add", ".")
        self.git("commit", "-qm", "second PR commit")
        head = self.git("rev-parse", "HEAD")
        self.git("switch", "main")
        (self.root / "base-only.txt").write_text("not a PR change\n")
        self.git("add", ".")
        self.git("commit", "-qm", "advance base")
        base = self.git("rev-parse", "HEAD")
        # Neither checkout HEAD, its index nor its working files are the PR head.
        (self.root / "a.txt").write_text("unrelated staged text\n")
        self.git("add", "a.txt")
        (self.root / "a.txt").write_text("unrelated unstaged text\n")
        (self.root / "untracked.txt").write_text("not in the PR\n")
        before = self.git("status", "--porcelain=v1"), (self.root / ".git/index").read_bytes()
        args = ("--scope", "pr", "--base", base, "--commit", head)
        review = self.cli("review", *args, "--file", "a.txt")
        self.assertEqual(review["baseOid"], ancestor)
        self.assertEqual(review["commitOid"], head)
        self.assertEqual({f["path"] for f in review["files"]}, {"a.txt", "feature.txt"})
        rows = review["file"]["hunks"][0]["rows"]
        self.assertIn("TWO", [r["text"] for r in rows])
        self.assertNotIn("unrelated unstaged text", [r["text"] for r in rows])
        self.mark_file(review)
        review = self.cli("review", *args, "--file", "a.txt")
        self.assertTrue(next(f for f in review["files"] if f["path"] == "a.txt")["viewed"])
        made, _ = self.create(review)
        anchor = made["thread"]["anchor"]
        self.assertEqual((anchor["scope"], anchor["baseRef"], anchor["commitOid"]), ("pr", base, head))
        reply = self.cli("reply", data={"commandId":"pr-reply", "expectedRevision":made["revision"],
            "actor":AGENT, "threadId":made["thread"]["id"], "body":"Fixed in a follow-up."})
        self.assertEqual(reply["thread"]["anchor"]["validity"], "current")
        again = self.cli("review", *args)
        self.assertEqual(again["snapshotId"], review["snapshotId"])
        self.assertEqual(again["threads"][0]["anchor"]["validity"], "current")
        self.assertFalse(any(f["viewed"] for f in self.cli("review", "--scope", "commit", "--commit", head)["files"]))
        # Same head and merge base, different pinned base: a separate PR checklist.
        other = self.cli("review", "--scope", "pr", "--base", ancestor, "--commit", head)
        self.assertFalse(any(f["viewed"] for f in other["files"]))
        self.assertEqual((self.git("status", "--porcelain=v1"), (self.root / ".git/index").read_bytes()), before)

    def test_pr_requires_full_available_commits_and_common_history(self):
        head = self.git("rev-parse", "HEAD")
        self.cli("review", "--scope", "pr", "--base", "main", "--commit", head, error="invalid_request")
        self.cli("review", "--scope", "pr", "--base", head, "--commit", "f" * 40, error="pr_objects_missing")
        self.cli("review", "--scope", "pr", "--base", head, error="invalid_request")
        tree = self.git("rev-parse", "HEAD^{tree}")
        unrelated = self.git("commit-tree", tree, "-m", "unrelated root")
        self.cli("review", "--scope", "pr", "--base", head, "--commit", unrelated, error="invalid_base")
        self.assertEqual(self.cli("context")["revision"], 0)

    def test_viewed_marks_survive_restart_unrelated_changes_and_explicit_retries(self):
        review = self.changed()
        self.assertFalse(review["files"][0]["viewed"], "reading a file never marks it")
        marked, data = self.mark_file(review)
        self.assertEqual(self.cli("file-view", data=data), marked)
        self.assertEqual(self.cli("context")["revision"], 1)
        (self.root / "other.txt").write_text("unrelated\n")
        again = self.cli("review", "--file", "a.txt")
        self.assertNotEqual(again["snapshotId"], review["snapshotId"])
        self.assertTrue(next(f for f in again["files"] if f["path"] == "a.txt")["viewed"])
        self.assertEqual(again["file"]["diffFingerprint"], review["file"]["diffFingerprint"])
        self.create(again)
        again = self.cli("review", "--file", "a.txt")
        self.assertTrue(again["files"][0]["viewed"], "a discussion is not a code change")
        before = self.store.read_bytes()
        self.cli("review")
        self.assertEqual(self.store.read_bytes(), before, "viewed reads must not wake the store watcher")
        unmarked, data = self.mark_file(again, viewed=False, command="unview")
        self.assertEqual(self.cli("file-view", data=data), unmarked)
        self.assertFalse(self.cli("review")["files"][0]["viewed"])

    def test_viewed_fingerprints_follow_both_sides_not_commit_ids_or_other_comparisons(self):
        original_base = self.git("rev-parse", "HEAD")
        self.git("switch", "-c", "feature")
        self.changed()
        branch = self.cli("review", "--scope", "branch", "--base", "main")
        self.mark_file(branch)
        self.assertFalse(self.cli("review")["files"][0]["viewed"], "uncommitted has its own checklist")
        self.git("branch", "other-base", "main")
        self.assertFalse(self.cli("review", "--scope", "branch", "--base", "other-base")["files"][0]["viewed"])
        # The new side stays fixed while HEAD and then the base move.
        self.git("add", "a.txt")
        self.git("commit", "-qm", "feature")
        self.assertTrue(self.cli("review", "--scope", "branch", "--base", "main")["files"][0]["viewed"])
        self.git("switch", "main")
        (self.root / "other.txt").write_text("unrelated\n")
        self.git("add", ".")
        self.git("commit", "-qm", "unrelated base change")
        self.git("switch", "feature")
        self.git("merge", "--no-edit", "main")
        same = self.cli("review", "--scope", "branch", "--base", "main")
        self.assertTrue(same["files"][0]["viewed"], "moving the base elsewhere preserves this file's mark")
        # A new base has the same working file, but a different removed side.
        self.git("branch", "-f", "main", "feature")
        (self.root / "a.txt").write_text("one\nthree\n")
        fresh = self.cli("review", "--scope", "branch", "--base", "main")
        self.mark_file(fresh, command="view-new-diff")
        self.git("branch", "-f", "main", original_base)
        changed_base = self.cli("review", "--scope", "branch", "--base", "main")
        self.assertFalse(changed_base["files"][0]["viewed"], "changing only the old side invalidates a mark")

    def test_viewed_rejects_stale_file_but_not_unrelated_file_edits(self):
        review = self.changed()
        (self.root / "other.txt").write_text("unrelated\n")
        _, data = self.mark_file(review)
        (self.root / "a.txt").write_text("one\nnewer\nthree\n")
        fresh = self.cli("review")
        self.assertFalse(fresh["files"][0]["viewed"])
        data.update(commandId="stale-view", expectedRevision=fresh["context"]["revision"])
        self.cli("file-view", data=data, error="snapshot_conflict")
        self.assertEqual(self.cli("context")["revision"], 1, "a refused mark leaves no receipt or revision")
        self.mark_file(fresh, command="new-view")
        # File modes are part of the review even when the bytes do not change.
        self.git("config", "core.filemode", "true")
        (self.root / "a.txt").chmod(0o755)
        self.assertFalse(self.cli("review")["files"][0]["viewed"])
        self.git("switch", "-c", "other-review")
        self.assertFalse(self.cli("review")["files"][0]["viewed"], "a different branch starts unviewed")

    def test_viewed_handles_binary_deleted_renamed_and_unfingerprinted_files(self):
        for name, raw in (("binary", b"old\0"), ("deleted", b"delete me\n"), ("rename", b"rename me\n")):
            (self.root / name).write_bytes(raw)
        self.git("add", ".")
        self.git("commit", "-qm", "fixtures")
        (self.root / "binary").write_bytes(b"new\0")
        (self.root / "deleted").unlink()
        self.git("mv", "rename", "renamed")
        (self.root / "untracked").write_text("new\n")
        (self.root / "huge").write_bytes(b"x" * 2_000_001)
        (self.root / "link").symlink_to("a.txt")
        for path in ("binary", "deleted", "renamed", "untracked"):
            result = self.cli("review")
            self.mark_file(result, path, command="view-" + path)
        files = {f["path"]: f for f in self.cli("review")["files"]}
        self.assertTrue(all(files[p]["viewed"] for p in ("binary", "deleted", "renamed", "untracked")))
        for path in ("huge", "link"):
            self.assertIsNone(files[path]["diffFingerprint"])
            self.assertFalse(files[path]["viewed"])
            self.cli("file-view", data={"commandId": path, "expectedRevision": 4, "actor": HUMAN,
                     "path": path, "diffFingerprint": "invented", "viewed": True}, error="invalid_request")

    def test_v4_store_adds_viewed_marks_without_changing_existing_discussions(self):
        created, _ = self.create()
        with sqlite3.connect(self.store) as db:
            db.execute("DROP TABLE file_views")
            db.execute("PRAGMA user_version=4")
        review = self.cli("review")
        self.assertEqual(review["threads"][0]["id"], created["thread"]["id"])
        self.assertFalse(review["files"][0]["viewed"])
        self.mark_file(review)
        self.assertTrue(self.cli("review")["files"][0]["viewed"])

    def test_commit_review_reads_immutable_blobs_not_current_worktree_or_head(self):
        self.changed()
        self.git("add", "a.txt")
        self.git("commit", "-qm", "selected change")
        commit = self.git("rev-parse", "HEAD")
        first = self.cli("review", "--scope", "commit", "--commit", commit[:12], "--file", "a.txt")
        self.assertEqual(first["commitOid"], commit)
        self.assertEqual(first["parentCount"], 1)
        self.assertEqual(first["baseOid"], self.git("rev-parse", "HEAD^"))
        (self.root / "a.txt").write_text("later committed content\n")
        self.git("add", ".")
        self.git("commit", "-qm", "later change")
        (self.root / "a.txt").write_text("staged content\n")
        self.git("add", "a.txt")
        (self.root / "a.txt").unlink()
        (self.root / "a.txt").symlink_to("/does/not/exist")
        (self.root / "untracked").write_text("not in commit\n")
        index = (self.root / ".git/index").read_bytes()
        review = self.cli("review", "--scope", "commit", "--commit", commit, "--file", "a.txt")
        self.assertEqual(review["snapshotId"], first["snapshotId"])
        self.assertEqual(review["file"], first["file"])
        self.assertEqual([f["path"] for f in review["files"]], ["a.txt"])
        self.assertEqual(review["headOid"], commit)
        self.assertNotEqual(review["context"]["headOid"], commit)
        self.assertEqual((self.root / ".git/index").read_bytes(), index)
        created, _ = self.create(review)
        self.assertEqual(created["thread"]["anchor"]["snippet"], "TWO")
        self.assertEqual(created["thread"]["anchor"]["commitOid"], commit)
        self.assertEqual(created["thread"]["anchor"]["validity"], "current")
        replied = self.cli("reply", data={"commandId": "historical-reply", "expectedRevision": 1,
                           "actor": AGENT, "threadId": created["thread"]["id"], "body": "Historical reply"})
        self.assertEqual(replied["thread"]["anchor"]["validity"], "current")
        self.assertEqual(self.cli("list")["threads"][0]["anchor"]["validity"], "stale")

    def test_commit_marks_and_anchors_are_scoped_to_the_exact_commit_not_its_diff(self):
        self.changed()
        self.git("add", ".")
        tree = self.git("write-tree")
        parent = self.git("rev-parse", "HEAD")
        a = self.git("commit-tree", tree, "-p", parent, "-m", "A")
        b = self.git("commit-tree", tree, "-p", parent, "-m", "B")
        self.git("tag", "review-a", a)
        review = self.cli("review", "--scope", "commit", "--commit", "review-a", "--file", "a.txt")
        self.mark_file(review)
        self.create(self.cli("review", "--scope", "commit", "--commit", a, "--file", "a.txt"))
        another = self.cli("review", "--scope", "commit", "--commit", b, "--file", "a.txt")
        self.assertEqual(another["file"]["diffFingerprint"], review["file"]["diffFingerprint"])
        self.assertNotEqual(another["snapshotId"], review["snapshotId"])
        self.assertFalse(another["files"][0]["viewed"])
        self.assertEqual(another["threads"][0]["anchor"]["validity"], "stale")
        again = self.cli("review", "--scope", "commit", "--commit", a)
        self.assertTrue(again["files"][0]["viewed"])
        self.assertEqual(again["threads"][0]["anchor"]["validity"], "current")

    def test_root_empty_and_merge_commits_use_empty_tree_or_first_parent(self):
        root = self.git("rev-parse", "HEAD")
        result = self.cli("review", "--scope", "commit", "--commit", root, "--file", "a.txt")
        self.assertEqual(result["parentCount"], 0)
        self.assertEqual(result["baseRef"], "(empty tree)")
        self.assertEqual({f["status"] for f in result["files"]}, {"added"})
        self.assertEqual([r["text"] for r in result["file"]["hunks"][0]["rows"]], ["one", "two", "three"])
        self.git("commit", "--allow-empty", "-qm", "empty")
        empty = self.cli("review", "--scope", "commit", "--commit", "HEAD", "--select-first")
        self.assertEqual(empty["files"], [])
        self.assertIsNone(empty["file"])
        self.git("switch", "-c", "side", root)
        (self.root / "side.txt").write_text("side\n")
        self.git("add", ".")
        self.git("commit", "-qm", "side")
        self.git("switch", "main")
        (self.root / "main.txt").write_text("main\n")
        self.git("add", ".")
        self.git("commit", "-qm", "main")
        parent = self.git("rev-parse", "HEAD")
        self.git("merge", "--no-edit", "side")
        merge = self.cli("review", "--scope", "commit", "--commit", "HEAD")
        self.assertEqual(merge["parentCount"], 2)
        self.assertEqual(merge["baseOid"], parent)
        self.assertEqual([f["path"] for f in merge["files"]], ["side.txt"])

    def test_commit_rejects_invalid_refs_and_missing_shallow_parents(self):
        for ref in ("no-such-commit", "HEAD:a.txt", "HEAD..HEAD", "--help"):
            with self.subTest(ref=ref):
                self.cli("review", "--scope", "commit", "--commit=" + ref, error="invalid_commit")
        self.cli("review", "--scope", "commit", error="invalid_request")
        self.cli("review", "--commit", "HEAD", error="invalid_request")
        self.cli("review", "--scope", "commit", "--commit", "HEAD", "--base", "main", error="invalid_request")
        self.git("commit", "--allow-empty", "-qm", "shallow tip")
        shallow = Path(self.temp.name) / "shallow"
        self.git("clone", "--depth=1", self.root.as_uri(), str(shallow))
        self.root = shallow
        response = self.cli("review", "--scope", "commit", "--commit", "HEAD", error="invalid_commit")
        self.assertIn("first parent is unavailable", response["error"]["message"])

    def test_open_commit_passes_pinned_props_and_returns_the_created_pane(self):
        # Fake only the app boundary. This test must never alter the user's window.
        tool_dir = Path(self.temp.name) / "bin"
        tool_dir.mkdir()
        calls = Path(self.temp.name) / "calls.jsonl"
        app = tool_dir / "herdr-app"
        app.write_text(f"#!{sys.executable}\n" + "import json,sys\n"
            + f"with open({str(calls)!r}, 'a') as f: f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            + "print(json.dumps({'spaces':[{'tabs':[{'terminals':[{'terminal':'t-source','pane':'p-source'}]}]}]} if sys.argv[1] == 'ls' else {'pane':'p-created'}))\n")
        app.chmod(0o755)
        env = {**os.environ, "PATH": str(tool_dir) + os.pathsep + os.environ.get("PATH", ""), "HC_TERMINAL_ID": "t-source"}
        argv = [sys.executable, str(HELPER), "open", "--repo", str(self.root), "--scope", "commit", "--commit", "HEAD"]
        result = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result = json.loads(result.stdout)["data"]
        commit = self.git("rev-parse", "HEAD")
        self.assertEqual(result["pane"], "p-created")
        self.assertEqual(result["commitOid"], commit)
        sent = [json.loads(line) for line in calls.read_text().splitlines()]
        split = json.loads(sent[1][2])
        self.assertEqual(split["pane"], "p-source")
        self.assertEqual(split["props"]["commit"], commit)
        self.assertEqual(split["props"]["scope"], "commit")
        self.assertEqual(split["props"]["folder"], str(self.root.resolve()))
        calls.write_text("")
        argv[-1] = "missing"
        failed = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(json.loads(failed.stdout)["error"]["code"], "invalid_commit")
        self.assertEqual(calls.read_text(), "", "a bad commit cannot open a pane")

    def test_uncommitted_coordinates_untracked_and_index_are_read_only(self):
        (self.root / "a.txt").write_text("one\nstaged\nthree\n", encoding="utf-8")
        self.git("add", "a.txt")
        (self.root / "a.txt").write_text("one\nnet worktree\nthree\n", encoding="utf-8")
        (self.root / "new [x].txt").write_text("new\n", encoding="utf-8")
        (self.root / "ignored.txt").write_text("secret\n", encoding="utf-8")
        index = (self.root / ".git/index").read_bytes()
        result = self.cli("review", "--file", "a.txt")
        self.assertEqual({f["path"] for f in result["files"]}, {"a.txt", "new [x].txt"})
        rows = result["file"]["hunks"][0]["rows"]
        self.assertIn({"kind": "delete", "oldLine": 2, "newLine": None, "text": "two"}, rows)
        self.assertIn({"kind": "add", "oldLine": None, "newLine": 2, "text": "net worktree"}, rows)
        self.assertEqual((self.root / ".git/index").read_bytes(), index)
        untracked = self.cli("review", "--file", "new [x].txt")
        self.assertEqual(untracked["file"]["hunks"][0]["rows"], [{"kind": "add", "oldLine": None, "newLine": 1, "text": "new"}])

    def test_branch_includes_commits_and_worktree_using_merge_base(self):
        base = self.git("rev-parse", "HEAD")
        self.git("switch", "-c", "feature")
        (self.root / "a.txt").write_text("one\ncommitted\nthree\n", encoding="utf-8")
        self.git("add", "a.txt")
        self.git("commit", "-qm", "feature")
        (self.root / "working.txt").write_text("uncommitted\n", encoding="utf-8")
        result = self.cli("review", "--scope", "branch", "--base", "main")
        self.assertEqual(result["baseOid"], base)
        self.assertEqual({f["path"] for f in result["files"]}, {"a.txt", "working.txt"})
        self.cli("review", "--scope", "branch", "--base", "missing", error="invalid_base")

    def test_two_way_durable_threads_resolution_exact_body_and_export(self):
        created, _ = self.create()
        thread_id = created["thread"]["id"]
        message_id = created["thread"]["messages"][0]["id"]
        reply_body = "Because:\n\n  preserve indentation\n"
        replied = self.cli("reply", data={"commandId": "reply1", "expectedRevision": 1, "actor": AGENT,
                                          "threadId": thread_id, "body": reply_body})
        self.assertEqual(replied["thread"]["messages"][1]["actor"], AGENT)
        self.assertEqual(replied["thread"]["messages"][1]["body"], reply_body)
        resolved = self.cli("resolve", data={"commandId": "resolve1", "expectedRevision": 2, "actor": HUMAN,
                                             "threadId": thread_id, "resolved": True})
        self.assertEqual(resolved["thread"]["status"], "resolved")
        listed = self.cli("list")
        self.assertEqual(listed["threads"][0]["messages"][0]["id"], message_id)
        self.assertEqual(listed["threads"][0]["anchor"]["validity"], "current")
        self.assertEqual(listed["revision"], 3)
        exported = self.cli("export")
        self.assertIn("Why?\n\nKeep the blank line.", exported["text"])
        self.assertIn(reply_body, exported["text"])
        self.assertEqual(self.cli("list")["revision"], 3)
        self.assertFalse(self.store.is_relative_to(self.root))
        self.assertEqual(os.stat(self.store).st_mode & 0o777, 0o600)

    def test_retry_dedup_and_concurrent_revision_conflict(self):
        created, data = self.create()
        self.assertEqual(self.cli("create", data=data), created)
        self.cli("create", data=dict(data, body="different"), error="idempotency_conflict")
        thread_id = created["thread"]["id"]
        commands = [{"commandId": f"reply{i}", "reviewId": created["reviewId"], "expectedRevision": 1, "actor": actor,
                     "threadId": thread_id, "body": f"message {i}"} for i, actor in enumerate((HUMAN, AGENT))]
        def invoke(payload):
            argv = [sys.executable, str(HELPER), "--repo", str(self.root), "--store", str(self.store),
                    "reply", "--data", json.dumps(payload)]
            return json.loads(subprocess.run(argv, text=True, capture_output=True, timeout=30).stdout)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(invoke, commands))
        self.assertEqual(sum(o["ok"] for o in outcomes), 1, outcomes)
        loser = next(i for i, o in enumerate(outcomes) if not o["ok"])
        self.assertEqual(outcomes[loser]["error"]["code"], "revision_conflict")
        self.cli("reply", data=dict(commands[loser], commandId="reconciled", expectedRevision=2))
        listed = self.cli("list")
        self.assertEqual(listed["revision"], 3)
        self.assertEqual(len(listed["threads"][0]["messages"]), 3)

    def test_anchor_staleness_and_snapshot_conflict_never_retarget(self):
        review = self.changed()
        created, data = self.create(review)
        (self.root / "a.txt").write_text("inserted\none\nTWO\nthree\n", encoding="utf-8")
        self.cli("create", data=dict(data, commandId="old-snapshot", expectedRevision=1), error="snapshot_conflict")
        thread = self.cli("list")["threads"][0]
        self.assertEqual(thread["anchor"]["startLine"], 2)
        self.assertEqual(thread["anchor"]["snippet"], "TWO")
        self.assertEqual(thread["anchor"]["validity"], "stale")
        (self.root / "a.txt").unlink()
        self.assertEqual(self.cli("list")["threads"][0]["id"], created["thread"]["id"])

    def test_old_side_and_deletion_notes(self):
        (self.root / "a.txt").unlink()
        review = self.cli("review", "--file", "a.txt")
        data = {"commandId": "old", "expectedRevision": 0, "actor": AGENT, "scope": "uncommitted",
                "anchor": {"path": "a.txt", "side": "old", "startLine": 1, "endLine": 3, "snapshotId": review["snapshotId"]}, "body": "This deletion changes the contract."}
        result = self.cli("create", data=data)
        self.assertEqual(result["thread"]["anchor"]["snippet"], "one\ntwo\nthree")
        self.assertEqual(self.cli("list")["threads"][0]["anchor"]["validity"], "current")

    def test_reject_outside_paths_and_symlinks_and_preserve_binary_metadata(self):
        self.changed()
        for path in ("../outside", "/tmp/outside", ".git/config", "ignored.txt", "./a.txt"):
            self.cli("review", "--file", path, error="invalid_request" if path != "ignored.txt" else "not_found")
        outside = Path(self.temp.name) / "outside.txt"
        outside.write_text("private\n", encoding="utf-8")
        (self.root / "link.txt").symlink_to(outside)
        (self.root / "binary.dat").write_bytes(b"binary\0file")
        result = self.cli("review", "--file", "link.txt")
        self.assertTrue(result["file"]["unsupported"])
        self.assertEqual(result["file"]["hunks"], [])
        result = self.cli("review", "--file", "binary.dat")
        self.assertTrue(result["file"]["binary"])
        self.assertEqual(result["file"]["hunks"], [])

    def test_drafts_actor_ownership_and_restart(self):
        self.cli("draft-save", data={"commandId": "draft1", "expectedRevision": 0, "actor": HUMAN,
                                     "draftId": "composer", "body": "unfinished\n\n", "target": {"threadId": "future"}})
        listed = self.cli("list")
        self.assertEqual(listed["drafts"][0]["body"], "unfinished\n\n")
        self.cli("draft-delete", data={"commandId": "steal", "expectedRevision": 1, "actor": AGENT,
                                       "draftId": "composer"}, error="invalid_request")
        self.cli("draft-delete", data={"commandId": "delete1", "expectedRevision": 1, "actor": HUMAN,
                                       "draftId": "composer"})
        self.assertEqual(self.cli("list")["drafts"], [])

    def test_rename_source_and_literal_pathspec(self):
        (self.root / "a.txt").rename(self.root / "renamed [x].txt")
        self.git("add", "-A")
        (self.root / "renamed [x].txt").write_text("one\nTWO\nthree\n", encoding="utf-8")
        review = self.cli("review", "--file", "renamed [x].txt")
        entry = review["files"][0]
        self.assertEqual(entry["status"], "renamed")
        self.assertEqual(entry["oldPath"], "a.txt")
        rows = review["file"]["hunks"][0]["rows"]
        self.assertTrue(any(r["text"] == "two" and r["kind"] == "delete" for r in rows))

    def test_unborn_and_detached_head_persist_explicit_context(self):
        self.git("switch", "--orphan", "unborn")
        (self.root / "new.txt").write_text("new\n", encoding="utf-8")
        result = self.cli("review", "--file", "new.txt")
        self.assertIsNone(result["context"]["headOid"])
        self.assertEqual(result["file"]["hunks"][0]["newLines"], 1)
        self.git("switch", "--detach", "main")
        self.assertIsNone(self.cli("context")["branch"])
        created, _ = self.create()
        self.assertEqual(self.cli("list")["threads"][0]["id"], created["thread"]["id"])

    @unittest.skipUnless(sys.platform == "darwin", "worst-case argv uses macOS budget; Linux caps one argument at 128 KiB")
    def test_draft_can_retain_max_escaped_post_receipt_and_retry_after_restart(self):
        # Every allowed U+0001 byte needs six JSON bytes. Two exact copies live in
        # draft.body and target.pendingPost.payload.body: ~786 KiB on the wire.
        unit = '"\\\n\x01日本語'
        unit_bytes = len(unit.encode("utf-8"))
        mixed = unit * (65_536 // unit_bytes) + "x" * (65_536 % unit_bytes)
        for index, body in enumerate(("\x01" * 65_536, mixed)):
            with self.subTest(body_kind="worst-case-controls" if index == 0 else "mixed-escaped-unicode"):
                self.assertEqual(len(body.encode("utf-8")), 65_536)
                pending = {"commandId": f"post-after-draft-{index}", "context": {"reviewId": self.cli("context")["reviewId"]},
                           "operation": "reply", "payload": {"threadId": "pending-thread", "body": body}}
                request = {"commandId": f"receipt-draft-{index}", "expectedRevision": index, "actor": HUMAN,
                           "draftId": f"pending-post-{index}", "body": body, "target": {"pendingPost": pending}}
                result = self.cli("draft-save", data=request)
                self.assertEqual(self.cli("draft-save", data=request), result)
                restored = next(d for d in self.cli("list")["drafts"] if d["id"] == request["draftId"])
                self.assertEqual(restored["target"]["pendingPost"], pending)
                self.assertEqual(restored["body"], body)
                self.assertEqual(result["revision"], index + 1)

    def test_git_clean_filter_is_not_executed_by_read_only_review(self):
        marker = Path(self.temp.name) / "executed"
        driver = Path(self.temp.name) / "driver.sh"
        driver.write_text(f'#!/bin/sh\ntouch "{marker}"\ncat\n', encoding="utf-8")
        driver.chmod(0o755)
        self.git("config", "filter.hostile.clean", str(driver))
        self.git("config", "filter.hostile.process", str(driver))
        self.git("config", "filter.hostile.required", "true")
        (self.root / ".gitattributes").write_text("a.txt filter=hostile\n", encoding="utf-8")
        result = self.changed()
        self.assertTrue(result["file"]["hunks"])
        self.assertFalse(marker.exists(), "Git conversion filter ran during review")

    def test_git_line_coordinates_do_not_split_unicode_separators_or_lone_cr(self):
        (self.root / "unicode.txt").write_text("a\u2028b\rc\nsecond\n", encoding="utf-8")
        review = self.cli("review", "--file", "unicode.txt")
        rows = review["file"]["hunks"][0]["rows"]
        self.assertEqual([(r["newLine"], r["text"]) for r in rows], [(1, "a\u2028b\rc"), (2, "second")])
        result = self.cli("create", data={
            "commandId": "unicode", "expectedRevision": 0, "actor": HUMAN, "scope": "uncommitted",
            "anchor": {"path": "unicode.txt", "side": "new", "startLine": 2, "endLine": 2,
                       "snapshotId": review["snapshotId"]}, "body": "Second Git line"})
        self.assertEqual(result["thread"]["anchor"]["snippet"], "second")

    def test_aggregate_budget_releases_bodies_but_retains_stable_evidence_and_warning(self):
        # Exercise the actual hydration boundary with a small configured budget;
        # no fake Git/content provider can accidentally implement the invariant.
        from backend import codewalker
        from unittest.mock import patch
        for name in ("01.txt", "02.txt", "03.txt"):
            (self.root / name).write_bytes(b"x" * 48)
        git = codewalker.Git(str(self.root))
        with patch.object(codewalker, "MAX_TOTAL_BYTES", 64):
            review, contents = git.snapshot(selected="03.txt")
            repeated, _ = git.snapshot(selected="03.txt")
        retained = sum(len(raw) for sides in contents.values() for raw in sides[:2] if raw is not None)
        self.assertLessEqual(retained, 64)
        self.assertIsNone(contents["03.txt"][1])
        self.assertEqual(len(contents["03.txt"][3]), 64)
        self.assertEqual(review["snapshotId"], repeated["snapshotId"])
        self.assertTrue(review["warnings"])
        self.assertTrue(review["file"]["tooLarge"])
        self.assertEqual(review["file"]["hunks"], [])
        self.assertTrue(next(f for f in review["files"] if f["path"] == "03.txt")["aggregateLimit"])

    def test_oversized_file_reports_metadata_without_line_anchors(self):
        (self.root / "large.txt").write_bytes(b"x" * 2_000_001)
        result = self.cli("review", "--file", "large.txt")
        self.assertTrue(result["file"]["tooLarge"])
        self.assertEqual(result["file"]["hunks"], [])
        self.cli("create", data={
            "commandId": "large", "expectedRevision": 0, "actor": HUMAN, "scope": "uncommitted",
            "anchor": {"path": "large.txt", "side": "new", "startLine": 1, "endLine": 1,
                       "snapshotId": result["snapshotId"]}, "body": "Not a fake line"}, error="invalid_request")

    def test_shared_blob_reader_stays_bounded_and_in_step_across_odd_inputs(self):
        # One cat-file reader serves every old side. Skipped large bodies, binary
        # bodies and unusual names must not shift the bytes later files receive.
        odd = "a [odd] ünï\tname.txt"
        (self.root / "a-large.txt").write_bytes(b"x\n" * 1_000_001)
        (self.root / "a-binary.bin").write_bytes(b"\0old\n" * 64)
        (self.root / odd).write_text("old odd\n", encoding="utf-8")
        (self.root / "b.txt").write_text("old b\n", encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-qm", "odd inputs")
        (self.root / "a-large.txt").write_bytes(b"y\n" * 1_000_001)
        (self.root / "a-binary.bin").write_bytes(b"\0new\n" * 64)
        (self.root / odd).write_text("new odd\n", encoding="utf-8")
        (self.root / "b.txt").write_text("new b\n", encoding="utf-8")
        self.assertTrue(self.cli("review", "--file", "a-large.txt")["file"]["tooLarge"])
        binary = self.cli("review", "--file", "a-binary.bin")["file"]
        self.assertTrue(binary["binary"])
        self.assertEqual(binary["hunks"], [])
        for path, name in ((odd, "odd"), ("b.txt", "b")):
            rows = self.cli("review", "--file", path)["file"]["hunks"][0]["rows"]
            self.assertEqual([(r["kind"], r["text"]) for r in rows], [("delete", f"old {name}"), ("add", f"new {name}")])

    def test_reads_do_not_write_the_store(self):
        # Store watchers refresh the GUI; a read that writes would wake itself forever.
        self.changed()
        before = (self.store.stat().st_mtime_ns, self.store.read_bytes())
        self.cli("review", "--select-first")
        self.cli("list")
        self.cli("context")
        self.assertEqual((self.store.stat().st_mtime_ns, self.store.read_bytes()), before)

    def test_select_first_opens_the_first_changed_file_in_one_read(self):
        (self.root / "z.txt").write_text("last\n", encoding="utf-8")
        review = self.changed()
        first = self.cli("review", "--select-first")
        self.assertEqual(first["file"]["path"], "a.txt")
        self.assertEqual(first["file"]["hunks"], review["file"]["hunks"])
        self.assertEqual(first["snapshotId"], review["snapshotId"])
        self.cli("review", "--select-first", "--file", "a.txt", error="invalid_request")

    def test_reply_request_is_owed_until_someone_answers(self):
        review = self.changed()
        data = {"commandId": "ask", "expectedRevision": review["context"]["revision"], "actor": HUMAN, "scope": review["scope"],
                "anchor": {"path": "a.txt", "side": "new", "startLine": 2, "endLine": 2, "snapshotId": review["snapshotId"]},
                "body": "Why upper case?", "replyRequested": "yes"}
        self.cli("create", data=dict(data), error="invalid_request")
        created = self.cli("create", data={**data, "replyRequested": True})["thread"]
        self.assertTrue(created["messages"][0]["replyRequested"])
        self.assertTrue(created["awaitingReply"])
        self.assertIn("reply in thread required", self.cli("export")["text"])
        answered = self.cli("reply", data={"commandId": "answer", "expectedRevision": 1, "actor": AGENT,
                                           "threadId": created["id"], "body": "It matches the header."})["thread"]
        self.assertFalse(answered["messages"][1]["replyRequested"])
        self.assertFalse(answered["awaitingReply"])
        asked = self.cli("reply", data={"commandId": "again", "expectedRevision": 2, "actor": HUMAN, "threadId": created["id"],
                                        "body": "And the footer?", "replyRequested": True})["thread"]
        self.assertTrue(asked["awaitingReply"])
        resolved = self.cli("resolve", data={"commandId": "close", "expectedRevision": 3, "actor": HUMAN,
                                             "threadId": created["id"], "resolved": True})["thread"]
        self.assertFalse(resolved["awaitingReply"])

    def test_skill_recipe_replies_and_edits_as_printed(self):
        # Send to agent pastes only `skill --repo`; agents run its recipe as printed.
        created, _ = self.create()
        thread = created["thread"]["id"]
        text = subprocess.run([sys.executable, str(HELPER), "skill", "--repo", str(self.root)], capture_output=True, text=True, check=True).stdout
        self.assertIn(str(self.root), text)
        recipe = text.split("python3 - <<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
        # The temp store only; the printed recipe uses the default store.
        recipe = recipe.replace('"--repo", ', f'"--store", {str(self.store)!r}, "--repo", ', 1)

        def run(action, target, body):
            filled = (recipe.replace('ACTION = "reply"', f"ACTION = {action!r}", 1).replace('"<thread id>"', repr(target), 1)
                      .replace('\"\"\"<your reply>\"\"\"', repr(body), 1).replace('"<your agent name>"', '"Test agent"', 1))
            return json.loads(subprocess.run([sys.executable, "-c", filled], capture_output=True, text=True, check=True).stdout)

        body = "It's `$HOME` and \"quoted\".\n\nKept."
        replied = run("reply", thread, body)
        self.assertTrue(replied["ok"], replied)
        message = replied["data"]["thread"]["messages"][1]
        self.assertEqual((message["body"], message["actor"]["name"]), (body, "Test agent"))
        edited = run("edit", message["id"], "Short answer.")
        self.assertEqual(edited["data"]["thread"]["messages"][1]["body"], "Short answer.")
        self.assertIn("--repo is required", subprocess.run([sys.executable, str(HELPER), "list", "--json"], capture_output=True, text=True).stdout)

    def test_older_stores_migrate_and_keep_messages(self):
        # Rebuild the messages table as version 1 and 2 had it: same rows, fewer columns.
        for version, columns, extra in ((1, "sequence,id,thread_id,actor,body,created_at", ""),
                                        (2, "sequence,id,thread_id,actor,body,created_at,reply_requested", ", reply_requested INTEGER NOT NULL DEFAULT 0")):
            with self.subTest(version=version):
                self.store.unlink(missing_ok=True)
                created, _ = self.create(command=f"create-v{version}")
                with sqlite3.connect(self.store) as db:
                    db.executescript(f"""
                        DROP TABLE message_edits; DROP TABLE deliveries;
                        CREATE TABLE old AS SELECT {columns} FROM messages;
                        DROP TABLE messages;
                        CREATE TABLE messages (sequence INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE, thread_id TEXT NOT NULL REFERENCES threads(id), actor TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL{extra});
                        INSERT INTO messages SELECT * FROM old; DROP TABLE old;
                        CREATE INDEX messages_thread ON messages(thread_id,sequence);
                        PRAGMA user_version={version};""")
                thread = self.cli("list")["threads"][0]
                self.assertEqual(thread["messages"][0]["body"], created["thread"]["messages"][0]["body"])
                self.assertFalse(thread["messages"][0]["replyRequested"])
                self.assertIsNone(thread["messages"][0]["editedAt"])
                self.assertFalse(thread["awaitingReply"])
                with sqlite3.connect(self.store) as db:
                    self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 6)
                # The migrated store takes edits.
                message = thread["messages"][0]["id"]
                self.cli("edit", data={"commandId": f"edit-v{version}", "expectedRevision": 1, "actor": HUMAN, "messageId": message, "body": "Edited"})

    def test_deleting_a_reply_keeps_the_thread_deleting_the_comment_removes_it(self):
        created, _ = self.create()
        thread_id, first = created["thread"]["id"], created["thread"]["messages"][0]["id"]
        reply = self.cli("reply", data={"commandId": "r1", "expectedRevision": 1, "actor": AGENT, "threadId": thread_id, "body": "Agent note"})["thread"]["messages"][1]["id"]
        mine = self.cli("reply", data={"commandId": "r2", "expectedRevision": 2, "actor": HUMAN, "threadId": thread_id, "body": "Thanks", "replyRequested": True})["thread"]["messages"][2]["id"]
        self.cli("edit", data={"commandId": "e1", "expectedRevision": 3, "actor": HUMAN, "messageId": first, "body": "Edited"})
        self.cli("delete", data={"commandId": "d0", "expectedRevision": 4, "actor": HUMAN, "messageId": reply}, error="invalid_request")
        kept = self.cli("delete", data={"commandId": "d1", "expectedRevision": 4, "actor": HUMAN, "messageId": mine})
        self.assertEqual(kept["deleted"], {"messageId": mine, "threadId": thread_id, "thread": False})
        self.assertEqual([m["id"] for m in kept["thread"]["messages"]], [first, reply])
        self.assertFalse(kept["thread"]["awaitingReply"])
        gone = self.cli("delete", data={"commandId": "d2", "expectedRevision": 5, "actor": HUMAN, "messageId": first})
        self.assertEqual(gone["deleted"], {"messageId": first, "threadId": thread_id, "thread": True})
        self.assertEqual(self.cli("list")["threads"], [])
        with sqlite3.connect(self.store) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM messages").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM message_edits").fetchone()[0], 0)
        self.cli("delete", data={"commandId": "d3", "expectedRevision": 6, "actor": HUMAN, "messageId": first}, error="not_found")

    def test_delivery_is_recorded_per_message_and_goes_with_it(self):
        created, _ = self.create()
        message = created["thread"]["messages"][0]["id"]
        deliver = {"commandId": "s1", "expectedRevision": 1, "actor": HUMAN, "messageIds": [message], "state": "uncertain", "target": "t7"}
        self.cli("deliver", data={**deliver, "state": "maybe"}, error="invalid_request")
        self.cli("deliver", data={**deliver, "messageIds": ["message_missing"]}, error="not_found")
        self.cli("deliver", data=deliver)
        self.assertEqual(self.cli("list")["threads"][0]["messages"][0]["delivery"]["state"], "uncertain")
        self.cli("deliver", data={**deliver, "commandId": "s2", "expectedRevision": 2, "state": "sent"})
        delivery = self.cli("list")["threads"][0]["messages"][0]["delivery"]
        self.assertEqual((delivery["state"], delivery["target"]), ("sent", "t7"))
        self.cli("deliver", data={**deliver, "commandId": "s3", "expectedRevision": 3, "state": "none"})
        self.assertIsNone(self.cli("list")["threads"][0]["messages"][0]["delivery"])
        self.cli("deliver", data={**deliver, "commandId": "s4", "expectedRevision": 4, "state": "sent"})
        self.cli("delete", data={"commandId": "d1", "expectedRevision": 5, "actor": HUMAN, "messageId": message})
        with sqlite3.connect(self.store) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM deliveries").fetchone()[0], 0)

    def test_only_the_author_edits_a_message_and_the_old_body_is_kept(self):
        created, _ = self.create()
        message = created["thread"]["messages"][0]
        edit = {"commandId": "edit1", "expectedRevision": 1, "actor": AGENT, "messageId": message["id"], "body": "Hijacked"}
        self.cli("edit", data=dict(edit), error="invalid_request")
        self.cli("edit", data={**edit, "commandId": "edit2", "messageId": "message_missing", "actor": HUMAN}, error="not_found")
        edited = self.cli("edit", data={**edit, "commandId": "edit3", "actor": HUMAN, "body": "Why upper case?"})["thread"]
        self.assertEqual(edited["messages"][0]["id"], message["id"])
        self.assertEqual(edited["messages"][0]["body"], "Why upper case?")
        self.assertIsNotNone(edited["messages"][0]["editedAt"])
        self.assertEqual(self.cli("list")["revision"], 2)
        with sqlite3.connect(self.store) as db:
            self.assertEqual(db.execute("SELECT body FROM message_edits WHERE message_id=?", (message["id"],)).fetchall(), [(message["body"],)])

    def test_corrupt_and_newer_stores_fail_without_reset(self):
        self.store.parent.mkdir()
        self.store.write_bytes(b"not a sqlite database")
        self.cli("context", error="storage_error")
        self.assertEqual(self.store.read_bytes(), b"not a sqlite database")
        self.store.unlink()
        with sqlite3.connect(self.store) as db:
            db.execute("PRAGMA user_version=99")
        self.cli("context", error="storage_error")
        with sqlite3.connect(self.store) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 99)


    # Walkthroughs ---------------------------------------------------------

    def walkthrough_fixture(self):
        """b.py: lines 4-5 replaced and one line added after line 16, so the diff has two hunks."""
        (self.root / "b.py").write_text("".join(f"line {n}\n" for n in range(1, 21)), encoding="utf-8")
        self.git("add", "b.py")
        self.git("commit", "-qm", "b")
        lines = [f"line {n}" for n in range(1, 21)]
        lines[3:5] = ["LINE 4", "LINE 5"]
        lines.insert(16, "added")
        (self.root / "b.py").write_text("\n".join(lines) + "\n", encoding="utf-8")
        return self.cli("review")

    def walk(self, review, steps, command="walk1", actor=AGENT, **extra):
        data = {"commandId": command, "expectedRevision": review["context"]["revision"], "actor": actor,
                "scope": review["scope"], "snapshotId": review["snapshotId"], "title": "How b.py changed",
                "summary": "Two edits.", "steps": steps, **extra}
        return data

    STEPS = [
        {"title": "Intro", "body": "Read this **first**.", "view": None},
        {"title": "Rename", "body": "Upper case.", "view": {"kind": "diff", "path": "b.py", "side": "new", "startLine": 4, "endLine": 5}},
        {"title": "Caller", "body": "Unchanged context.", "view": {"kind": "source", "path": "a.txt", "rev": "head", "startLine": 1, "endLine": 2}},
        {"title": "Whole file", "body": "All of it.", "view": {"kind": "diff", "path": "b.py"}},
    ]

    def test_walkthrough_freezes_rows_and_marks_steps_stale_when_code_changes(self):
        review = self.walkthrough_fixture()
        created = self.cli("walkthrough-create", data=self.walk(review, self.STEPS))
        walkthrough = created["walkthrough"]
        self.assertEqual(created["revision"], 1)
        self.assertEqual([s["title"] for s in walkthrough["steps"]], ["Intro", "Rename", "Caller", "Whole file"])
        self.assertEqual([s["position"] for s in walkthrough["steps"]], [0, 1, 2, 3])
        self.assertEqual(walkthrough["validity"], "current")
        self.assertEqual(walkthrough["comparison"], {"scope": "uncommitted", "baseRef": "HEAD"})
        intro, rename, caller, whole = walkthrough["steps"]
        self.assertIsNone(intro["view"])
        # The step body is the first message of the step's thread.
        self.assertEqual(intro["thread"]["messages"][0]["body"], "Read this **first**.")
        self.assertEqual(intro["thread"]["messages"][0]["actor"], AGENT)
        self.assertEqual(intro["thread"]["walkthrough"]["position"], 0)
        self.assertEqual(intro["thread"]["walkthrough"]["stepCount"], 4)
        # A new-side range keeps the removed lines it replaces.
        self.assertEqual([(r["kind"], r["oldLine"], r["newLine"], r["text"]) for r in rename["view"]["hunks"][0]["rows"]],
                         [("delete", 4, None, "line 4"), ("delete", 5, None, "line 5"), ("add", None, 4, "LINE 4"), ("add", None, 5, "LINE 5")])
        self.assertEqual([(r["newLine"], r["text"]) for r in caller["view"]["hunks"][0]["rows"]], [(1, "one"), (2, "two")])
        self.assertEqual(len(whole["view"]["hunks"]), 2)
        # Step threads stay out of Review's discussions; the summary is listed.
        listing = self.cli("list")
        self.assertEqual(listing["threads"], [])
        self.assertEqual([(w["id"], w["stepCount"], w["replies"]) for w in listing["walkthroughs"]], [(walkthrough["id"], 4, 0)])
        self.assertEqual(self.cli("review")["walkthroughs"][0]["id"], walkthrough["id"])
        self.assertIn('Walkthrough "How b.py changed"', self.cli("export")["text"])
        # Code changes: the excerpt stays as it was; only the changed file's steps are stale.
        (self.root / "b.py").write_text("rewritten\n", encoding="utf-8")
        full = self.cli("walkthrough", "--walkthrough", walkthrough["id"])
        # The pane watches this store and restores unsent step replies from drafts.
        self.assertEqual((full["context"]["storePath"], full["drafts"]), (str(self.store.resolve()), []))
        read = full["walkthrough"]
        self.assertEqual([s["validity"] for s in read["steps"]], ["current", "stale", "current", "stale"])
        self.assertEqual(read["steps"][1]["view"], rename["view"])
        self.assertEqual(read["validity"], "stale")
        (self.root / "a.txt").write_text("changed\n", encoding="utf-8")
        read = self.cli("walkthrough", "--walkthrough", walkthrough["id"])["walkthrough"]
        self.assertEqual(read["steps"][2]["validity"], "stale")
        self.cli("walkthrough", "--walkthrough", "walkthrough_missing", error="not_found")
        self.cli("walkthrough", error="invalid_request")

    def test_walkthrough_create_checks_every_step_and_writes_nothing_on_failure(self):
        review = self.walkthrough_fixture()
        bad = [dict(self.STEPS[0]), {"title": "Wrong", "body": "x", "view": {"kind": "diff", "path": "b.py", "side": "new", "startLine": 2, "endLine": 9}}]
        failed = self.cli("walkthrough-create", data=self.walk(review, bad), error="invalid_request")
        self.assertEqual(failed["error"]["details"]["step"], 1)
        self.cli("walkthrough-create", data=self.walk(review, self.STEPS, command="h", actor=HUMAN), error="invalid_request")
        self.cli("walkthrough-create", data=self.walk(review, [{"title": "t", "body": "b", "view": {"kind": "diff", "path": "a.txt"}}], command="n"), error="not_found")
        self.cli("walkthrough-create", data=self.walk(review, [{"title": "t", "body": "b", "view": {"kind": "source", "path": "a.txt", "rev": "base", "startLine": 1, "endLine": 9}}], command="p"), error="invalid_request")
        self.cli("walkthrough-create", data=self.walk(review, [{"title": "t", "body": "b", "rows": []}], command="r"), error="invalid_request")
        self.cli("walkthrough-create", data=self.walk(review, [], command="e"), error="invalid_request")
        self.cli("walkthrough-create", data=self.walk(review, self.STEPS, command="s", snapshotId="snapshot_old"), error="snapshot_conflict")
        state = self.cli("walkthroughs")
        self.assertEqual((state["revision"], state["walkthroughs"]), (0, []))
        with sqlite3.connect(self.store) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM threads").fetchone()[0], 0)

    def test_walkthrough_steps_take_replies_and_keep_their_first_message(self):
        review = self.walkthrough_fixture()
        walkthrough = self.cli("walkthrough-create", data=self.walk(review, self.STEPS))["walkthrough"]
        thread = walkthrough["steps"][1]["thread"]
        asked = self.cli("reply", data={"commandId": "ask", "expectedRevision": 1, "actor": HUMAN, "threadId": thread["id"],
                                        "body": "Why upper case?", "replyRequested": True})["thread"]
        self.assertTrue(asked["awaitingReply"])
        self.assertEqual(asked["anchor"]["validity"], "current")
        self.assertEqual(asked["walkthrough"]["stepId"], walkthrough["steps"][1]["id"])
        summary = self.cli("walkthroughs")["walkthroughs"][0]
        self.assertEqual((summary["awaitingReply"], summary["replies"]), (1, 1))
        self.cli("reply", data={"commandId": "answer", "expectedRevision": 2, "actor": AGENT, "threadId": thread["id"], "body": "It is a constant."})
        self.assertEqual(self.cli("walkthroughs")["walkthroughs"][0]["awaitingReply"], 0)
        first = thread["messages"][0]["id"]
        self.cli("delete", data={"commandId": "del", "expectedRevision": 3, "actor": AGENT, "messageId": first}, error="invalid_request")
        edited = self.cli("edit", data={"commandId": "edit", "expectedRevision": 3, "actor": AGENT, "messageId": first, "body": "Constants are upper case."})
        self.assertEqual(edited["thread"]["messages"][0]["body"], "Constants are upper case.")
        self.cli("resolve", data={"commandId": "res", "expectedRevision": 4, "actor": HUMAN, "threadId": thread["id"], "resolved": True})
        # Clearing Review's history keeps walkthroughs: they have their own delete.
        self.create(command="note")
        cleared = self.cli("history-clear", data={"commandId": "clear", "expectedRevision": 6, "actor": HUMAN, "confirmed": True})
        self.assertEqual(cleared["cleared"], {"threads": 1})
        kept = self.cli("walkthrough", "--walkthrough", walkthrough["id"])["walkthrough"]
        self.assertEqual(len(kept["steps"]), 4)
        self.assertEqual(kept["steps"][1]["thread"]["status"], "resolved")

    def test_walkthrough_append_update_and_delete_follow_authorship(self):
        review = self.walkthrough_fixture()
        walkthrough = self.cli("walkthrough-create", data=self.walk(review, self.STEPS[:2]))["walkthrough"]
        wid, first = walkthrough["id"], walkthrough["steps"][0]["id"]
        base = {"expectedRevision": 1, "walkthroughId": wid}
        extra = [{"title": "Added", "body": "Inserted.", "view": {"kind": "diff", "path": "b.py", "side": "new", "startLine": 17, "endLine": 17}}]
        append = {**base, "commandId": "app", "actor": AGENT, "snapshotId": review["snapshotId"], "afterStepId": first, "steps": extra}
        self.cli("walkthrough-append", data={**append, "commandId": "other", "actor": {**AGENT, "id": "other"}}, error="invalid_request")
        self.cli("walkthrough-append", data={**append, "commandId": "miss", "afterStepId": "step_missing"}, error="not_found")
        appended = self.cli("walkthrough-append", data=dict(append))
        self.assertEqual([s["title"] for s in appended["walkthrough"]["steps"]], ["Intro", "Added", "Rename"])
        self.assertEqual([s["position"] for s in appended["walkthrough"]["steps"]], [0, 1, 2])
        self.assertEqual(appended["walkthrough"]["steps"][1]["view"]["hunks"][0]["rows"][0]["text"], "added")
        # A retry of the same command returns the first result and writes nothing.
        self.assertEqual(self.cli("walkthrough-append", data=dict(append)), appended)
        update = {**base, "expectedRevision": 2, "commandId": "up", "actor": HUMAN, "title": "Mine"}
        self.cli("walkthrough-update", data=dict(update), error="invalid_request")
        archived = self.cli("walkthrough-update", data={**base, "expectedRevision": 2, "commandId": "arch", "actor": HUMAN, "status": "archived"})
        self.assertEqual(archived["walkthrough"]["status"], "archived")
        renamed = self.cli("walkthrough-update", data={**base, "expectedRevision": 3, "commandId": "ren", "actor": AGENT, "title": "Renamed"})
        self.assertEqual(renamed["walkthrough"]["title"], "Renamed")
        delete = {**base, "expectedRevision": 4, "commandId": "del", "actor": HUMAN}
        self.cli("walkthrough-delete", data=dict(delete), error="invalid_request")
        deleted = self.cli("walkthrough-delete", data={**delete, "commandId": "del2", "confirmed": True})
        self.assertEqual(deleted["deleted"], {"walkthroughId": wid, "steps": 3})
        self.assertEqual(self.cli("walkthroughs")["walkthroughs"], [])
        with sqlite3.connect(self.store) as db:
            for table in ("threads", "messages", "walkthrough_steps", "walkthroughs"):
                self.assertEqual(db.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0, table)

    def test_walkthrough_pins_a_commit_and_its_source_steps_never_go_stale(self):
        self.walkthrough_fixture()
        self.git("commit", "-qam", "upper")
        review = self.cli("review", "--scope", "commit", "--commit", "HEAD")
        steps = [self.STEPS[1], {"title": "Before", "body": "Old.", "view": {"kind": "source", "path": "b.py", "rev": "base", "startLine": 4, "endLine": 4}}]
        walkthrough = self.cli("walkthrough-create", data=self.walk(review, steps, commit=review["commitOid"]))["walkthrough"]
        self.assertEqual(walkthrough["comparison"]["commitOid"], review["commitOid"])
        self.assertEqual(walkthrough["steps"][1]["view"]["hunks"][0]["rows"][0]["text"], "line 4")
        (self.root / "b.py").write_text("worktree edits do not matter\n", encoding="utf-8")
        self.git("commit", "-qam", "later")
        read = self.cli("walkthrough", "--walkthrough", walkthrough["id"])["walkthrough"]
        self.assertEqual([s["validity"] for s in read["steps"]], ["current", "current"])

    def test_open_walkthrough_checks_it_exists_before_opening_a_pane(self):
        review = self.walkthrough_fixture()
        wid = self.cli("walkthrough-create", data=self.walk(review, self.STEPS))["walkthrough"]["id"]
        tool_dir = Path(self.temp.name) / "bin"
        tool_dir.mkdir()
        calls = Path(self.temp.name) / "calls.jsonl"
        app = tool_dir / "herdr-app"
        app.write_text(f"#!{sys.executable}\n" + "import json,sys\n"
            + f"with open({str(calls)!r}, 'a') as f: f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            + "print(json.dumps({'spaces':[{'tabs':[{'terminals':[{'terminal':'t-source','pane':'p-source'}]}]}]} if sys.argv[1] == 'ls' else {'pane':'p-created'}))\n")
        app.chmod(0o755)
        env = {**os.environ, "PATH": str(tool_dir) + os.pathsep + os.environ.get("PATH", ""), "HC_TERMINAL_ID": "t-source"}
        argv = [sys.executable, str(HELPER), "--store", str(self.store), "open", "--repo", str(self.root), "--walkthrough", wid]
        result = json.loads(subprocess.run(argv, env=env, capture_output=True, text=True, timeout=30).stdout)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["data"], {"pane": "p-created", "folder": str(self.root.resolve()), "walkthroughId": wid, "scope": "uncommitted"})
        split = json.loads([json.loads(line) for line in calls.read_text().splitlines()][1][2])
        self.assertEqual(split["props"], {"framed": True, "folder": str(self.root.resolve()), "walkthrough": wid})
        calls.write_text("")
        argv[-1] = "walkthrough_missing"
        self.assertEqual(json.loads(subprocess.run(argv, env=env, capture_output=True, text=True, timeout=30).stdout)["error"]["code"], "not_found")
        self.assertEqual(calls.read_text(), "", "a missing walkthrough cannot open a pane")

    def test_walkthrough_skill_recipe_creates_a_walkthrough_as_printed(self):
        self.walkthrough_fixture()
        text = subprocess.run([sys.executable, str(HELPER), "skill", "--topic", "walkthrough", "--repo", str(self.root)],
                              capture_output=True, text=True, check=True).stdout
        self.assertIn("walkthrough-create", text)
        recipe = text.split("python3 - <<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
        recipe = (recipe.replace('"--repo", ', f'"--store", {str(self.store)!r}, "--repo", ', 1)
                  .replace('"<your agent name>"', '"Test agent"', 1).replace('"<path>"', '"b.py"', 1)
                  .replace('"startLine": 1, "endLine": 10', '"startLine": 4, "endLine": 5', 1))
        result = json.loads(subprocess.run([sys.executable, "-c", recipe], capture_output=True, text=True, check=True).stdout)
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(result["steps"]), 2)
        read = self.cli("walkthrough", "--walkthrough", result["walkthroughId"])["walkthrough"]
        self.assertEqual(read["author"]["name"], "Test agent")
        hello = json.loads(subprocess.run([sys.executable, str(HELPER), "hello", "--json"], capture_output=True, text=True).stdout)["data"]
        self.assertIn("walkthroughs", hello["capabilities"])
        self.assertEqual(hello["helperVersion"], 3)

if __name__ == "__main__":
    unittest.main()

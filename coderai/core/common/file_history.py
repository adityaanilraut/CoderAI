"""GitFileHistory — isolated content-addressable checkpoint undo (deepcode file-history.ts)."""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import subprocess
from dataclasses import dataclass
from typing import Any

MANIFEST_PATH = "manifest.json"
FILE_HISTORY_AUTHOR_NAME = "CoderAI"
FILE_HISTORY_AUTHOR_EMAIL = "coderai@local"

COMMIT_HASH_PATTERN = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)
STORED_PATH_PATTERN = re.compile(r"^files-[0-9a-f]{64}$")
SESSION_REF_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")


@dataclass
class FileHistoryEntry:
    path: str
    blob: str | None
    mode: str = "100644"


@dataclass
class FileHistoryManifest:
    version: int
    files: dict[str, FileHistoryEntry]


@dataclass
class FileHistoryCheckpointResult:
    checkpoint_hash: str | None
    changed: bool


class GitFileHistory:
    def __init__(self, project_root: str, git_dir: str):
        self.project_root = str(pathlib.Path(project_root).resolve())
        self.git_dir = str(pathlib.Path(git_dir).resolve())

    def _get_git_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.setdefault("GIT_AUTHOR_NAME", FILE_HISTORY_AUTHOR_NAME)
        env.setdefault("GIT_AUTHOR_EMAIL", FILE_HISTORY_AUTHOR_EMAIL)
        env.setdefault("GIT_COMMITTER_NAME", FILE_HISTORY_AUTHOR_NAME)
        env.setdefault("GIT_COMMITTER_EMAIL", FILE_HISTORY_AUTHOR_EMAIL)
        return env

    def _spawn_git(
        self,
        args: list[str],
        input_data: bytes | str | None = None,
        env: dict[str, str] | None = None,
    ) -> bytes:
        git_args = [
            "git",
            "-c",
            "core.autocrlf=false",
            "-c",
            "core.eol=lf",
            f"--git-dir={self.git_dir}",
            *args,
        ]
        input_bytes = input_data.encode("utf-8") if isinstance(input_data, str) else input_data
        res = subprocess.run(
            git_args,
            input=input_bytes,
            capture_output=True,
            env=env or self._get_git_env(),
        )
        if res.returncode != 0:
            err = (res.stderr or res.stdout or b"").decode("utf-8", errors="replace").strip()
            raise RuntimeError(err or f"git {' '.join(args)} failed with code {res.returncode}")
        return res.stdout

    def _run_git_text(self, args: list[str], input_data: str | None = None) -> str:
        out = self._spawn_git(args, input_data=input_data)
        return out.decode("utf-8", errors="replace")

    def _init_repo_if_needed(self) -> None:
        if os.path.exists(self.git_dir):
            return
        os.makedirs(self.git_dir, exist_ok=True)
        res = subprocess.run(
            ["git", "init", "--bare", self.git_dir],
            capture_output=True,
        )
        if res.returncode != 0:
            err = (res.stderr or b"").decode("utf-8", errors="replace").strip()
            raise RuntimeError(err or "git init --bare failed")

    def _get_session_branch_ref(self, session_id: str) -> str | None:
        if not SESSION_REF_PATTERN.match(session_id):
            return None
        return f"refs/heads/{session_id}"

    def ensure_session(self, session_id: str) -> str | None:
        branch_ref = self._get_session_branch_ref(session_id)
        if not branch_ref:
            return None
        self._init_repo_if_needed()

        current_hash = self.get_current_checkpoint_hash(session_id)
        if current_hash:
            return current_hash

        manifest: dict[str, Any] = {"version": 2, "files": {}}
        tree_hash = self._create_tree(manifest)
        commit_hash = self._create_commit(tree_hash, None, "Initial session checkpoint")
        self._spawn_git(["update-ref", branch_ref, commit_hash])
        return commit_hash

    def get_current_checkpoint_hash(self, session_id: str) -> str | None:
        branch_ref = self._get_session_branch_ref(session_id)
        if not branch_ref or not os.path.exists(self.git_dir):
            return None
        try:
            out = self._run_git_text(["rev-parse", "--verify", f"{branch_ref}^{{commit}}"]).strip()
            return out if _is_commit_hash(out) else None
        except Exception:
            return None

    def record_checkpoint(
        self, session_id: str, file_paths: list[str], message: str
    ) -> FileHistoryCheckpointResult:
        branch_ref = self._get_session_branch_ref(session_id)
        if not branch_ref:
            return FileHistoryCheckpointResult(None, False)

        current_hash = self.ensure_session(session_id)
        if not current_hash:
            return FileHistoryCheckpointResult(None, False)

        normalized_paths = list(dict.fromkeys(str(pathlib.Path(p).resolve()) for p in file_paths))
        if not normalized_paths:
            return FileHistoryCheckpointResult(current_hash, False)

        current_manifest = self._read_manifest(current_hash)
        next_files = dict(current_manifest.files)

        for abs_path in normalized_paths:
            key = self._get_file_key(abs_path)
            if not os.path.exists(abs_path) or os.path.isdir(abs_path):
                next_files[key] = FileHistoryEntry(path=abs_path, blob=None, mode="100644")
                continue

            try:
                blob_hash = self._hash_file(abs_path)
                next_files[key] = FileHistoryEntry(path=abs_path, blob=blob_hash, mode="100644")
            except Exception:
                continue

        manifest_dict: dict[str, Any] = {
            "version": 2,
            "files": {
                k: {"path": v.path, "blob": v.blob, "mode": v.mode}
                for k, v in sorted(next_files.items())
            },
        }
        tree_hash = self._create_tree(manifest_dict)
        try:
            parent_tree_hash = self._run_git_text(["rev-parse", f"{current_hash}^{{tree}}"]).strip()
            if tree_hash == parent_tree_hash:
                return FileHistoryCheckpointResult(current_hash, False)
        except Exception:
            pass

        next_commit_hash = self._create_commit(tree_hash, current_hash, message)
        self._spawn_git(["update-ref", branch_ref, next_commit_hash])
        return FileHistoryCheckpointResult(next_commit_hash, True)

    def record_tracked_files_checkpoint(
        self, session_id: str, message: str
    ) -> FileHistoryCheckpointResult:
        current_hash = self.ensure_session(session_id)
        if not current_hash:
            return FileHistoryCheckpointResult(None, False)
        manifest = self._read_manifest(current_hash)
        file_paths = [e.path for e in manifest.files.values()]
        return self.record_checkpoint(session_id, file_paths, message)

    def can_restore(self, session_id: str, checkpoint_hash: str) -> bool:
        if not _is_commit_hash(checkpoint_hash):
            return False
        if not self._get_session_branch_ref(session_id):
            return False
        if not os.path.exists(self.git_dir):
            return False
        try:
            self._spawn_git(["cat-file", "-e", f"{checkpoint_hash}^{{commit}}"])
            self._read_manifest(checkpoint_hash)
            return True
        except Exception:
            return False

    def restore(self, session_id: str, checkpoint_hash: str) -> None:
        if not _is_commit_hash(checkpoint_hash):
            raise ValueError("Invalid checkpoint hash.")
        branch_ref = self._get_session_branch_ref(session_id)
        if not branch_ref or not os.path.exists(self.git_dir):
            raise RuntimeError("File history Git repository was not found for this project.")

        self._spawn_git(["cat-file", "-e", f"{checkpoint_hash}^{{commit}}"])

        current_hash = self.get_current_checkpoint_hash(session_id)
        current_manifest = (
            self._read_manifest(current_hash)
            if current_hash
            else FileHistoryManifest(version=2, files={})
        )
        target_manifest = self._read_manifest(checkpoint_hash)

        # Handle files present in current that were removed in target
        for key, entry in current_manifest.files.items():
            if key not in target_manifest.files:
                self._restore_first_known_entry(current_hash, key, entry.path)

        # Restore files in target
        for entry in target_manifest.files.values():
            if not entry.blob:
                _remove_tracked_file(entry.path)
                continue
            os.makedirs(os.path.dirname(entry.path), exist_ok=True)
            blob_bytes = self._read_blob(entry.blob)
            with open(entry.path, "wb") as f:
                f.write(blob_bytes)

        self._spawn_git(["update-ref", branch_ref, checkpoint_hash])

    def fork_session(
        self,
        source_session_id: str,
        target_session_id: str,
        checkpoint_hash: str | None = None,
    ) -> None:
        source_ref = self._get_session_branch_ref(source_session_id)
        target_ref = self._get_session_branch_ref(target_session_id)
        if not source_ref or not target_ref or not os.path.exists(self.git_dir):
            return
        target_hash = (
            checkpoint_hash
            if (checkpoint_hash and _is_commit_hash(checkpoint_hash))
            else self.get_current_checkpoint_hash(source_session_id)
        )
        if target_hash:
            self._spawn_git(["update-ref", target_ref, target_hash])

    def list_checkpoints(self, session_id: str) -> list[dict[str, str]]:
        branch_ref = self._get_session_branch_ref(session_id)
        if not branch_ref or not os.path.exists(self.git_dir):
            return []
        try:
            out = self._run_git_text(["log", "--format=%H|%s", branch_ref]).strip()
            if not out:
                return []
            results: list[dict[str, str]] = []
            for line in out.splitlines():
                if "|" in line:
                    h, msg = line.split("|", 1)
                    if _is_commit_hash(h.strip()):
                        results.append({"hash": h.strip(), "message": msg.strip()})
            return results
        except Exception:
            return []

    def get_diff(self, session_id: str, from_checkpoint: str | None = None) -> str:
        """Compute unified diff of all tracked files between a base checkpoint and current state on disk."""
        import difflib

        branch_ref = self._get_session_branch_ref(session_id)
        if not branch_ref or not os.path.exists(self.git_dir):
            return ""

        current_hash = self.get_current_checkpoint_hash(session_id)
        if not current_hash:
            return ""

        current_manifest = self._read_manifest(current_hash)

        if from_checkpoint and _is_commit_hash(from_checkpoint):
            try:
                base_manifest = self._read_manifest(from_checkpoint)
            except Exception:
                base_manifest = FileHistoryManifest(version=2, files={})
        else:
            base_manifest = None

        all_keys = (
            sorted(set(base_manifest.files.keys()) | set(current_manifest.files.keys()))
            if base_manifest
            else sorted(current_manifest.files.keys())
        )
        diff_chunks: list[str] = []

        for key in all_keys:
            if base_manifest:
                base_entry = base_manifest.files.get(key)
            else:
                base_entry = self._find_first_known_entry(current_hash, key)

            curr_entry = current_manifest.files.get(key)
            entry = curr_entry or base_entry
            if not entry:
                continue
            file_path = entry.path
            if not file_path:
                continue

            try:
                rel_path = os.path.relpath(file_path, self.project_root)
            except Exception:
                rel_path = file_path

            base_content = ""
            if base_entry and base_entry.blob:
                try:
                    base_content = self._read_blob(base_entry.blob).decode(
                        "utf-8", errors="replace"
                    )
                except Exception:
                    base_content = ""

            curr_content = ""
            if os.path.exists(file_path) and os.path.isfile(file_path):
                try:
                    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                        curr_content = f.read()
                except Exception:
                    curr_content = ""

            if base_content == curr_content:
                continue

            base_lines = base_content.splitlines(keepends=True)
            curr_lines = curr_content.splitlines(keepends=True)
            diff_lines = list(
                difflib.unified_diff(
                    base_lines,
                    curr_lines,
                    fromfile=f"a/{rel_path}",
                    tofile=f"b/{rel_path}",
                )
            )
            if diff_lines:
                diff_chunks.append("".join(diff_lines))

        return "\n".join(diff_chunks)

    def _restore_first_known_entry(
        self, current_hash: str | None, key: str, fallback_path: str
    ) -> None:
        first_entry = self._find_first_known_entry(current_hash, key) if current_hash else None
        entry = first_entry or FileHistoryEntry(path=fallback_path, blob=None, mode="100644")
        if not entry.blob:
            _remove_tracked_file(entry.path)
            return
        os.makedirs(os.path.dirname(entry.path), exist_ok=True)
        blob_bytes = self._read_blob(entry.blob)
        with open(entry.path, "wb") as f:
            f.write(blob_bytes)

    def _find_first_known_entry(self, current_hash: str, key: str) -> FileHistoryEntry | None:
        try:
            commits = (
                self._run_git_text(["rev-list", "--reverse", current_hash]).strip().splitlines()
            )
            for c in commits:
                c = c.strip()
                if _is_commit_hash(c):
                    manifest = self._read_manifest(c)
                    if key in manifest.files:
                        return manifest.files[key]
        except Exception:
            return None
        return None

    def _create_commit(self, tree_hash: str, parent_hash: str | None, message: str) -> str:
        args = ["commit-tree", tree_hash]
        if parent_hash:
            args.extend(["-p", parent_hash])
        args.extend(["-m", message])
        return self._run_git_text(args).strip()

    def _create_tree(self, manifest_dict: dict[str, Any]) -> str:
        manifest_json = json.dumps(manifest_dict, indent=2) + "\n"
        manifest_blob = self._hash_content(manifest_json)
        entries: list[str] = [f"100644 blob {manifest_blob}\t{MANIFEST_PATH}\0"]

        files = manifest_dict.get("files", {})
        for key, entry in files.items():
            blob = entry.get("blob")
            mode = entry.get("mode", "100644")
            if blob:
                entries.append(f"{mode} blob {blob}\t{key}\0")

        input_data = "".join(entries)
        return self._run_git_text(["mktree", "-z"], input_data=input_data).strip()

    def _read_manifest(self, commit_hash: str) -> FileHistoryManifest:
        out = self._spawn_git(["cat-file", "blob", f"{commit_hash}:{MANIFEST_PATH}"])
        data = json.loads(out.decode("utf-8", errors="replace"))
        files: dict[str, FileHistoryEntry] = {}
        for key, item in data.get("files", {}).items():
            if not STORED_PATH_PATTERN.match(key):
                continue
            files[key] = FileHistoryEntry(
                path=str(pathlib.Path(item["path"]).resolve()),
                blob=item.get("blob"),
                mode=item.get("mode", "100644"),
            )
        return FileHistoryManifest(version=data.get("version", 2), files=files)

    def _read_blob(self, blob_hash: str) -> bytes:
        if not _is_commit_hash(blob_hash):
            raise ValueError("Invalid blob hash.")
        return self._spawn_git(["cat-file", "blob", blob_hash])

    def _hash_file(self, file_path: str) -> str:
        out = self._run_git_text(["hash-object", "-w", "--", file_path]).strip()
        if not _is_commit_hash(out):
            raise RuntimeError("Failed to hash file into git object database.")
        return out

    def _hash_content(self, content: str) -> str:
        out = self._run_git_text(["hash-object", "-w", "--stdin"], input_data=content).strip()
        if not _is_commit_hash(out):
            raise RuntimeError("Failed to hash content into git object database.")
        return out

    def _get_file_key(self, file_path: str) -> str:
        h = hashlib.sha256(file_path.encode("utf-8")).hexdigest()
        return f"files-{h}"


def _is_same_entry(left: FileHistoryEntry | None, right: FileHistoryEntry | None) -> bool:
    if left is None or right is None:
        return False
    return left.path == right.path and left.blob == right.blob and left.mode == right.mode


def _remove_tracked_file(file_path: str) -> None:
    if not os.path.exists(file_path):
        return
    if os.path.isdir(file_path):
        return
    try:
        os.remove(file_path)
    except Exception:
        pass


def _is_commit_hash(val: str) -> bool:
    return bool(COMMIT_HASH_PATTERN.match(val))

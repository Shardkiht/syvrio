from __future__ import annotations

import fnmatch
import os
import posixpath
import re
import subprocess
from contextlib import contextmanager
from pathlib import Path

LOCAL_TIMEOUT = 15.0
PUSH_TIMEOUT = 120.0

SENSITIVE_PATTERNS = (
    ".env",
    "*.env",
    ".env.*",
    "*.env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "id_rsa*",
    "id_ed25519*",
    "credentials*",
    "syvrio.toml",
    "config.toml",
)

_AUTH_MARKERS = (
    "authentication failed",
    "permission denied (publickey",
    "could not read username",
    "could not read password",
    "invalid username or password",
    "terminal prompts disabled",
    "http 401",
    "http 403",
)
_NETWORK_MARKERS = (
    "could not resolve host",
    "connection timed out",
    "connection refused",
    "failed to connect",
    "unable to access",
    "network is unreachable",
    "operation timed out",
    "timed out",
)
_DIVERGED_MARKERS = (
    "non-fast-forward",
    "fetch first",
    "rejected",
    "divergent",
    "not possible to fast-forward",
    "behind its remote counterpart",
)
_NO_UPSTREAM_MARKERS = ("no upstream branch", "has no upstream branch", "no upstream configured")
_URL_CREDENTIALS = re.compile(r"(https?://)[^/\s@]+@", re.IGNORECASE)


def _physical_path(path: Path) -> str:
    """比较用路径：展开符号链接、绝对化，并在 Windows 上忽略大小写。"""
    return os.path.normcase(os.path.realpath(os.path.abspath(os.fspath(path))))


def _normalized_relative(path: str) -> str:
    candidate = posixpath.normpath(path.replace("\\", "/"))
    if candidate.startswith("./"):
        candidate = candidate[2:]
    return candidate.casefold()


def _foreign_repo_message(root: Path) -> str:
    return (
        f"Vault 位于另一个 Git 仓库（{root}）内部；为避免把父仓库文件纳入提交，"
        "已拒绝 Git 操作。请为 Vault 单独执行 git init，或将 Vault 移出该仓库。"
    )


def redact_git_message(text: str) -> str:
    """去掉 Git 错误里 URL 内嵌的用户名/令牌。"""
    return _URL_CREDENTIALS.sub(r"\1***@", text or "")


def classify_git_error(text: str) -> str:
    lowered = (text or "").lower()
    if any(marker in lowered for marker in _DIVERGED_MARKERS):
        return "diverged"
    if any(marker in lowered for marker in _AUTH_MARKERS):
        return "auth"
    if any(marker in lowered for marker in _NETWORK_MARKERS):
        return "network"
    if any(marker in lowered for marker in _NO_UPSTREAM_MARKERS):
        return "no_upstream"
    return "other"


class GitError(RuntimeError):
    def __init__(self, message: str, kind: str = "other") -> None:
        super().__init__(redact_git_message(message))
        self.kind = kind


class GitRepo:
    def __init__(self, path: Path, timeout: float = LOCAL_TIMEOUT, push_timeout: float = PUSH_TIMEOUT):
        self.path = Path(path)
        self.timeout = timeout
        self.push_timeout = push_timeout

    def _run(
        self, args: list[str], check: bool = False, timeout: float | None = None
    ) -> subprocess.CompletedProcess:
        # 后台任务绝不能停在凭据输入上；只影响本次子进程，不改用户 Git 配置。
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GCM_INTERACTIVE"] = "never"
        env.setdefault("GIT_SSH_COMMAND", "ssh -oBatchMode=yes")
        try:
            completed = subprocess.run(
                ["git", "-C", str(self.path), *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                timeout=self.timeout if timeout is None else timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise GitError(f"Git 命令超时：git {' '.join(args)}", kind="timeout") from exc
        if check and completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or "git 命令失败"
            raise GitError(detail, kind=classify_git_error(detail))
        return completed

    def repo_root(self) -> Path | None:
        """Git 工作树根目录；Vault 位于父仓库子目录时可能是父目录。"""
        if not self.path.exists():
            return None
        try:
            completed = self._run(["rev-parse", "--show-toplevel"])
        except GitError:
            return None
        raw = completed.stdout.strip()
        if completed.returncode != 0 or not raw:
            return None
        return Path(raw)

    def foreign_repo_root(self) -> Path | None:
        """Vault 本身不是仓库、但被某个父 Git 仓库包含时返回该父仓库根。"""
        root = self.repo_root()
        if root is None or _physical_path(root) == _physical_path(self.path):
            return None
        return root

    @property
    def is_repo(self) -> bool:
        """仅当 Vault 根目录本身就是 Git 工作树根时才算已初始化。"""
        root = self.repo_root()
        return root is not None and _physical_path(root) == _physical_path(self.path)

    def _require_own_repo(self) -> None:
        if self.is_repo:
            return
        foreign = self.foreign_repo_root()
        if foreign is not None:
            raise GitError(_foreign_repo_message(foreign), kind="foreign_repo")
        raise GitError("Vault 尚未初始化 Git 仓库。", kind="not_repo")

    def ensure_init(self) -> bool:
        self.path.mkdir(parents=True, exist_ok=True)
        if self.is_repo:
            return False
        foreign = self.foreign_repo_root()
        if foreign is not None:
            # 旧逻辑会把父仓库当成“已初始化”，随后 add/commit 到父仓库；
            # 这里不自动 git init，也不接管父仓库，由用户显式决定归属。
            raise GitError(_foreign_repo_message(foreign), kind="foreign_repo")
        completed = self._run(["init"], check=True)
        if completed.returncode != 0:
            raise GitError(completed.stderr.strip())
        return True

    def _identity_args(self) -> list[str]:
        name = self._run(["config", "user.name"]).stdout.strip()
        email = self._run(["config", "user.email"]).stdout.strip()
        args: list[str] = []
        if not name:
            args += ["-c", "user.name=syvrio"]
        if not email:
            args += ["-c", "user.email=syvrio@localhost"]
        return args

    def status_short(self) -> str:
        return self._run(["status", "--short"]).stdout

    def status_paths(self) -> list[str]:
        """工作区变更路径（含未跟踪文件）；重命名取新路径。"""
        completed = self._run(["status", "--porcelain", "--untracked-files=all"])
        paths: list[str] = []
        for line in completed.stdout.splitlines():
            if len(line) < 4:
                continue
            path = line[3:]
            if " -> " in path:
                path = path.split(" -> ", 1)[1]
            paths.append(path.strip().strip('"'))
        return paths

    def modified_count(self) -> int:
        return len(self.status_paths())

    def staged_paths(self) -> list[str]:
        """已进入索引的路径；`-z` 避免 Git 对空格/非 ASCII 路径加引号或转义。"""
        completed = self._run(["diff", "--cached", "--name-only", "-z"])
        return [item for item in completed.stdout.split("\x00") if item]

    @staticmethod
    def _sensitive_in(paths: list[str]) -> list[str]:
        found: list[str] = []
        seen: set[str] = set()
        for path in paths:
            relative = _normalized_relative(path)
            if relative in seen:
                continue
            seen.add(relative)
            name = posixpath.basename(relative)
            if any(
                fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(relative, pattern)
                for pattern in SENSITIVE_PATTERNS
            ):
                found.append(path)
        return found

    def sensitive_staged_paths(self) -> list[str]:
        """仅索引中即将提交的疑似凭据文件。"""
        return self._sensitive_in(self.staged_paths())

    def sensitive_paths(self) -> list[str]:
        """工作区与暂存区中疑似凭据文件；返回 Git 原始相对路径供提示。"""
        return self._sensitive_in([*self.status_paths(), *self.staged_paths()])

    def has_staged(self) -> bool:
        completed = self._run(["diff", "--cached", "--quiet"])
        return completed.returncode == 1

    def has_changes(self) -> bool:
        return bool(self.status_short().strip())

    def stage_all(self) -> None:
        self._require_own_repo()
        self._run(["add", "-A"], check=True)

    def _index_path(self) -> Path:
        result = self._run(["rev-parse", "--path-format=absolute", "--git-path", "index"], check=True)
        return Path(result.stdout.rstrip("\r\n"))

    @staticmethod
    def _restore_index(path: Path, previous: bytes | None, staged: bytes | None) -> None:
        lock = path.with_name(path.name + ".lock")
        published = False
        try:
            with lock.open("xb") as handle:
                try:
                    current = path.read_bytes() if path.exists() else None
                    if current != staged:
                        raise GitError("Git index changed during sensitive-path validation.", kind="sensitive")
                    if previous is not None:
                        handle.write(previous)
                        handle.close()
                        os.replace(lock, path)
                        published = True
                    else:
                        path.unlink(missing_ok=True)
                finally:
                    if not published:
                        lock.unlink(missing_ok=True)
        except OSError as exc:
            raise GitError(f"Could not restore the Git index: {exc}", kind="sensitive") from exc

    @contextmanager
    def stage_checked(self):
        """Restore the previous index if validation rejects our staged changes."""
        index = self._index_path()
        try:
            previous = index.read_bytes()
        except FileNotFoundError:
            previous = None
        except OSError as exc:
            raise GitError(f"Could not read the Git index: {exc}") from exc
        self.stage_all()
        try:
            staged = index.read_bytes()
        except OSError as exc:
            raise GitError(f"Could not verify the Git index: {exc}") from exc
        try:
            self._refuse_staged_sensitive()
            yield
        except GitError as exc:
            if exc.kind == "sensitive":
                self._restore_index(index, previous, staged)
            raise

    def staged_stat(self) -> str:
        return self._run(["diff", "--cached", "--stat"]).stdout

    def staged_diff(self, max_chars: int = 24000) -> str:
        diff = self._run(["diff", "--cached", "--unified=1"]).stdout
        return diff[:max_chars]

    def commit(self, message: str) -> str | None:
        self._require_own_repo()
        if not self.has_staged():
            return None
        # 最终保护：只按即将提交的索引判断，未暂存的工作区文件不影响独立提交。
        self._refuse_staged_sensitive()
        completed = self._run([*self._identity_args(), "commit", "-m", message])
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise GitError(detail, kind=classify_git_error(detail))
        return self._run(["rev-parse", "--short", "HEAD"]).stdout.strip()

    def _refuse_staged_sensitive(self) -> None:
        sensitive = self.sensitive_staged_paths()
        if sensitive:
            paths = "、".join(sensitive[:5])
            raise GitError(f"拒绝提交疑似敏感文件：{paths}。请移出 vault 或加入 .gitignore。", kind="sensitive")

    def has_commits(self) -> bool:
        return self._run(["rev-parse", "--verify", "HEAD"]).returncode == 0

    def history(self, limit: int = 10) -> list[dict[str, str]]:
        if not self.is_repo:
            return []
        completed = self._run(
            ["log", f"-{limit}", "--pretty=format:%h%x1f%ad%x1f%s", "--date=format:%Y-%m-%d %H:%M"]
        )
        result: list[dict[str, str]] = []
        for line in completed.stdout.splitlines():
            parts = line.split("\x1f")
            if len(parts) == 3:
                result.append({"hash": parts[0], "date": parts[1], "subject": parts[2]})
        return result

    def last_commit_info(self) -> dict[str, str] | None:
        if not self.has_commits():
            return None
        completed = self._run(["log", "-1", "--format=%h%x1f%cI"])
        parts = completed.stdout.strip().split("\x1f")
        if len(parts) != 2:
            return None
        return {"hash": parts[0], "at": parts[1]}

    def branch(self) -> str | None:
        completed = self._run(["rev-parse", "--abbrev-ref", "HEAD"])
        if completed.returncode != 0:
            return None
        branch = completed.stdout.strip()
        return None if not branch or branch == "HEAD" else branch

    def remote_name(self) -> str | None:
        completed = self._run(["remote"])
        names = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
        return names[0] if names else None

    def has_remote(self) -> bool:
        return self.remote_name() is not None

    def upstream(self) -> str | None:
        completed = self._run(
            ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"]
        )
        if completed.returncode != 0:
            return None
        return completed.stdout.strip() or None

    def ahead_behind(self, upstream: str | None = None) -> tuple[int, int] | None:
        """返回 (ahead, behind)；没有 upstream 或 HEAD 不可用时返回 None。"""
        target = upstream or self.upstream()
        if not target or not self.has_commits():
            return None
        completed = self._run(["rev-list", "--left-right", "--count", f"{target}...HEAD"])
        if completed.returncode != 0:
            return None
        parts = completed.stdout.split()
        if len(parts) != 2:
            return None
        try:
            behind, ahead = int(parts[0]), int(parts[1])
        except ValueError:
            return None
        return ahead, behind

    def unpushed_count(self) -> int | None:
        counts = self.ahead_behind()
        return counts[0] if counts is not None else None

    def push(self) -> str:
        self._require_own_repo()
        if not self.has_remote():
            raise GitError("未配置 git remote，无法 push。", kind="no_remote")
        upstream = self.upstream()
        if upstream is None:
            raise GitError(
                "未配置 upstream；请先运行 git push -u <remote> <branch>。",
                kind="no_upstream",
            )
        counts = self.ahead_behind(upstream)
        if counts is None:
            raise GitError("无法读取本地与远程分支的关系。", kind="other")
        ahead, behind = counts
        if ahead == 0:
            return ""
        if behind > 0:
            raise GitError(
                "远程分支已分叉，需人工处理；已停止自动推送（不执行 Pull/Merge/Force）。",
                kind="diverged",
            )
        completed = self._run(["push"], timeout=self.push_timeout)
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or "git push 失败"
            raise GitError(detail, kind=classify_git_error(detail))
        return redact_git_message(completed.stdout.strip() or completed.stderr.strip())

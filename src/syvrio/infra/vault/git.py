"""Vault 的暂存、隐私检查、提交与非强制推送规则。"""

from __future__ import annotations

import fnmatch
import posixpath
from contextlib import contextmanager

from ..system.gitops import GitError, GitRepo, classify_git_error, redact_git_message

# 根目录名使用通配模式。Git 会将字面前缀 archived/ 当作显式指定被忽略的目录，
# 导致 git add 报错退出。
ACTIVE_VAULT_PATHS = [
    "--",
    ".",
    ":(top,glob,exclude)[a]rchived/**",
    ":(top,glob,exclude)[a]rchive/**",
]

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
    "models.toml",
    "chat.toml",
    "memory.toml",
    ".syvrio-config-transaction.json",
)


def _normalized_relative(path: str) -> str:
    candidate = posixpath.normpath(path.replace("\\", "/"))
    if candidate.startswith("./"):
        candidate = candidate[2:]
    return candidate.casefold()


class VaultGitRepo(GitRepo):
    """仅托管活动 Vault 内容，敏感文件或已暂存的归档会阻止提交。"""

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
        return self._run(["status", "--short", *ACTIVE_VAULT_PATHS]).stdout

    def status_paths(self) -> list[str]:
        """工作区变更路径（含未跟踪文件）；重命名取新路径。"""
        completed = self._run(
            ["status", "--porcelain", "--untracked-files=all", *ACTIVE_VAULT_PATHS]
        )
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
        completed = self._run(["diff", "--cached", "--name-only", "-z", *ACTIVE_VAULT_PATHS])
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
        completed = self._run(["diff", "--cached", "--quiet", *ACTIVE_VAULT_PATHS])
        return completed.returncode == 1

    def has_changes(self) -> bool:
        return bool(self.status_short().strip())

    def stage_all(self) -> None:
        self._require_own_repo()
        self._run(["add", "-A", *ACTIVE_VAULT_PATHS], check=True)

    @contextmanager
    def stage_checked(self):
        """暂存变更未通过校验时恢复之前的索引。"""
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
            if exc.kind in ("sensitive", "archive_staged"):
                try:
                    self._restore_index(index, previous, staged)
                except GitError as restore_error:
                    raise GitError(str(restore_error), kind="sensitive") from restore_error
            raise

    def staged_stat(self) -> str:
        return self._run(["diff", "--cached", "--stat", *ACTIVE_VAULT_PATHS]).stdout

    def staged_diff(self) -> str:
        # 忽略已移动对话记录原路径上的删除，否则旧正文会被发给模型。
        return self._run(
            ["diff", "--cached", "--irreversible-delete", "--unified=1", *ACTIVE_VAULT_PATHS]
        ).stdout

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
        # 手工暂存的归档保留在用户索引中，并排除在托管提交之外。
        if self._run(["diff", "--cached", "--quiet", "--", "archived", "archive"]).returncode == 1:
            raise GitError(
                "Archive changes are already staged. Finish that Git operation first.",
                kind="archive_staged",
            )
        sensitive = self.sensitive_staged_paths()
        if sensitive:
            paths = "、".join(sensitive[:5])
            raise GitError(
                f"拒绝提交疑似敏感文件：{paths}。请移出 vault 或加入 .gitignore。", kind="sensitive"
            )

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

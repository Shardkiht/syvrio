"""依赖边界：Application 纯 Python，核心层不反向依赖它。"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "src" / "syvrio"
FORBIDDEN_APP = ("textual", "rich", "typer", "syvrio.app")
CORE = ("agent", "memory", "retrieval", "llm", "infra")


def _module_name(path: Path, root: Path) -> tuple[str, bool]:
    parts = list(path.relative_to(root).with_suffix("").parts)
    is_package = parts[-1] == "__init__"
    if is_package:
        parts = parts[:-1]
    return ".".join([root.name, *parts]), is_package


def _resolve_from(node: ast.ImportFrom, module: str, is_package: bool) -> str:
    if not node.level:
        return node.module or ""
    package = module.split(".") if is_package else module.split(".")[:-1]
    base = package[: max(0, len(package) - (node.level - 1))]
    parts = [*base, *((node.module or "").split(".") if node.module else [])]
    return ".".join(part for part in parts if part)


def _imports(path: Path, root: Path = ROOT) -> set[str]:
    module, is_package = _module_name(path, root)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            resolved = _resolve_from(node, module, is_package)
            if resolved:
                names.add(resolved)
                # from ..app import chat_widgets 的子模块导入也要被识别。
                names.update(f"{resolved}.{alias.name}" for alias in node.names)
    return names


def _matches(name: str, prefixes: tuple[str, ...]) -> bool:
    return any(name == prefix or name.startswith(prefix + ".") for prefix in prefixes)


def _violations(paths, forbidden: tuple[str, ...], root: Path = ROOT) -> list[str]:
    found = []
    for path in paths:
        for name in _imports(path, root):
            if _matches(name, forbidden):
                found.append(f"{path.relative_to(root)}: {name}")
    return found


def test_application_does_not_import_frontends():
    violations = _violations((ROOT / "application").rglob("*.py"), FORBIDDEN_APP)
    assert not violations, violations


def test_core_does_not_import_application():
    paths = [path for package in CORE for path in (ROOT / package).rglob("*.py")]
    violations = _violations(paths, ("syvrio.application",))
    assert not violations, violations


def test_relative_import_detection_handles_levels(tmp_path):
    root = tmp_path / "src" / "syvrio"
    files = {
        "application/mod.py": "from ..app.chat_widgets import ChatReply\n",
        "application/sub/mod.py": "from ...app.chat_widgets import ChatReply\n",
        "application/legal.py": "from ..memory.models import Entry\nimport os\nfrom .contracts import TurnResult\n",
        "agent/back.py": "from ..application.conversation import ConversationUseCases\n",
    }
    for relative, source in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")

    app_paths = [root / "application/mod.py", root / "application/sub/mod.py"]
    app_violations = [
        item.replace("\\", "/") for item in _violations(app_paths, FORBIDDEN_APP, root)
    ]
    assert any("application/mod.py" in item for item in app_violations)
    assert any("application/sub/mod.py" in item for item in app_violations)
    assert any("syvrio.app.chat_widgets" in item for item in app_violations)

    core_violations = _violations([root / "agent/back.py"], ("syvrio.application",), root)
    assert core_violations and "syvrio.application.conversation" in core_violations[0]

    assert _violations([root / "application/legal.py"], FORBIDDEN_APP, root) == []


def test_multilevel_relative_import_resolution(tmp_path):
    root = tmp_path / "src" / "syvrio"
    path = root / "application" / "sub" / "mod.py"
    path.parent.mkdir(parents=True)
    path.write_text("from ...app import chat_widgets\n", encoding="utf-8")

    assert "syvrio.app.chat_widgets" in _imports(path, root)


def test_application_settings_does_not_load_the_setup_wizard():
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
import builtins
import sys

class BlockInteractiveImports:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'questionary', 'prompt_toolkit', 'textual', 'typer'}:
            raise AssertionError(f'Unexpected interactive dependency: {fullname}')

def check_import(name, globals=None, locals=None, fromlist=(), level=0):
    caller = (globals or {}).get('__name__', '')
    if caller == 'syvrio.infra.providers' and name.split('.')[0] == 'rich':
        raise AssertionError('The settings import loaded the wizard console')
    return original_import(name, globals, locals, fromlist, level)

original_import = builtins.__import__
builtins.__import__ = check_import
sys.meta_path.insert(0, BlockInteractiveImports())
import syvrio.application.settings
""",
        ],
        check=True,
        timeout=15,
    )


def test_application_imports_without_textual_or_app():
    import importlib

    for name in (
        "syvrio.application",
        "syvrio.application.conversation",
        "syvrio.application.memory",
        "syvrio.application.settings",
        "syvrio.application.reflection",
        "syvrio.application.git",
        "syvrio.application.service",
    ):
        importlib.import_module(name)

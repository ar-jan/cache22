"""Keep bundle implementation details behind the repository facade."""

import ast
from importlib.util import resolve_name
from pathlib import Path


def test_only_storage_imports_git_bundle() -> None:
    source = Path(__file__).resolve().parents[1] / "src"
    package = source / "cache22"
    violations: list[str] = []
    for path in package.rglob("*.py"):
        if path == package / "storage.py":
            continue
        parent = ".".join(path.parent.relative_to(source).parts)
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                imports = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if node.level:
                    module = resolve_name("." * node.level + module, parent)
                imports = [module, *(f"{module}.{alias.name}" for alias in node.names)]
            else:
                continue
            if any(name == "cache22.git_bundle" for name in imports):
                violations.append(f"{path.relative_to(source)}:{node.lineno}")
    assert not violations, f"Bundle imports outside storage: {', '.join(violations)}"

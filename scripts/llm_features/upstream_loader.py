"""Load upstream Omi schema classes, prompts and constants without the backend's dependencies.

Light modules (``models/*``, prompt-constant modules) import normally from the
read-only submodule. Heavy modules (``utils/llm/chat.py`` pulls in Firestore,
Redis, ...) can't be imported in a slim container, so ``extract()`` parses the
file with ``ast`` and executes only the requested top-level definitions plus
whatever they reference (recursively, across modules). Nothing is copied: the
code always comes from the submodule as checked out.
"""

from __future__ import annotations

import ast
import importlib
import os
import sys
from pathlib import Path
from types import ModuleType

BACKEND = Path(os.getenv("UPSTREAM_BACKEND", "/upstream/backend"))
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

_cache: dict[tuple[str, str], object] = {}


def _module_file(module: str) -> Path | None:
    base = BACKEND.joinpath(*module.split("."))
    for p in (base.with_suffix(".py"), base / "__init__.py"):
        if p.is_file():
            return p
    return None


def _try_import(module: str) -> ModuleType | None:
    try:
        return importlib.import_module(module)
    except Exception:  # noqa: BLE001 - heavy upstream module; fall back to extraction
        return None


def get(module: str, name: str) -> object:
    """Return ``module.name``: real import when the module is light, AST extraction otherwise."""
    key = (module, name)
    if key in _cache:
        return _cache[key]
    mod = _try_import(module) if _is_light(module) else None
    value = getattr(mod, name) if mod is not None and hasattr(mod, name) else extract(module, [name])[name]
    _cache[key] = value
    return value


# Upstream modules that import cleanly with only pydantic installed.
_LIGHT_PREFIXES = ("models.", "utils.llm.conversation_notes_prompts")


def _is_light(module: str) -> bool:
    return module.startswith(_LIGHT_PREFIXES) or _module_file(module) is None


def _names_used(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _defined(node: ast.stmt) -> list[str]:
    if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
        return [node.name]
    if isinstance(node, ast.Assign):
        return [t.id for t in node.targets if isinstance(t, ast.Name)]
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return [node.target.id]
    return []


def extract(module: str, names: list[str], overrides: dict[str, object] | None = None) -> dict[str, object]:
    """Execute only ``names`` (and their transitive top-level dependencies) from an upstream file.

    ``overrides`` replaces module-level names (e.g. ``get_llm``, DB helpers) instead of
    resolving them, which lets a real upstream feature function run against a test client.
    """
    overrides = overrides or {}
    path = _module_file(module)
    if path is None:
        raise ImportError(f"upstream module {module} not found under {BACKEND}")
    tree = ast.parse(path.read_text(), filename=str(path))
    defs: dict[str, ast.stmt] = {}
    imports: dict[str, tuple[str | None, str, int]] = {}  # local -> (module, attr, level)
    for node in tree.body:
        for n in _defined(node):
            defs[n] = node
        if isinstance(node, (ast.Try, ast.If)):
            # Optional-import blocks (`try: from x import y / except ImportError: y = None`):
            # anything defined inside pulls in the whole block, executed as written.
            for sub in ast.walk(node):
                if isinstance(sub, ast.stmt) and sub is not node:
                    for n in _defined(sub):
                        defs.setdefault(n, node)
                    if isinstance(sub, (ast.ImportFrom, ast.Import)):
                        for a in sub.names:
                            defs.setdefault(a.asname or a.name.split(".")[0], node)
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                imports[a.asname or a.name] = (node.module, a.name, node.level)
        elif isinstance(node, ast.Import):
            for a in node.names:
                imports[a.asname or a.name.split(".")[0]] = (None, a.name, 0)

    needed_defs: set[int] = set()
    needed_imports: set[str] = set()
    todo = list(names)
    seen: set[str] = set()
    while todo:
        name = todo.pop()
        if name in seen or name in overrides:
            continue
        seen.add(name)
        if name in defs:
            node = defs[name]
            if id(node) not in needed_defs:
                needed_defs.add(id(node))
                todo.extend(_names_used(node))
        elif name in imports:
            needed_imports.add(name)

    # A real module object in sys.modules: dataclasses and pydantic look classes' modules up there.
    mod = ModuleType(f"upstream_extract.{module}.{len(_cache)}.{abs(hash(tuple(names)))}")
    sys.modules[mod.__name__] = mod
    ns: dict[str, object] = mod.__dict__
    ns.update(overrides)
    for local in needed_imports:
        mod_name, attr, level = imports[local]
        if level:  # relative import
            pkg = module.rsplit(".", level)[0]
            mod_name = f"{pkg}.{mod_name}" if mod_name else pkg
        if mod_name is None:  # plain `import x`
            ns[local] = importlib.import_module(attr) if "." not in attr else importlib.import_module(attr.split(".")[0])
        elif _module_file(mod_name) is not None:
            ns[local] = get(mod_name, attr)
        else:
            ns[local] = getattr(importlib.import_module(mod_name), attr)

    body = [n for n in tree.body if id(n) in needed_defs]
    future = [n for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == "__future__"]
    code = compile(ast.Module(body=future + body, type_ignores=[]), str(path), "exec")
    exec(code, ns)  # noqa: S102 - trusted code from the pinned submodule
    for value in ns.values():  # resolve forward refs of pydantic models defined here
        rebuild = getattr(value, "model_rebuild", None)
        if callable(rebuild) and isinstance(value, type):
            try:
                rebuild(_types_namespace=ns)
            except Exception:  # noqa: BLE001
                pass
    return {n: ns[n] for n in names}

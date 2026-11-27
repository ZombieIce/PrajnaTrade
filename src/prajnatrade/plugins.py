"""Explicit local strategy plugin registration, dependency closure, and verification."""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

PLUGIN_CONFIG_ENV = "PRAJNATRADE_PLUGIN_CONFIG"
BUILTIN_FAMILIES = frozenset(
    {"equal_weight", "momentum", "trend_filter", "inverse_volatility", "python"}
)


class PluginError(ValueError):
    """Typed diagnostic error for local plugin registration and verification."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.details = {
            "code": code,
            "message": message,
            **(dict(details) if details else {}),
        }
        super().__init__(f"{code}: {message}")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_json(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _parse_entrypoint(entrypoint: object) -> tuple[str, str]:
    if not isinstance(entrypoint, str) or not entrypoint.strip():
        raise PluginError(
            "PLUGIN_ENTRYPOINT_UNRESOLVABLE",
            "Plugin entrypoint must be a non-empty '<file_or_module>:<attr>' string.",
        )
    if ":" not in entrypoint:
        raise PluginError(
            "PLUGIN_ENTRYPOINT_UNRESOLVABLE",
            f"Plugin entrypoint {entrypoint!r} must have '<file_or_module>:<attr>' format.",
        )
    target, attr = entrypoint.rsplit(":", 1)
    target = target.strip()
    attr = attr.strip()
    if not target or not attr:
        raise PluginError(
            "PLUGIN_ENTRYPOINT_UNRESOLVABLE",
            f"Plugin entrypoint {entrypoint!r} has an empty target or attribute.",
        )
    return target, attr


def _is_path_target(target: str) -> bool:
    return target.endswith(".py") or "/" in target or "\\" in target


def _resolve_target_path(target: str, base_dir: Path) -> Path:
    if _is_path_target(target):
        candidate = Path(target)
        path = candidate.resolve() if candidate.is_absolute() else (base_dir / candidate).resolve()
        if not path.is_file() or path.suffix != ".py":
            raise PluginError(
                "PLUGIN_ENTRYPOINT_UNRESOLVABLE",
                f"Plugin entrypoint file {target!r} does not exist or is not a .py file.",
            )
        return path
    try:
        spec = importlib.util.find_spec(target)
    except (ImportError, ValueError) as error:
        raise PluginError(
            "PLUGIN_ENTRYPOINT_UNRESOLVABLE",
            f"Plugin module {target!r} could not be resolved: {error}",
        ) from error
    if spec is None or spec.origin is None:
        raise PluginError(
            "PLUGIN_ENTRYPOINT_UNRESOLVABLE",
            f"Plugin module {target!r} could not be resolved.",
        )
    if not spec.origin.endswith(".py") or not Path(spec.origin).is_file():
        raise PluginError(
            "PLUGIN_DEPENDENCY_UNBINDABLE",
            f"Plugin module {target!r} has no verifiable local .py source ({spec.origin!r}).",
        )
    return Path(spec.origin).resolve()


def _is_ignored_external_origin(origin: str) -> bool:
    if origin in {"built-in", "frozen"}:
        return True
    normalized = origin.replace("\\", "/")
    if "/site-packages/" in normalized or "/dist-packages/" in normalized:
        return True
    prajna_root = str(Path(__file__).resolve().parent).replace("\\", "/")
    return normalized.startswith(prajna_root + "/") or normalized == prajna_root


def _resolve_relative_import(
    source_path: Path, level: int, module: str | None, alias_names: Sequence[str]
) -> list[tuple[str, Path]]:
    anchor = source_path.parent
    for _ in range(level - 1):
        anchor = anchor.parent
    resolved: list[tuple[str, Path]] = []
    if module:
        rel = Path(*module.split("."))
        for candidate, mod_key in (
            (anchor / f"{rel}.py", module),
            (anchor / rel / "__init__.py", module),
        ):
            if candidate.is_file():
                resolved.append((mod_key, candidate.resolve()))
                return resolved
        raise PluginError(
            "PLUGIN_DEPENDENCY_UNBINDABLE",
            f"Relative import {'.' * level}{module} in {source_path.name} is unresolvable.",
        )
    for alias in alias_names:
        if alias == "*":
            continue
        for candidate in (anchor / f"{alias}.py", anchor / alias / "__init__.py"):
            if candidate.is_file():
                resolved.append((alias, candidate.resolve()))
                break
        else:
            raise PluginError(
                "PLUGIN_DEPENDENCY_UNBINDABLE",
                f"Relative import {'.' * level}{alias} in {source_path.name} is unresolvable.",
            )
    return resolved


def _resolve_absolute_import(
    mod_name: str, search_dirs: Sequence[Path], source_path: Path
) -> list[tuple[str, Path]]:
    top_level = mod_name.split(".", 1)[0]
    if top_level in sys.stdlib_module_names:
        return []
    if top_level == "prajnatrade":
        return []
    rel = Path(*mod_name.split("."))
    for directory in search_dirs:
        for candidate in (directory / f"{rel}.py", directory / rel / "__init__.py"):
            if candidate.is_file():
                return [(mod_name, candidate.resolve())]
    try:
        spec = importlib.util.find_spec(mod_name)
    except (ImportError, ValueError):
        spec = None
    if spec is None:
        # Check top-level package in search_dirs in case `mod_name` is `pkg.attr`
        if "." in mod_name:
            return _resolve_absolute_import(top_level, search_dirs, source_path)
        raise PluginError(
            "PLUGIN_DEPENDENCY_UNBINDABLE",
            f"Local import {mod_name!r} in {source_path.name} could not be resolved.",
        )
    if spec.origin is None:
        if spec.submodule_search_locations:
            return []
        raise PluginError(
            "PLUGIN_DEPENDENCY_UNBINDABLE",
            f"Import {mod_name!r} in {source_path.name} has no verifiable source.",
        )
    if _is_ignored_external_origin(spec.origin):
        return []
    if not spec.origin.endswith(".py") or not Path(spec.origin).is_file():
        raise PluginError(
            "PLUGIN_DEPENDENCY_UNBINDABLE",
            f"Import {mod_name!r} in {source_path.name} is not a verifiable local .py file.",
        )
    return [(mod_name, Path(spec.origin).resolve())]


def _resolve_declared_dependency(dep: object, base_dir: Path) -> tuple[str, Path]:
    if not isinstance(dep, str) or not dep.strip():
        raise PluginError(
            "PLUGIN_DEPENDENCY_UNBINDABLE",
            f"Declared dependency {dep!r} must be a non-empty path or module string.",
        )
    dep = dep.strip()
    if _is_path_target(dep) or (base_dir / dep).exists():
        candidate = Path(dep)
        path = candidate.resolve() if candidate.is_absolute() else (base_dir / candidate).resolve()
        if not path.is_file() or path.suffix != ".py":
            raise PluginError(
                "PLUGIN_DEPENDENCY_UNBINDABLE",
                f"Declared dependency {dep!r} is missing or not a .py file.",
            )
        return path.stem, path
    try:
        spec = importlib.util.find_spec(dep)
    except (ImportError, ValueError) as error:
        raise PluginError(
            "PLUGIN_DEPENDENCY_UNBINDABLE",
            f"Declared dependency {dep!r} could not be resolved: {error}",
        ) from error
    if spec is None or spec.origin is None or not spec.origin.endswith(".py"):
        raise PluginError(
            "PLUGIN_DEPENDENCY_UNBINDABLE",
            f"Declared dependency {dep!r} has no verifiable local .py source.",
        )
    path = Path(spec.origin).resolve()
    if not path.is_file():
        raise PluginError(
            "PLUGIN_DEPENDENCY_UNBINDABLE",
            f"Declared dependency {dep!r} file does not exist.",
        )
    return dep, path


def _relpath_key(path: Path, roots: Sequence[Path]) -> str:
    for root in roots:
        try:
            return path.relative_to(root).as_posix()
        except ValueError:
            continue
    return path.name


def _load_callable(entrypoint_path: Path, attr_name: str, search_dirs: Sequence[Path]) -> Callable[..., Any]:
    added: list[str] = []
    for d in reversed(search_dirs):
        s = str(d)
        if s not in sys.path:
            sys.path.insert(0, s)
            added.append(s)
    module_name = f"_prajnatrade_plugin_{_sha256_bytes(str(entrypoint_path).encode('utf-8'))[:16]}"
    try:
        spec = importlib.util.spec_from_file_location(module_name, entrypoint_path)
        if spec is None or spec.loader is None:
            raise PluginError(
                "PLUGIN_ENTRYPOINT_UNRESOLVABLE",
                f"Could not create module spec for {entrypoint_path.name}.",
            )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except PluginError:
        raise
    except Exception as error:
        raise PluginError(
            "PLUGIN_ENTRYPOINT_UNRESOLVABLE",
            f"Failed to load plugin entrypoint {entrypoint_path.name}: {error}",
        ) from error
    finally:
        for s in added:
            if s in sys.path:
                sys.path.remove(s)
    target = getattr(module, attr_name, None)
    if target is None or not callable(target):
        raise PluginError(
            "PLUGIN_ENTRYPOINT_UNRESOLVABLE",
            f"Entrypoint attribute {attr_name!r} is missing or not callable in {entrypoint_path.name}.",
        )
    return target


def analyze_plugin_entry(
    family: str,
    entrypoint: str,
    *,
    base_dir: Path,
    declared_dependencies: Sequence[str] = (),
) -> dict[str, Any]:
    """Resolve entrypoint, trace local .py dependency closure, and compute content hashes."""
    if not isinstance(family, str) or not family.strip() or family != family.strip():
        raise PluginError(
            "PLUGIN_ENTRYPOINT_UNRESOLVABLE",
            "Plugin family must be a non-empty string without outer whitespace.",
        )
    if family in BUILTIN_FAMILIES:
        raise PluginError(
            "PLUGIN_FAMILY_CONFLICT",
            f"Plugin family {family!r} collides with a built-in strategy family.",
        )
    target, attr_name = _parse_entrypoint(entrypoint)
    entrypoint_path = _resolve_target_path(target, base_dir)
    search_dirs = [entrypoint_path.parent, base_dir]

    queue: list[tuple[str, Path]] = [(entrypoint_path.stem, entrypoint_path)]
    for dep in declared_dependencies:
        queue.append(_resolve_declared_dependency(dep, base_dir))

    visited: dict[Path, dict[str, str]] = {}
    while queue:
        mod_name, dep_path = queue.pop(0)
        if dep_path in visited:
            continue
        try:
            raw_bytes = dep_path.read_bytes()
            tree = ast.parse(raw_bytes.decode("utf-8"), filename=str(dep_path))
        except (OSError, UnicodeDecodeError, SyntaxError) as error:
            raise PluginError(
                "PLUGIN_DEPENDENCY_UNBINDABLE",
                f"Cannot parse plugin dependency {dep_path.name}: {error}",
            ) from error
        visited[dep_path] = {
            "module": mod_name,
            "relpath_or_key": _relpath_key(dep_path, [base_dir, entrypoint_path.parent]),
            "sha256": _sha256_bytes(raw_bytes),
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    for resolved in _resolve_absolute_import(alias.name, search_dirs, dep_path):
                        if resolved[1] not in visited:
                            queue.append(resolved)
            elif isinstance(node, ast.ImportFrom):
                if node.level > 0:
                    for resolved in _resolve_relative_import(
                        dep_path,
                        node.level,
                        node.module,
                        [alias.name for alias in node.names],
                    ):
                        if resolved[1] not in visited:
                            queue.append(resolved)
                elif node.module:
                    for resolved in _resolve_absolute_import(node.module, search_dirs, dep_path):
                        if resolved[1] not in visited:
                            queue.append(resolved)

    _load_callable(entrypoint_path, attr_name, search_dirs)
    dependencies = sorted(
        visited.values(),
        key=lambda item: (item["relpath_or_key"], item["module"]),
    )
    entrypoint_sha256 = visited[entrypoint_path]["sha256"]
    dependencies_hash = _sha256_json(dependencies)
    canonical_entrypoint = f"{visited[entrypoint_path]['relpath_or_key']}:{attr_name}"
    plugin_id = _sha256_json(
        {
            "family": family,
            "entrypoint": canonical_entrypoint,
            "entrypoint_sha256": entrypoint_sha256,
            "dependencies_hash": dependencies_hash,
        }
    )
    return {
        "family": family,
        "plugin_id": plugin_id,
        "entrypoint": canonical_entrypoint,
        "entrypoint_sha256": entrypoint_sha256,
        "dependencies": dependencies,
        "dependencies_hash": dependencies_hash,
        "declared_dependencies": list(declared_dependencies),
        "entrypoint_path": str(entrypoint_path),
        "base_dir": str(base_dir),
    }


def public_plugin_metadata(entry: Mapping[str, Any]) -> dict[str, Any]:
    """Return the deterministic, path-free plugin identity record."""
    return {
        "plugin_id": str(entry["plugin_id"]),
        "family": str(entry["family"]),
        "entrypoint": str(entry["entrypoint"]),
        "entrypoint_sha256": str(entry["entrypoint_sha256"]),
        "dependencies": [dict(dep) for dep in entry["dependencies"]],
        "dependencies_hash": str(entry["dependencies_hash"]),
    }


def _load_config_file(config_path: Path) -> list[dict[str, Any]]:
    resolved = config_path.resolve()
    if not resolved.is_file():
        raise PluginError(
            "PLUGIN_ENTRYPOINT_UNRESOLVABLE",
            f"Plugin config file {config_path} does not exist.",
        )
    try:
        raw = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PluginError(
            "PLUGIN_ENTRYPOINT_UNRESOLVABLE",
            f"Invalid plugin config JSON in {config_path}: {error}",
        ) from error
    if not isinstance(raw, Mapping) or not isinstance(raw.get("plugins"), list):
        raise PluginError(
            "PLUGIN_ENTRYPOINT_UNRESOLVABLE",
            "Plugin config must be a JSON object with a 'plugins' list.",
        )
    entries: list[dict[str, Any]] = []
    for item in raw["plugins"]:
        if not isinstance(item, Mapping):
            raise PluginError(
                "PLUGIN_ENTRYPOINT_UNRESOLVABLE",
                "Each entry in 'plugins' must be a JSON object.",
            )
        family = item.get("family")
        entrypoint = item.get("entrypoint")
        declared = item.get("declared_dependencies", [])
        if not isinstance(declared, list):
            raise PluginError(
                "PLUGIN_DEPENDENCY_UNBINDABLE",
                "declared_dependencies must be a list of strings.",
            )
        entries.append(
            analyze_plugin_entry(
                family,
                entrypoint,
                base_dir=resolved.parent,
                declared_dependencies=declared,
            )
        )
    return entries


def load_plugin_registry(
    *,
    plugin_config: Path | str | None = None,
    plugin_specs: Sequence[str] = (),
    env: Mapping[str, str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Explicitly load local plugins from config file, env var, and --plugin flags (no dir scan)."""
    registry: dict[str, dict[str, Any]] = {}
    env_map = os.environ if env is None else env
    env_cfg = env_map.get(PLUGIN_CONFIG_ENV)
    configs: list[Path] = []
    if env_cfg and env_cfg.strip():
        configs.append(Path(env_cfg.strip()))
    if plugin_config is not None:
        configs.append(Path(plugin_config))

    for cfg in configs:
        for entry in _load_config_file(cfg):
            family = entry["family"]
            if family in registry:
                raise PluginError(
                    "PLUGIN_FAMILY_CONFLICT",
                    f"Duplicate plugin family {family!r} in plugin configuration.",
                )
            registry[family] = entry

    for spec in plugin_specs:
        if "=" not in spec:
            raise PluginError(
                "PLUGIN_ENTRYPOINT_UNRESOLVABLE",
                f"--plugin value {spec!r} must have '<family>=<file_or_module:attr>' format.",
            )
        family, entrypoint = spec.split("=", 1)
        family = family.strip()
        if family in registry:
            raise PluginError(
                "PLUGIN_FAMILY_CONFLICT",
                f"Duplicate plugin family {family!r} in --plugin arguments.",
            )
        registry[family] = analyze_plugin_entry(
            family,
            entrypoint.strip(),
            base_dir=Path.cwd(),
        )
    return registry


def resolve_plugin(
    family: str,
    registry: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Look up a registered plugin by family, failing with PLUGIN_NOT_REGISTERED if absent."""
    if registry is None or family not in registry:
        raise PluginError(
            "PLUGIN_NOT_REGISTERED",
            f"Local plugin family {family!r} is not registered.",
        )
    return dict(registry[family])


def verify_plugin_identity(
    frozen_plugin: Mapping[str, Any],
    registry: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Verify a frozen plugin identity against the operator-configured plugin on disk."""
    family = str(frozen_plugin.get("family", ""))
    entry = resolve_plugin(family, registry)
    fresh = analyze_plugin_entry(
        family,
        entry["entrypoint"],
        base_dir=Path(entry["base_dir"]),
        declared_dependencies=entry.get("declared_dependencies", ()),
    )
    if (
        fresh["plugin_id"] != frozen_plugin.get("plugin_id")
        or fresh["entrypoint_sha256"] != frozen_plugin.get("entrypoint_sha256")
        or fresh["dependencies_hash"] != frozen_plugin.get("dependencies_hash")
    ):
        raise PluginError(
            "PLUGIN_CONTENT_CHANGED",
            f"Local plugin {family!r} content changed since it was frozen.",
            details={
                "family": family,
                "expected_plugin_id": frozen_plugin.get("plugin_id"),
                "actual_plugin_id": fresh["plugin_id"],
                "expected_dependencies_hash": frozen_plugin.get("dependencies_hash"),
                "actual_dependencies_hash": fresh["dependencies_hash"],
            },
        )
    return fresh


def make_plugin_strategy(
    entry: Mapping[str, Any],
    parameters: Mapping[str, Any],
) -> Callable[..., Any]:
    """Instantiate the strategy callable from a verified local plugin entry."""
    entrypoint_path = Path(entry["entrypoint_path"])
    base_dir = Path(entry["base_dir"])
    _, attr_name = _parse_entrypoint(entry["entrypoint"])
    factory = _load_callable(entrypoint_path, attr_name, [entrypoint_path.parent, base_dir])
    strategy = factory(dict(parameters))
    if not callable(strategy):
        raise PluginError(
            "PLUGIN_ENTRYPOINT_UNRESOLVABLE",
            f"Plugin factory {entry['entrypoint']!r} did not return a callable strategy.",
        )
    return strategy

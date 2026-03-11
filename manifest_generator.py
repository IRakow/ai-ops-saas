"""
AI Ops Debugger - Codebase Manifest Generator
===============================================
Pre-generates a JSON map of the target codebase so the AI Ops pipeline
can skip the cold-index scout phase (120+ seconds) and jump straight to the
relevant module files.

Usage:
    python ai_ops_manifest_generator.py                    # current dir
    python ai_ops_manifest_generator.py /path/to/repo      # explicit repo

Exports:
    generate_manifest(repo_dir) -> dict
    generate_manifest_file(repo_dir, output_path=None) -> str
"""

import ast
import json
import os
import sys
from datetime import datetime, timezone


# Auto-discover modules from routes directory (no hardcoded list)
MODULES = None  # Set dynamically per-project

# Key utility files the pipeline frequently needs
UTILITY_PATTERNS = [
    "app/utils/*.py",
    "app/supabase_client.py",
    "app/__init__.py",
    "utils/*.py",
    "src/utils/*.py",
]

# Directories to skip when counting total files
SKIP_DIRS = {
    "__pycache__",
    ".git",
    "node_modules",
    ".venv",
    "venv",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    "env",
}


def _count_files(repo_dir: str) -> int:
    """Walk the repo and count all files, skipping irrelevant directories."""
    total = 0
    for root, dirs, files in os.walk(repo_dir):
        # Prune skipped directories in-place so os.walk does not descend
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        total += len(files)
    return total


def _safe_parse(filepath: str) -> ast.Module | None:
    """Parse a Python file into an AST, returning None on any error."""
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            source = f.read()
        return ast.parse(source, filename=filepath)
    except (SyntaxError, OSError, UnicodeDecodeError):
        return None


def _extract_routes(filepath: str) -> list[dict]:
    """
    Parse a Flask route file and extract route definitions.

    Looks for patterns like:
        @blueprint.route("/path", methods=["GET", "POST"])
        def function_name(...):

    Returns a list of dicts with keys: path, methods, function, line.
    """
    tree = _safe_parse(filepath)
    if tree is None:
        return []

    routes = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue

        for decorator in node.decorator_list:
            # Handle both @bp.route(...) and plain @route(...)
            call = None
            if isinstance(decorator, ast.Call):
                call = decorator
            else:
                continue

            # Check if this is a .route() call
            func = call.func
            is_route = False
            if isinstance(func, ast.Attribute) and func.attr == "route":
                is_route = True
            elif isinstance(func, ast.Name) and func.id == "route":
                is_route = True

            if not is_route:
                continue

            # Extract the path (first positional arg)
            path = None
            if call.args:
                arg = call.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    path = arg.value

            # Extract methods from keyword arg
            methods = ["GET"]  # Flask default
            for kw in call.keywords:
                if kw.arg == "methods" and isinstance(kw.value, ast.List):
                    methods = []
                    for elt in kw.value.elts:
                        if isinstance(elt, ast.Constant) and isinstance(
                            elt.value, str
                        ):
                            methods.append(elt.value)

            if path is not None:
                routes.append(
                    {
                        "path": path,
                        "methods": methods,
                        "function": node.name,
                        "line": node.lineno,
                    }
                )

    return routes


def _extract_service_methods(filepath: str) -> list[dict]:
    """
    Parse a service file and extract class methods.

    Looks for classes and their method definitions, extracting:
    name, args (as strings), line number.
    """
    tree = _safe_parse(filepath)
    if tree is None:
        return []

    methods = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue

        for item in node.body:
            if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue

            # Skip dunder methods except __init__
            if item.name.startswith("__") and item.name != "__init__":
                continue

            args = []
            for arg in item.args.args:
                args.append(arg.arg)

            methods.append(
                {
                    "name": item.name,
                    "args": args,
                    "line": item.lineno,
                    "class": node.name,
                }
            )

    return methods


def _list_templates(repo_dir: str, module_name: str) -> list[str]:
    """
    List template files for a given module.

    Looks in app/templates/{module_name}/ and returns paths relative
    to the templates directory (e.g., "maintenance/dashboard.html").
    """
    template_dir = os.path.join(repo_dir, "app", "templates", module_name)
    if not os.path.isdir(template_dir):
        return []

    templates = []
    for entry in sorted(os.listdir(template_dir)):
        full_path = os.path.join(template_dir, entry)
        if os.path.isfile(full_path) and entry.endswith((".html", ".htm", ".jinja2")):
            templates.append(f"{module_name}/{entry}")

    return templates


def _find_existing_utilities(repo_dir: str) -> list[str]:
    """Find utility files matching common patterns."""
    import glob
    found = []
    for pattern in UTILITY_PATTERNS:
        matches = glob.glob(os.path.join(repo_dir, pattern))
        for m in sorted(matches):
            rel = os.path.relpath(m, repo_dir)
            if os.path.isfile(m) and rel not in found:
                found.append(rel)
    return found


def _discover_modules(repo_dir: str) -> list[str]:
    """Auto-discover modules from the routes directory."""
    routes_dir = os.path.join(repo_dir, "app", "routes")
    if not os.path.isdir(routes_dir):
        # Try alternative locations
        for alt in ["routes", "src/routes", "src/api", "api"]:
            alt_dir = os.path.join(repo_dir, alt)
            if os.path.isdir(alt_dir):
                routes_dir = alt_dir
                break
        else:
            return []

    modules = []
    for fname in sorted(os.listdir(routes_dir)):
        if fname.endswith(".py") and fname != "__init__.py":
            modules.append(fname[:-3])  # Strip .py
    return modules


def _find_claude_md(repo_dir: str) -> str | None:
    """
    Look for CLAUDE.md in the repo root or one level up (the project root
    is often the parent of backend/).
    """
    candidates = [
        os.path.join(repo_dir, "CLAUDE.md"),
        os.path.join(os.path.dirname(repo_dir), "CLAUDE.md"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            return os.path.relpath(path, repo_dir)
    return None


def generate_manifest(repo_dir: str) -> dict:
    """
    Walk the repository and produce a manifest dict describing every
    module's routes, services, and templates.

    Parameters
    ----------
    repo_dir : str
        Absolute path to the backend/ directory of the project.

    Returns
    -------
    dict
        The manifest structure ready for JSON serialization.
    """
    repo_dir = os.path.abspath(repo_dir)

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo_dir": repo_dir,
        "total_files": _count_files(repo_dir),
        "modules": {},
        "extra_route_files": [],
        "utilities": _find_existing_utilities(repo_dir),
        "claude_md": _find_claude_md(repo_dir),
    }

    # ── Per-module extraction ──────────────────────────────────────────
    modules = _discover_modules(repo_dir)
    for module in modules:
        route_rel = f"app/routes/{module}.py"
        route_path = os.path.join(repo_dir, route_rel)

        service_rel = f"app/services/{module}_service.py"
        service_path = os.path.join(repo_dir, service_rel)

        module_data = {
            "route_file": route_rel if os.path.isfile(route_path) else None,
            "service_file": service_rel if os.path.isfile(service_path) else None,
            "routes": [],
            "service_methods": [],
            "templates": [],
        }

        # Routes
        if module_data["route_file"]:
            module_data["routes"] = _extract_routes(route_path)

        # Service methods
        if module_data["service_file"]:
            module_data["service_methods"] = _extract_service_methods(service_path)

        # Templates
        module_data["templates"] = _list_templates(repo_dir, module)

        manifest["modules"][module] = module_data

    # ── Extra route files not covered by the 10 modules ────────────────
    routes_dir = os.path.join(repo_dir, "app", "routes")
    if os.path.isdir(routes_dir):
        module_basenames = {f"{m}.py" for m in modules}
        for fname in sorted(os.listdir(routes_dir)):
            if (
                fname.endswith(".py")
                and fname != "__init__.py"
                and fname not in module_basenames
            ):
                manifest["extra_route_files"].append(f"app/routes/{fname}")

    return manifest


def generate_manifest_file(
    repo_dir: str, output_path: str | None = None
) -> str:
    """
    Generate the manifest and write it to a JSON file.

    Parameters
    ----------
    repo_dir : str
        Absolute path to the backend/ directory.
    output_path : str, optional
        Where to write the file.  Defaults to
        ``{repo_dir}/.ai-ops-manifest.json``.

    Returns
    -------
    str
        The absolute path of the written manifest file.
    """
    manifest = generate_manifest(repo_dir)

    if output_path is None:
        output_path = os.path.join(repo_dir, ".ai-ops-manifest.json")
    else:
        output_path = os.path.abspath(output_path)

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)

    return output_path


# ── CLI entry point ─────────────────────────────────────────────────────
def main():
    if len(sys.argv) > 1:
        repo_dir = sys.argv[1]
    else:
        repo_dir = os.getcwd()

    repo_dir = os.path.abspath(repo_dir)
    if not os.path.isdir(repo_dir):
        print(f"Error: {repo_dir} is not a directory", file=sys.stderr)
        sys.exit(1)

    output = generate_manifest_file(repo_dir)
    manifest = generate_manifest(repo_dir)

    # ── Summary ────────────────────────────────────────────────────────
    print(f"AI Ops Manifest Generator")
    print(f"=" * 50)
    print(f"Repo:          {repo_dir}")
    print(f"Total files:   {manifest['total_files']}")
    print(f"CLAUDE.md:     {manifest['claude_md'] or 'not found'}")
    print(f"Utilities:     {len(manifest['utilities'])}")
    print(f"Extra routes:  {len(manifest['extra_route_files'])}")
    print()

    total_routes = 0
    total_methods = 0
    total_templates = 0

    for name, data in manifest["modules"].items():
        r = len(data["routes"])
        m = len(data["service_methods"])
        t = len(data["templates"])
        total_routes += r
        total_methods += m
        total_templates += t

        status_parts = []
        if data["route_file"]:
            status_parts.append(f"{r} routes")
        else:
            status_parts.append("no routes file")
        if data["service_file"]:
            status_parts.append(f"{m} methods")
        else:
            status_parts.append("no service file")
        status_parts.append(f"{t} templates")

        print(f"  {name:<20s} {', '.join(status_parts)}")

    print()
    print(f"Totals: {total_routes} routes, {total_methods} service methods, {total_templates} templates")
    print(f"Written to: {output}")


if __name__ == "__main__":
    main()

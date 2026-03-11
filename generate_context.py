"""
AI Ops Debugger — Auto Context Generator
==========================================
Scans a target codebase and generates a codebase_context.md file
that agents use to understand the project.

Usage:
    python generate_context.py /path/to/project
    python generate_context.py /path/to/project --app-name "MyApp" --output context.md

Can also be imported:
    from generate_context import scan_and_generate
    content = scan_and_generate("/path/to/project", app_name="MyApp")
"""

import ast
import os
import sys
import json
import argparse
from pathlib import Path
from collections import Counter, defaultdict

SKIP_DIRS = {
    "__pycache__", ".git", "node_modules", ".venv", "venv",
    ".tox", ".mypy_cache", ".pytest_cache", "env", ".next",
    "dist", "build", ".cache", "coverage", ".eggs",
}

SKIP_EXTENSIONS = {
    ".pyc", ".pyo", ".so", ".o", ".a", ".dylib",
    ".whl", ".egg", ".tar", ".gz", ".zip",
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg",
    ".woff", ".woff2", ".ttf", ".eot",
    ".min.js", ".min.css", ".map",
}


def _walk_files(root: str) -> list[Path]:
    """Walk the project tree, skipping irrelevant directories and files."""
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for fname in filenames:
            fp = Path(dirpath) / fname
            if fp.suffix in SKIP_EXTENSIONS:
                continue
            if fname.endswith(".min.js") or fname.endswith(".min.css"):
                continue
            files.append(fp)
    return files


def _detect_stack(root: Path, files: list[Path]) -> dict:
    """Detect the tech stack from project files."""
    stack = {
        "language": [],
        "framework": [],
        "database": [],
        "hosting": [],
        "other": [],
    }

    file_names = {f.name for f in files}
    extensions = Counter(f.suffix for f in files if f.suffix)

    # Language detection
    if extensions.get(".py", 0) > 5:
        stack["language"].append("Python")
        # Check version from pyproject.toml or setup.cfg
        pyproject = root / "pyproject.toml"
        if pyproject.exists():
            content = pyproject.read_text(errors="replace")
            if "python_requires" in content:
                for line in content.splitlines():
                    if "python_requires" in line:
                        stack["language"][-1] = f"Python ({line.split('=')[-1].strip().strip('\"')})"
                        break

    if extensions.get(".js", 0) > 5 or extensions.get(".ts", 0) > 5:
        stack["language"].append("JavaScript/TypeScript")
    if extensions.get(".rb", 0) > 5:
        stack["language"].append("Ruby")
    if extensions.get(".go", 0) > 5:
        stack["language"].append("Go")
    if extensions.get(".rs", 0) > 5:
        stack["language"].append("Rust")
    if extensions.get(".java", 0) > 5:
        stack["language"].append("Java")

    # Framework detection
    requirements = root / "requirements.txt"
    if requirements.exists():
        req_text = requirements.read_text(errors="replace").lower()
        if "flask" in req_text:
            stack["framework"].append("Flask")
        if "django" in req_text:
            stack["framework"].append("Django")
        if "fastapi" in req_text:
            stack["framework"].append("FastAPI")
        if "supabase" in req_text:
            stack["database"].append("Supabase (PostgreSQL)")
        if "sqlalchemy" in req_text:
            stack["database"].append("SQLAlchemy")
        if "psycopg" in req_text:
            stack["database"].append("PostgreSQL")
        if "pymongo" in req_text:
            stack["database"].append("MongoDB")
        if "redis" in req_text:
            stack["other"].append("Redis")
        if "celery" in req_text:
            stack["other"].append("Celery")
        if "gunicorn" in req_text:
            stack["hosting"].append("Gunicorn")

    if "package.json" in file_names:
        pkg = root / "package.json"
        try:
            pkg_data = json.loads(pkg.read_text(errors="replace"))
            deps = {**pkg_data.get("dependencies", {}), **pkg_data.get("devDependencies", {})}
            if "next" in deps:
                stack["framework"].append("Next.js")
            elif "react" in deps:
                stack["framework"].append("React")
            if "vue" in deps:
                stack["framework"].append("Vue.js")
            if "express" in deps:
                stack["framework"].append("Express.js")
            if "prisma" in deps or "@prisma/client" in deps:
                stack["database"].append("Prisma")
        except (json.JSONDecodeError, OSError):
            pass

    if "Gemfile" in file_names:
        stack["framework"].append("Rails (check Gemfile)")
    if "go.mod" in file_names:
        stack["framework"].append("Go modules")

    # Template engine
    html_count = extensions.get(".html", 0)
    if html_count > 0 and "Flask" in stack["framework"]:
        stack["other"].append("Jinja2 templates")

    # Dockerfile / docker-compose
    if "Dockerfile" in file_names or "docker-compose.yml" in file_names:
        stack["hosting"].append("Docker")
    if "nginx.conf" in file_names or any(f.name == "nginx.conf" for f in files):
        stack["hosting"].append("Nginx")

    return stack


def _analyze_structure(root: Path, files: list[Path]) -> dict:
    """Analyze the project directory structure."""
    structure = defaultdict(lambda: {"count": 0, "examples": []})
    root_str = str(root)

    for f in files:
        rel = f.relative_to(root)
        parts = rel.parts

        if len(parts) >= 2:
            top_dir = parts[0]
            if parts[0] in ("app", "src", "lib", "backend", "frontend"):
                if len(parts) >= 3:
                    key = f"{parts[0]}/{parts[1]}"
                else:
                    key = parts[0]
            else:
                key = top_dir
        else:
            key = "(root)"

        structure[key]["count"] += 1
        if len(structure[key]["examples"]) < 3:
            structure[key]["examples"].append(str(rel))

    return dict(structure)


def _count_by_type(files: list[Path]) -> dict:
    """Count files by extension."""
    counts = Counter()
    for f in files:
        ext = f.suffix
        if ext:
            counts[ext] += 1
    return dict(counts.most_common(15))


def _find_routes(root: Path) -> list[str]:
    """Find Flask/Django/FastAPI route files."""
    route_dirs = [
        root / "app" / "routes",
        root / "routes",
        root / "api",
        root / "app" / "api",
        root / "src" / "routes",
        root / "src" / "api",
    ]

    route_files = []
    for d in route_dirs:
        if d.is_dir():
            for f in sorted(d.iterdir()):
                if f.suffix == ".py" and f.name != "__init__.py":
                    route_files.append(str(f.relative_to(root)))
    return route_files


def _find_services(root: Path) -> list[str]:
    """Find service files."""
    service_dirs = [
        root / "app" / "services",
        root / "services",
        root / "src" / "services",
    ]

    service_files = []
    for d in service_dirs:
        if d.is_dir():
            for f in sorted(d.iterdir()):
                if f.suffix == ".py" and f.name != "__init__.py":
                    service_files.append(str(f.relative_to(root)))
    return service_files


def _detect_patterns(root: Path, files: list[Path]) -> list[str]:
    """Detect code patterns by sampling Python files."""
    patterns = []
    py_files = [f for f in files if f.suffix == ".py"][:30]  # Sample up to 30

    decorator_counts = Counter()
    import_counts = Counter()
    base_classes = Counter()

    for f in py_files:
        try:
            source = f.read_text(errors="replace")
            tree = ast.parse(source, filename=str(f))
        except (SyntaxError, OSError):
            continue

        for node in ast.walk(tree):
            # Decorators
            if isinstance(node, ast.FunctionDef):
                for dec in node.decorator_list:
                    if isinstance(dec, ast.Name):
                        decorator_counts[f"@{dec.id}"] += 1
                    elif isinstance(dec, ast.Attribute):
                        decorator_counts[f"@...{dec.attr}"] += 1
                    elif isinstance(dec, ast.Call):
                        if isinstance(dec.func, ast.Attribute):
                            decorator_counts[f"@...{dec.func.attr}()"] += 1
                        elif isinstance(dec.func, ast.Name):
                            decorator_counts[f"@{dec.func.id}()"] += 1

            # Base classes
            if isinstance(node, ast.ClassDef) and node.bases:
                for base in node.bases:
                    if isinstance(base, ast.Name):
                        base_classes[base.id] += 1

    # Report common decorators
    common_decs = [d for d, c in decorator_counts.most_common(5) if c >= 2]
    if common_decs:
        patterns.append(f"Common decorators: {', '.join(common_decs)}")

    # Report common base classes
    common_bases = [b for b, c in base_classes.most_common(3) if c >= 2]
    if common_bases:
        patterns.append(f"Common base classes: {', '.join(common_bases)}")

    # Check for common Flask patterns
    routes_dir = root / "app" / "routes"
    if routes_dir.is_dir():
        sample = list(routes_dir.glob("*.py"))[:3]
        for f in sample:
            try:
                content = f.read_text(errors="replace")
                if "g.user" in content or "g.organization_id" in content:
                    patterns.append("Routes access Flask g object (g.user, g.organization_id)")
                    break
                if "current_user" in content:
                    patterns.append("Routes use current_user for auth context")
                    break
            except OSError:
                continue

    # Check for template inheritance
    templates_dir = root / "app" / "templates"
    if not templates_dir.is_dir():
        templates_dir = root / "templates"
    if templates_dir.is_dir():
        sample = list(templates_dir.rglob("*.html"))[:5]
        for f in sample:
            try:
                content = f.read_text(errors="replace")
                if "{% extends" in content:
                    # Extract base template name
                    for line in content.splitlines():
                        if "{% extends" in line:
                            patterns.append(f"Templates extend a base template ({line.strip()[:60]})")
                            break
                    break
            except OSError:
                continue

    return patterns


def _find_db_tables(root: Path) -> int:
    """Estimate table count from migrations or schema files."""
    count = 0

    # Check migration files
    migration_dirs = [
        root / "migrations",
        root / "alembic" / "versions",
        root / "db" / "migrate",
        root / "supabase" / "migrations",
    ]

    for d in migration_dirs:
        if d.is_dir():
            for f in d.rglob("*.sql"):
                try:
                    content = f.read_text(errors="replace").upper()
                    count += content.count("CREATE TABLE")
                except OSError:
                    continue

    # Check for Supabase tables referenced in code
    if count == 0:
        py_files = list(root.rglob("*.py"))[:100]
        tables = set()
        for f in py_files:
            try:
                content = f.read_text(errors="replace")
                # Look for .table("name") pattern
                import re
                matches = re.findall(r'\.table\(["\'](\w+)["\']', content)
                tables.update(matches)
            except OSError:
                continue
        count = len(tables)

    return count


def scan_and_generate(
    project_dir: str,
    app_name: str = "My Application",
    app_description: str = "A web application",
    app_url: str = "",
) -> str:
    """
    Scan a project directory and generate a codebase_context.md string.

    Parameters
    ----------
    project_dir : str
        Path to the root of the target project.
    app_name : str
        Name of the application.
    app_description : str
        One-line description.
    app_url : str
        Base URL of the running app.

    Returns
    -------
    str
        The markdown content for codebase_context.md.
    """
    root = Path(project_dir).resolve()
    if not root.is_dir():
        raise ValueError(f"{root} is not a directory")

    print(f"  Scanning {root}...")
    files = _walk_files(str(root))
    print(f"  Found {len(files)} files")

    stack = _detect_stack(root, files)
    structure = _analyze_structure(root, files)
    file_counts = _count_by_type(files)
    route_files = _find_routes(root)
    service_files = _find_services(root)
    patterns = _detect_patterns(root, files)
    table_count = _find_db_tables(root)

    # Build the markdown
    lines = []

    # Header
    url_part = f" ({app_url})" if app_url else ""
    lines.append(f"You are working on {app_name}{url_part}, {app_description}.")
    lines.append("")

    # Key facts
    lines.append("KEY FACTS:")

    stack_parts = []
    if stack["framework"]:
        stack_parts.append(", ".join(stack["framework"]))
    if stack["language"]:
        stack_parts.append(", ".join(stack["language"]))
    if stack["database"]:
        stack_parts.append(", ".join(stack["database"]))
    if stack["other"]:
        stack_parts.append(", ".join(stack["other"]))
    if stack_parts:
        lines.append(f"- Stack: {', '.join(stack_parts)}")

    lines.append(f"- {len(files)} total files")
    if route_files:
        lines.append(f"- {len(route_files)} route files")
    if service_files:
        lines.append(f"- {len(service_files)} service files")
    if table_count:
        lines.append(f"- ~{table_count} database tables")
    if stack["hosting"]:
        lines.append(f"- Hosting: {', '.join(stack['hosting'])}")

    lines.append("")

    # Project structure
    lines.append("PROJECT STRUCTURE:")
    sorted_dirs = sorted(structure.items(), key=lambda x: x[1]["count"], reverse=True)
    for dir_name, info in sorted_dirs[:15]:
        if dir_name == "(root)":
            continue
        lines.append(f"- {dir_name}/ — {info['count']} files")
    lines.append("")

    # Route files
    if route_files:
        lines.append("ROUTE FILES:")
        for rf in route_files[:20]:
            lines.append(f"- {rf}")
        if len(route_files) > 20:
            lines.append(f"- ... and {len(route_files) - 20} more")
        lines.append("")

    # Service files
    if service_files:
        lines.append("SERVICE FILES:")
        for sf in service_files[:20]:
            lines.append(f"- {sf}")
        if len(service_files) > 20:
            lines.append(f"- ... and {len(service_files) - 20} more")
        lines.append("")

    # Patterns
    if patterns:
        lines.append("PATTERNS:")
        for p in patterns:
            lines.append(f"- {p}")
        lines.append("")

    # File type breakdown
    lines.append("FILE TYPES:")
    for ext, count in file_counts.items():
        lines.append(f"- {ext}: {count}")
    lines.append("")

    # Placeholder for critical paths
    lines.append("CRITICAL PATHS (agents should be extra careful here):")
    lines.append("- TODO: Add your payment processing files")
    lines.append("- TODO: Add your authentication files")
    lines.append("- TODO: Add your data migration files")
    lines.append("")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Scan a codebase and generate codebase_context.md for AI Ops agents"
    )
    parser.add_argument("project_dir", help="Path to the target project")
    parser.add_argument("--app-name", default="My Application", help="Application name")
    parser.add_argument("--app-description", default="A web application", help="One-line description")
    parser.add_argument("--app-url", default="", help="Base URL of the running app")
    parser.add_argument(
        "--output", "-o",
        default="codebase_context.md",
        help="Output file path (default: codebase_context.md)"
    )

    args = parser.parse_args()

    content = scan_and_generate(
        args.project_dir,
        app_name=args.app_name,
        app_description=args.app_description,
        app_url=args.app_url,
    )

    output = Path(args.output)
    output.write_text(content)
    print(f"\nGenerated {output}")
    print(f"Lines: {len(content.splitlines())}")
    print("\nReview and edit this file — it directly affects agent quality.")


if __name__ == "__main__":
    main()

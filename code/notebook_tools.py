#!/usr/bin/env python3
"""Export marked notebook cells and focus their standard Jupyter source views.

Only ``metadata.chapter_module`` code cells are exported. Validation happens
before writing, and each changed file is replaced atomically. Multiple files
are not a single filesystem transaction.
"""

from __future__ import annotations

import argparse
import ast
import json
import keyword
import os
from pathlib import Path
import shlex
import stat
import sys
import tempfile


def _read_notebook(path):
    try:
        notebook = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError("Could not read valid UTF-8 notebook JSON.") from None
    if not isinstance(notebook, dict) or not isinstance(notebook.get("cells"), list):
        raise ValueError("Notebook must contain a cells list.")
    if not isinstance(notebook.get("metadata", {}), dict):
        raise ValueError("Notebook metadata must be an object.")
    return notebook


def _validate_module_name(name):
    if not isinstance(name, str) or not name.isidentifier() or keyword.iskeyword(name):
        raise ValueError("Module names must be valid Python identifiers.")


def _module_cells(notebook):
    modules = {}
    for cell in notebook["cells"]:
        if not isinstance(cell, dict) or not isinstance(cell.get("metadata", {}), dict):
            raise ValueError("Each cell must have valid metadata.")
        metadata = cell.get("metadata", {})
        if "chapter_module" not in metadata:
            continue
        name = metadata["chapter_module"]
        _validate_module_name(name)
        if cell.get("cell_type") != "code":
            raise ValueError("Marked module cells must be code cells.")
        if name in modules:
            raise ValueError(f"Duplicate module: {name}.")
        modules[name] = cell
    if not modules:
        raise ValueError("Notebook contains no marked modules.")
    return modules


def _validate_sources(sources):
    if not isinstance(sources, dict) or not sources:
        raise ValueError("Sources must be a nonempty module-to-source dictionary.")
    normalized = {}
    for name, source in sources.items():
        _validate_module_name(name)
        if not isinstance(source, str) or not source.strip():
            raise ValueError(f"Module {name} must contain Python source.")
        source = source.rstrip("\r\n") + "\n"
        try:
            source.encode("utf-8")
            tree = ast.parse(source, filename=f"{name}.py")
            compile(tree, filename=f"{name}.py", mode="exec")
        except (SyntaxError, ValueError, UnicodeError):
            raise ValueError(f"Module {name} is not compilable UTF-8 Python.") from None
        normalized[name] = source
    return normalized


def collect_sources(notebook_path) -> dict[str, str]:
    """Read marked modules without executing cells or expanding magic targets."""
    modules = _module_cells(_read_notebook(notebook_path))
    sources = {}
    for name, cell in modules.items():
        source = cell.get("source")
        if isinstance(source, list) and all(isinstance(line, str) for line in source):
            source = "".join(source)
        if not isinstance(source, str):
            raise ValueError(f"Module {name} has an invalid cell source.")
        lines = source.splitlines(keepends=True)
        if lines and lines[0].strip().startswith("%%writefile"):
            try:
                magic = shlex.split(lines[0])
            except ValueError:
                raise ValueError(f"Module {name} has an invalid writefile header.") from None
            if len(magic) != 2 or magic[0] != "%%writefile":
                raise ValueError("Writefile headers must contain exactly one target.")
            if Path(magic[1]).name != f"{name}.py":
                raise ValueError(f"Writefile target must match module {name}.")
            source = "".join(lines[1:])
        sources[name] = source
    return _validate_sources(sources)


def _atomic_write(path, content):
    """Replace one file, retaining existing regular-file permissions."""
    mode = 0o644
    if path.exists() and not path.is_symlink():
        mode = stat.S_IMODE(path.stat().st_mode)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.",
                                         suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(content)
        os.chmod(temporary, mode)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _export(sources, output_dir, check):
    sources = _validate_sources(sources)
    destination = Path(output_dir).expanduser().resolve()
    if destination.exists() and not destination.is_dir():
        raise ValueError("Export destination must be a directory.")
    missing, changed, unchanged = [], [], []
    for name, source in sources.items():
        path = destination / f"{name}.py"
        if path.is_symlink():
            changed.append(name)
        elif not path.exists():
            missing.append(name)
        elif path.read_bytes() == source.encode("utf-8"):
            unchanged.append(name)
        else:
            changed.append(name)
    written = []
    if not check:
        destination.mkdir(parents=True, exist_ok=True)
        for name in sources:
            if name in unchanged:
                continue
            _atomic_write(destination / f"{name}.py", sources[name].encode("utf-8"))
            written.append(name)
    return {"output_dir": str(destination), "modules": list(sources), "check": check,
            "missing": missing, "changed": changed, "unchanged": unchanged,
            "written": written, "synced": not check or not (missing or changed)}


def export_sources(sources: dict[str, str], output_dir) -> dict:
    """Export an in-memory snapshot, including edits from an unsaved notebook."""
    return _export(sources, output_dir, check=False)


def _default_output(notebook_path):
    notebook = _read_notebook(notebook_path)
    settings = notebook.get("metadata", {}).get("codex_from_scratch", {})
    if not isinstance(settings, dict):
        raise ValueError("codex_from_scratch metadata must be an object.")
    export_dir = settings.get("export_dir")
    if not isinstance(export_dir, str) or not export_dir.strip():
        raise ValueError("Notebook metadata must declare export_dir.")
    relative = Path(export_dir)
    if relative.is_absolute():
        raise ValueError("Metadata export_dir must be relative to the project root.")
    for root in Path(notebook_path).resolve().parents:
        if ((root / "code").is_dir() and (root / "notebooks").is_dir()
                and (root / "README.md").is_file()):
            destination = (root / relative).resolve()
            if not destination.is_relative_to(root):
                raise ValueError("Metadata export_dir must stay inside the project root.")
            return destination
    raise ValueError("Could not locate a project with code, notebooks, and README.md.")


def export_notebook(notebook_path, output_dir=None, check=False) -> dict:
    """Export saved source cells, or report missing/changed modules without writes."""
    sources = collect_sources(notebook_path)
    if output_dir is None:
        output_dir = _default_output(notebook_path)
    return _export(sources, output_dir, check=check)


def set_focus(notebook_path, modules=None) -> dict:
    """Show selected modules and collapse others using standard Jupyter metadata."""
    notebook = _read_notebook(notebook_path)
    cells = _module_cells(notebook)
    visible = set(cells) if modules is None else set(modules)
    for name in visible:
        _validate_module_name(name)
    if not visible.issubset(cells):
        raise ValueError("Focus includes a module absent from the notebook.")
    for cell in cells.values():
        if not isinstance(cell["metadata"].get("jupyter", {}), dict):
            raise ValueError("Module jupyter metadata must be an object.")
    changed = False
    for name, cell in cells.items():
        metadata = cell["metadata"].setdefault("jupyter", {})
        hidden = name not in visible
        if metadata.get("source_hidden") is not hidden:
            metadata["source_hidden"] = hidden
            changed = True
    if changed:
        content = json.dumps(notebook, ensure_ascii=False, indent=1) + "\n"
        _atomic_write(Path(notebook_path), content.encode("utf-8"))
    return {"visible": [name for name in cells if name in visible],
            "hidden": [name for name in cells if name not in visible], "changed": changed}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export", help="Export saved notebook modules")
    export.add_argument("notebook")
    export.add_argument("--output", type=Path)
    export.add_argument("--check", action="store_true")
    focus = commands.add_parser("focus", help="Set standard Jupyter source visibility")
    focus.add_argument("notebook")
    selection = focus.add_mutually_exclusive_group()
    selection.add_argument("--modules", help="Comma-separated visible module names")
    selection.add_argument("--all", action="store_true", help="Show every module")
    args = parser.parse_args(argv)
    try:
        if args.command == "export":
            result = export_notebook(args.notebook, args.output, args.check)
        else:
            modules = None if args.modules is None else [name.strip() for name in args.modules.split(",")]
            result = set_focus(args.notebook, modules)
    except ValueError as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2
    except OSError:
        print(json.dumps({"error": "Filesystem operation failed."}), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 1 if args.command == "export" and not result["synced"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

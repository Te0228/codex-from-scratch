"""Focused standard-library checks for notebook source export and visibility."""

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

# Support discovery from the repository root as well as from code/.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from notebook_tools import collect_sources, export_notebook, export_sources, set_focus


ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "code" / "notebook_tools.py"


def module_cell(name, source=None):
    return {"cell_type": "code", "metadata": {"chapter_module": name},
            "source": source if source is not None else f"VALUE = {name!r}\n",
            "execution_count": 1, "outputs": []}


class NotebookToolsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="notebook-tools-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "code").mkdir()
        (self.root / "notebooks").mkdir()
        (self.root / "README.md").write_text("Test project\n", encoding="utf-8")
        self.path = self.root / "notebooks" / "01-test.ipynb"
        self.output = self.root / "code" / "example"
        self.notebook = {"nbformat": 4, "nbformat_minor": 5,
                         "metadata": {"codex_from_scratch": {"export_dir": "code/example"}},
                         "cells": [module_cell("agent"), module_cell("__init__", "from .agent import VALUE\n")]}
        self.save()

    def save(self):
        self.path.write_text(json.dumps(self.notebook, ensure_ascii=False, indent=1) + "\n",
                             encoding="utf-8")

    def cli(self, *arguments):
        return subprocess.run([sys.executable, str(TOOL), *map(str, arguments)],
                              capture_output=True, text=True, check=False)

    def test_collects_only_marked_cells_preserving_order_and_source(self):
        source = "# 中文\n\nVALUE = 'kept  spaces'  \n\n\n"
        self.notebook["cells"][0]["source"] = [
            '%%writefile "$PACKAGE_DIR/agent.py"\n', source]
        self.notebook["cells"].insert(0, {"cell_type": "code", "metadata": {},
                                           "source": "raise RuntimeError('must not execute')"})
        self.save()
        sources = collect_sources(self.path)
        self.assertEqual(list(sources), ["agent", "__init__"])
        self.assertEqual(sources["agent"], source.rstrip("\n") + "\n")

    def test_exports_to_metadata_destination_and_preserves_unlisted_files(self):
        self.output.mkdir()
        extra = self.output / "keep.py"
        extra.write_text("UNRELATED = True\n")
        report = export_notebook(self.path)
        self.assertTrue(report["synced"])
        self.assertEqual(report["written"], ["agent", "__init__"])
        self.assertEqual((self.output / "agent.py").read_text(), "VALUE = 'agent'\n")
        self.assertEqual(extra.read_text(), "UNRELATED = True\n")
        self.assertEqual(sorted(path.name for path in self.output.iterdir()),
                         ["__init__.py", "agent.py", "keep.py"])

    def test_unchanged_exports_retain_mtime(self):
        export_notebook(self.path)
        times = {path: path.stat().st_mtime_ns for path in self.output.iterdir()}
        report = export_notebook(self.path)
        self.assertEqual(report["written"], [])
        self.assertEqual(report["unchanged"], ["agent", "__init__"])
        self.assertEqual(times, {path: path.stat().st_mtime_ns for path in self.output.iterdir()})

    def test_check_reports_missing_without_creating_destination(self):
        report = export_notebook(self.path, check=True)
        self.assertFalse(report["synced"])
        self.assertEqual(report["missing"], ["agent", "__init__"])
        self.assertEqual(report["written"], [])
        self.assertFalse(self.output.exists())

    def test_check_reports_changed_and_preserves_bytes_and_mtime(self):
        export_notebook(self.path)
        agent = self.output / "agent.py"
        agent.write_text("OLD = True\n")
        before = agent.stat().st_mtime_ns
        report = export_notebook(self.path, check=True)
        self.assertEqual(report["changed"], ["agent"])
        self.assertEqual(report["unchanged"], ["__init__"])
        self.assertFalse(report["synced"])
        self.assertEqual(agent.read_text(), "OLD = True\n")
        self.assertEqual(agent.stat().st_mtime_ns, before)

    def test_explicit_output_works_without_project_metadata(self):
        self.notebook["metadata"] = {}
        self.save()
        explicit = self.root / "explicit"
        report = export_notebook(self.path, explicit)
        self.assertEqual(report["output_dir"], str(explicit.resolve()))
        self.assertTrue((explicit / "agent.py").is_file())

    def test_duplicate_or_invalid_module_names_rejected_before_write(self):
        for name in ["agent", "../escape", "class", "file.py", "", None]:
            with self.subTest(name=name):
                self.notebook["cells"] = [module_cell("agent"), module_cell(name)]
                self.save()
                with self.assertRaises(ValueError):
                    export_notebook(self.path)
                self.assertFalse(self.output.exists())

    def test_magic_filename_and_options_are_validated(self):
        for header in ['%%writefile "elsewhere/model.py"', "%%writefile -a agent.py",
                       "%%writefile", '%%writefile "unclosed']:
            with self.subTest(header=header):
                self.notebook["cells"][0]["source"] = header + "\nVALUE = 1\n"
                self.save()
                with self.assertRaises(ValueError):
                    collect_sources(self.path)

    def test_invalid_later_source_prevents_all_writes(self):
        self.output.mkdir()
        agent = self.output / "agent.py"
        agent.write_text("ORIGINAL = True\n")
        for invalid in ["def broken(\n", "return 1\n", "\n\n", "\ud800"]:
            with self.subTest(invalid=repr(invalid)):
                with self.assertRaises(ValueError):
                    export_sources({"agent": "NEW = True\n", "invalid": invalid}, self.output)
                self.assertEqual(agent.read_text(), "ORIGINAL = True\n")
                self.assertFalse((self.output / "invalid.py").exists())

    def test_metadata_absolute_and_escaping_paths_are_rejected(self):
        for target in [str(self.root / "absolute"), "../escape"]:
            with self.subTest(target=target):
                self.notebook["metadata"]["codex_from_scratch"]["export_dir"] = target
                self.save()
                with self.assertRaises(ValueError):
                    export_notebook(self.path)
                self.assertFalse(self.output.exists())

    def test_metadata_symlink_escape_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="outside-notebook-project-") as outside:
            (self.root / "code" / "outside").symlink_to(outside, target_is_directory=True)
            self.notebook["metadata"]["codex_from_scratch"]["export_dir"] = "code/outside"
            self.save()
            with self.assertRaises(ValueError):
                export_notebook(self.path)
            self.assertEqual(list(Path(outside).iterdir()), [])

    def test_memory_export_uses_current_source_and_never_executes_it(self):
        marker = self.root / "executed"
        source = f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
        report = export_sources({"agent": source}, self.output)
        self.assertTrue(report["synced"])
        self.assertEqual((self.output / "agent.py").read_text(), source)
        self.assertFalse(marker.exists())
        self.assertNotEqual(collect_sources(self.path)["agent"], source)

    def test_focus_preserves_complete_sources_outputs_and_unrelated_metadata(self):
        self.notebook["cells"][0]["metadata"]["jupyter"] = {"outputs_hidden": True}
        self.notebook["cells"][0]["outputs"] = [{"output_type": "stream", "name": "stdout", "text": ["kept\n"]}]
        self.notebook["cells"].append({"cell_type": "markdown", "metadata": {"custom": True},
                                          "source": ["# Keep this\n"]})
        self.save()
        expected = deepcopy(self.notebook)
        expected["cells"][0]["metadata"]["jupyter"]["source_hidden"] = True
        expected["cells"][1]["metadata"]["jupyter"] = {"source_hidden": False}
        report = set_focus(self.path, ["__init__"])
        self.assertEqual(report["hidden"], ["agent"])
        self.assertEqual(json.loads(self.path.read_text()), expected)
        self.assertEqual(collect_sources(self.path), {"agent": "VALUE = 'agent'\n",
                                                     "__init__": "from .agent import VALUE\n"})
        set_focus(self.path)
        result = json.loads(self.path.read_text())
        self.assertTrue(all(not cell["metadata"]["jupyter"]["source_hidden"]
                            for cell in result["cells"][:2]))

    def test_focus_rejects_unknown_module_without_modifying_file(self):
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            set_focus(self.path, ["absent"])
        self.assertEqual(self.path.read_bytes(), before)

    def test_unchanged_focus_retains_mtime(self):
        set_focus(self.path)
        before = self.path.stat().st_mtime_ns
        self.assertFalse(set_focus(self.path)["changed"])
        self.assertEqual(self.path.stat().st_mtime_ns, before)

    def test_cli_export_check_and_focus_exit_codes(self):
        missing = self.cli("export", self.path, "--check")
        self.assertEqual(missing.returncode, 1, missing.stderr)
        self.assertEqual(json.loads(missing.stdout)["missing"], ["agent", "__init__"])
        self.assertFalse(self.output.exists())
        exported = self.cli("export", self.path)
        self.assertEqual(exported.returncode, 0, exported.stderr)
        checked = self.cli("export", self.path, "--check")
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertTrue(json.loads(checked.stdout)["synced"])
        focused = self.cli("focus", self.path, "--modules", "agent")
        self.assertEqual(focused.returncode, 0, focused.stderr)
        self.assertEqual(json.loads(focused.stdout)["hidden"], ["__init__"])
        expanded = self.cli("focus", self.path, "--all")
        self.assertEqual(expanded.returncode, 0, expanded.stderr)
        self.assertEqual(json.loads(expanded.stdout)["hidden"], [])

    def test_cli_errors_do_not_print_source_or_traceback(self):
        secret = "PRIVATE_SOURCE_MUST_STAY_PRIVATE"
        self.notebook["cells"][0]["source"] = f"def {secret}(\n"
        self.save()
        result = self.cli("export", self.path)
        self.assertEqual(result.returncode, 2)
        self.assertIn("error", json.loads(result.stderr))
        self.assertNotIn(secret, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertFalse(self.output.exists())

    def test_actual_first_chapter_sources_parse_and_export_to_temporary_directory(self):
        sources = collect_sources(ROOT / "notebooks" / "01-loop.ipynb")
        self.assertTrue({"executor", "verifier", "tools", "history", "model", "agent"}.issubset(sources))
        report = export_sources(sources, self.output)
        self.assertEqual(report["written"], list(sources))
        for name, source in sources.items():
            self.assertEqual((self.output / f"{name}.py").read_text(encoding="utf-8"), source)


if __name__ == "__main__":
    unittest.main()

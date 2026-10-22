"""
Semantic diff engine for El Verificador.

Compares actual AST/git changes against the DistilledWorkPackage intent to
answer: did Taskrivet build the API route, or just write `# TODO: Implement route`?

Three things this module gets right that a regex-over-the-file approach does not:

1. **Modification is detected.** Each symbol carries its real source text, so a
   function whose body was rewritten is reported as `MODIFIED` with the before
   and after snippets. A tag-only comparison (name and kind) cannot see this,
   and a rewritten function body is the single most important change in the
   whole exercise.
2. **Stubs are found structurally.** A symbol is a stub when its body is
   nothing but `pass`, `...`, `raise NotImplementedError`, or a TODO string.
   A file that merely *contains* the word TODO is not a stub.
3. **Stub findings are scoped to the diff.** A pre-existing stub that the
   agent did not touch is not the agent's violation.
"""
from __future__ import annotations

import ast
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from .schemas import (
    ASTChange,
    ChangeType,
    IntentPackage,
    SemanticDiff,
    SymbolKind,
)

# Markers that make a body a placeholder rather than an implementation.
_STUB_MARKERS = ("todo", "fixme", "not implemented", "implement me", "xxx")

# Test-weakening markers. Adding one of these to a test is the classic way an
# agent makes a failing test stop failing without implementing anything.
_WEAKENING_MARKERS = (
    "pytest.mark.skip",
    "pytest.mark.xfail",
    "unittest.skip",
    "@skip",
    "skipif",
    "pytest.skip",
)

_SKIP_RE = re.compile(r"mark\.(skip|skipif|xfail)")


@dataclass
class _FileChange:
    path: str
    status: str  # 'A', 'M', 'D', 'R'
    added: int = 0
    removed: int = 0


@dataclass
class _Symbol:
    kind: str
    name: str
    qualified: str
    line_no: int
    source: str
    is_stub: bool
    assertions: int = 0
    weakens: bool = False


class SemanticDiffEngine:
    """
    Deterministic AST-aware diff between a base commit and HEAD.

    - Lists changed files via `git diff --name-status` / `--numstat`
    - Counts added/removed lines
    - Parses Python AST of changed files at base and head
    - Detects added / modified / removed symbols with real source snippets
    - Detects structurally-stub symbols, scoped to what the diff touched
    - Detects weakened tests (deleted, skipped, or de-asserted)
    """

    def __init__(self, repo_path: str, base_commit: str, head_commit: str = "HEAD"):
        self.repo_path = Path(repo_path).resolve()
        self.base_commit = base_commit
        self.head_commit = head_commit

    # ------------------------------------------------------------------ #
    # Entry point
    # ------------------------------------------------------------------ #
    def compute(self, intent: IntentPackage) -> SemanticDiff:
        file_changes = self._changed_files()
        ast_changes: List[ASTChange] = []
        stub_symbols: List[str] = []
        changed_symbols: Set[str] = set()
        weakened_tests: List[str] = []
        deleted_files: List[str] = []

        for change in file_changes:
            if change.status == "D":
                deleted_files.append(change.path)
                continue

            head_source = self._read_file_at(self.head_commit, change.path)
            base_source = (
                self._read_file_at(self.base_commit, change.path)
                if change.status != "A"
                else ""
            )

            if not change.path.endswith(".py"):
                continue

            base_symbols = self._symbols(base_source)
            head_symbols = self._symbols(head_source)

            file_changes_for_path, stubs, weakened = self._compare_ast(
                change.path, base_symbols, head_symbols
            )
            ast_changes.extend(file_changes_for_path)
            stub_symbols.extend(stubs)
            weakened_tests.extend(weakened)

            for sym in head_symbols:
                changed_symbols.add(sym.qualified)

        return SemanticDiff(
            repo_path=str(self.repo_path),
            base_commit=self.base_commit,
            head_commit=self.head_commit,
            files_changed=[c.path for c in file_changes if c.status != "D"],
            added_lines=sum(c.added for c in file_changes),
            removed_lines=sum(c.removed for c in file_changes),
            ast_changes=ast_changes,
            intent_coverage=0.0,  # filled in by the obligation evaluator
            has_todo_stubs=bool(stub_symbols),
            stub_symbols=sorted(set(stub_symbols)),
            deleted_files=deleted_files,
            weakened_tests=sorted(set(weakened_tests)),
        )

    # ------------------------------------------------------------------ #
    # Git
    # ------------------------------------------------------------------ #
    def _changed_files(self) -> List[_FileChange]:
        """Changed files with their status, from git itself."""
        status_by_path: Dict[str, str] = {}
        status = self._git(["diff", "--name-status", f"{self.base_commit}...{self.head_commit}"])
        for line in status.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            code = parts[0]
            if code.startswith("R") and len(parts) >= 3:
                # Rename: the old path is removed, the new path is added.
                status_by_path[parts[1]] = "D"
                status_by_path[parts[2]] = "A"
            else:
                status_by_path[parts[-1]] = code[0]

        numstat = self._git(
            ["diff", "--numstat", f"{self.base_commit}...{self.head_commit}"]
        )
        counts: Dict[str, Tuple[int, int]] = {}
        for line in numstat.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            added_raw, removed_raw, path = parts[0], parts[1], parts[2]
            added = int(added_raw) if added_raw.isdigit() else 0
            removed = int(removed_raw) if removed_raw.isdigit() else 0
            counts[path] = (added, removed)

        changes: List[_FileChange] = []
        for path, code in status_by_path.items():
            added, removed = counts.get(path, (0, 0))
            changes.append(_FileChange(path=path, status=code, added=added, removed=removed))
        return changes

    def _git(self, args: List[str]) -> "subprocess.CompletedProcess[str]":
        """Run a git command and decode its output leniently.

        A diff can legitimately contain binary files — a compiled artefact, an
        image, a lockfile. Strict UTF-8 decoding turned a routine `FAIL` into
        an unhandled `UnicodeDecodeError`, so the output is decoded with
        replacement characters instead.
        """
        try:
            return subprocess.run(
                ["git"] + args,
                capture_output=True,
                text=True,
                errors="replace",
                cwd=self.repo_path,
                timeout=30,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return subprocess.CompletedProcess(args, 1, "", "")

    def _read_file_at(self, commit: str, path: str) -> str:
        result = self._git(["show", f"{commit}:{path}"])
        if result.returncode == 0:
            return str(result.stdout)
        return ""

    # ------------------------------------------------------------------ #
    # AST comparison
    # ------------------------------------------------------------------ #
    def _compare_ast(
        self,
        path: str,
        base_symbols: List[_Symbol],
        head_symbols: List[_Symbol],
    ) -> Tuple[List[ASTChange], List[str], List[str]]:
        changes: List[ASTChange] = []
        stubs: List[str] = []
        weakened: List[str] = []

        base_by_name = {s.qualified: s for s in base_symbols}
        head_by_name = {s.qualified: s for s in head_symbols}

        for sym in base_symbols:
            if sym.qualified not in head_by_name:
                changes.append(
                    ASTChange(
                        change_type=ChangeType.REMOVED,
                        symbol_kind=_symbol_kind(sym.kind),
                        name=sym.name,
                        file_path=path,
                        line_no=sym.line_no,
                        before=sym.source,
                    )
                )
                if self._is_test_symbol(sym):
                    weakened.append(f"{path}::{sym.qualified} (removed)")

        for sym in head_symbols:
            previous = base_by_name.get(sym.qualified)
            if previous is None:
                changes.append(
                    ASTChange(
                        change_type=ChangeType.ADDED,
                        symbol_kind=_symbol_kind(sym.kind),
                        name=sym.name,
                        file_path=path,
                        line_no=sym.line_no,
                        after=sym.source,
                    )
                )
            elif previous.source != sym.source:
                changes.append(
                    ASTChange(
                        change_type=ChangeType.MODIFIED,
                        symbol_kind=_symbol_kind(sym.kind),
                        name=sym.name,
                        file_path=path,
                        line_no=sym.line_no,
                        before=previous.source,
                        after=sym.source,
                    )
                )

            # Stub findings are scoped to what the diff touched: a symbol the
            # agent added or rewrote is its responsibility; a pre-existing stub
            # it never touched is not.
            if sym.is_stub and (previous is None or previous.source != sym.source):
                stubs.append(sym.qualified)

            if self._is_test_symbol(sym) and previous is not None:
                if previous.source != sym.source:
                    reason = self._weakening_reason(previous, sym)
                    if reason:
                        weakened.append(f"{path}::{sym.qualified} ({reason})")

        return changes, stubs, weakened

    # ------------------------------------------------------------------ #
    # Symbol extraction
    # ------------------------------------------------------------------ #
    def _symbols(self, source: str) -> List[_Symbol]:
        """Every function, method and class in the source, with real text."""
        if not source:
            return []
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return []

        symbols: List[_Symbol] = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                symbols.append(
                    self._make_symbol(source, node, node.name, node.name, "function")
                )
            elif isinstance(node, ast.ClassDef):
                symbols.append(self._make_symbol(source, node, node.name, node.name, "class"))
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        symbols.append(
                            self._make_symbol(
                                source, item, item.name, f"{node.name}.{item.name}", "method"
                            )
                        )
        return symbols

    def _make_symbol(
        self,
        source: str,
        node: ast.AST,
        name: str,
        qualified: str,
        kind: str,
    ) -> _Symbol:
        segment = self._segment(source, node)
        return _Symbol(
            kind=kind,
            name=name,
            qualified=qualified,
            line_no=getattr(node, "lineno", 0),
            source=segment,
            is_stub=self._is_stub(node, segment),
            assertions=self._count_assertions(node),
            weakens=self._has_weakening_marker(segment),
        )

    @staticmethod
    def _segment(source: str, node: ast.AST) -> str:
        """The source of a node, decorators included.

        `ast.get_source_segment` starts at the `def` line, so adding
        `@pytest.mark.skip` to a test produced an identical "before" and
        "after" and the weakening went unnoticed. Decorators are part of what
        changed, so they are part of the segment.
        """
        lines = source.splitlines()
        start = getattr(node, "lineno", 1)
        for decorator in getattr(node, "decorator_list", []) or []:
            start = min(start, getattr(decorator, "lineno", start))
        end = getattr(node, "end_lineno", None) or start
        return "\n".join(lines[max(0, start - 1) : end])

    @staticmethod
    def _is_stub(node: ast.AST, source: str) -> bool:
        """True when the body is a placeholder rather than an implementation."""
        body = getattr(node, "body", None)
        if not body:
            return False

        # Drop a leading docstring: a documented function is not a stub.
        statements = list(body)
        if (
            statements
            and isinstance(statements[0], ast.Expr)
            and isinstance(statements[0].value, ast.Constant)
            and isinstance(statements[0].value.value, str)
        ):
            statements = statements[1:]

        if not statements:
            # A function whose only content is a docstring is a stub.
            return True

        for statement in statements:
            if isinstance(statement, ast.Pass):
                continue
            if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant):
                # `...` (Ellipsis) or a bare TODO string.
                value = statement.value.value
                if value is Ellipsis:
                    continue
                if isinstance(value, str) and SemanticDiffEngine._is_marker_text(value):
                    continue
                return False
            if isinstance(statement, ast.Raise):
                target = statement.exc
                name = getattr(target, "id", None) or getattr(
                    getattr(target, "func", None), "id", None
                )
                if name in ("NotImplementedError", "NotImplemented"):
                    continue
                return False
            return False
        return True

    @staticmethod
    def _is_marker_text(text: str) -> bool:
        lowered = text.lower()
        return any(marker in lowered for marker in _STUB_MARKERS)

    @staticmethod
    def _has_weakening_marker(source: str) -> bool:
        return any(marker in source for marker in _WEAKENING_MARKERS) or bool(
            _SKIP_RE.search(source)
        )

    @staticmethod
    def _count_assertions(node: ast.AST) -> int:
        count = 0
        for child in ast.walk(node):
            if isinstance(child, ast.Assert):
                count += 1
        return count

    @staticmethod
    def _is_test_symbol(symbol: _Symbol) -> bool:
        name = symbol.name.lower()
        return name.startswith("test") or name.endswith("test")

    @staticmethod
    def _weakening_reason(previous: _Symbol, current: _Symbol) -> Optional[str]:
        if current.weakens and not previous.weakens:
            return "skip marker added"
        if current.assertions < previous.assertions:
            return f"assertions reduced {previous.assertions}->{current.assertions}"
        return None


def _symbol_kind(kind: str) -> SymbolKind:
    mapping = {
        "function": SymbolKind.FUNCTION,
        "method": SymbolKind.METHOD,
        "class": SymbolKind.CLASS,
        "variable": SymbolKind.VARIABLE,
        "import": SymbolKind.IMPORT,
    }
    return mapping.get(kind, SymbolKind.FUNCTION)


__all__ = ["SemanticDiffEngine"]

"""
Semantic diff engine for El Verificador.

Compares actual AST/git changes against the DistilledWorkPackage intent to
answer: did Taskrivet build the API route, or just write `# TODO: Implement route`?
"""
from __future__ import annotations

import ast
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Set, Tuple

from .schemas import (
    ASTChange,
    ChangeType,
    IntentPackage,
    SemanticDiff,
    SymbolKind,
)

STUB_PATTERNS = [
    re.compile(r"pass\s*$", re.MULTILINE),
    re.compile(r"#\s*TODO", re.IGNORECASE),
    re.compile(r"#\s*FIXME", re.IGNORECASE),
    re.compile(r"raise NotImplementedError", re.IGNORECASE),
    re.compile(r"\.\.\.", re.MULTILINE),
]


@dataclass
class _FileChange:
    path: str
    status: str  # 'A', 'M', 'D', 'R'
    added: int = 0
    removed: int = 0


class SemanticDiffEngine:
    """
    Deterministic AST-aware diff between a base commit and HEAD.

    - Lists changed files via `git diff --numstat`
    - Counts added/removed lines
    - Parses Python AST of changed files at HEAD
    - Detects TODO/pass stubs
    - Computes intent coverage against the DistilledWorkPackage true_goal
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
        todo_stubs: List[str] = []
        all_symbols: Set[str] = set()

        for change in file_changes:
            if change.status == "D":
                continue
            head_source = self._read_file_at(self.head_commit, change.path)
            base_source = (
                self._read_file_at(self.base_commit, change.path)
                if change.status != "A"
                else ""
            )

            if change.path.endswith(".py"):
                ast_changes.extend(
                    self._compare_ast(change.path, base_source, head_source)
                )
                stubs, symbols = self._scan_stubs(head_source)
                todo_stubs.extend(stubs)
                all_symbols.update(symbols)

        coverage = self._intent_coverage(intent, all_symbols, file_changes)

        return SemanticDiff(
            repo_path=str(self.repo_path),
            base_commit=self.base_commit,
            head_commit=self.head_commit,
            files_changed=[c.path for c in file_changes if c.status != "D"],
            added_lines=sum(c.added for c in file_changes),
            removed_lines=sum(c.removed for c in file_changes),
            ast_changes=ast_changes,
            intent_coverage=coverage,
            has_todo_stubs=bool(todo_stubs),
            stub_symbols=sorted(todo_stubs),
        )

    # ------------------------------------------------------------------ #
    # Git
    # ------------------------------------------------------------------ #
    def _changed_files(self) -> List[_FileChange]:
        try:
            result = subprocess.run(
                [
                    "git",
                    "diff",
                    "--numstat",
                    f"{self.base_commit}...{self.head_commit}",
                    "--",
                    ".",
                ],
                capture_output=True,
                text=True,
                cwd=self.repo_path,
                timeout=30,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return []

        changes: List[_FileChange] = []
        for line in result.stdout.splitlines():
            if not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            added_raw, removed_raw, path = parts[0], parts[1], parts[2]
            added = int(added_raw) if added_raw != "-" else 0
            removed = int(removed_raw) if removed_raw != "-" else 0
            changes.append(
                _FileChange(path=path, status="M", added=added, removed=removed)
            )
        return changes

    def _read_file_at(self, commit: str, path: str) -> str:
        try:
            result = subprocess.run(
                ["git", "show", f"{commit}:{path}"],
                capture_output=True,
                text=True,
                cwd=self.repo_path,
                timeout=15,
            )
            if result.returncode == 0:
                return result.stdout
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        return ""

    # ------------------------------------------------------------------ #
    # AST comparison
    # ------------------------------------------------------------------ #
    def _compare_ast(
        self, path: str, base_source: str, head_source: str
    ) -> List[ASTChange]:
        changes: List[ASTChange] = []

        base_tree = self._parse(base_source)
        head_tree = self._parse(head_source)

        base_symbols = self._top_level_symbols(base_tree)
        head_symbols = self._top_level_symbols(head_tree)

        base_names = {s.name for s in base_symbols}
        head_names = {s.name for s in head_symbols}

        for sym in base_symbols:
            if sym.name not in head_names:
                changes.append(
                    ASTChange(
                        change_type=ChangeType.REMOVED,
                        symbol_kind=sym.kind,
                        name=sym.name,
                        file_path=path,
                        line_no=sym.line_no,
                        before=sym.source,
                    )
                )

        for sym in head_symbols:
            if sym.name not in base_names:
                changes.append(
                    ASTChange(
                        change_type=ChangeType.ADDED,
                        symbol_kind=sym.kind,
                        name=sym.name,
                        file_path=path,
                        line_no=sym.line_no,
                        after=sym.source,
                    )
                )
            else:
                base_sym = next(s for s in base_symbols if s.name == sym.name)
                if base_sym.source != sym.source:
                    changes.append(
                        ASTChange(
                            change_type=ChangeType.MODIFIED,
                            symbol_kind=sym.kind,
                            name=sym.name,
                            file_path=path,
                            line_no=sym.line_no,
                            before=base_sym.source,
                            after=sym.source,
                        )
                    )

        return changes

    @staticmethod
    def _parse(source: str) -> Optional[ast.AST]:
        try:
            return ast.parse(source)
        except SyntaxError:
            return None

    @staticmethod
    def _top_level_symbols(tree: Optional[ast.AST]) -> List["_Symbol"]:
        if tree is None:
            return []
        symbols: List["_Symbol"] = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                symbols.append(
                    _Symbol("function", node.name, node.lineno, _node_tag(node))
                )
            elif isinstance(node, ast.ClassDef):
                symbols.append(_Symbol("class", node.name, node.lineno, _node_tag(node)))
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        symbols.append(
                            _Symbol("method", item.name, item.lineno, _node_tag(item))
                        )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    symbols.append(
                        _Symbol("import", alias.name, node.lineno, _node_tag(node))
                    )
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for alias in node.names:
                    symbols.append(
                        _Symbol(
                            "import",
                            f"{module}.{alias.name}",
                            node.lineno,
                            _node_tag(node),
                        )
                    )
        return symbols

    # ------------------------------------------------------------------ #
    # Stub detection
    # ------------------------------------------------------------------ #
    @staticmethod
    def _scan_stubs(source: str) -> Tuple[List[str], Set[str]]:
        stubs: List[str] = []
        symbols: Set[str] = set()
        for pattern in STUB_PATTERNS:
            for match in pattern.finditer(source):
                line_start = source.rfind("\n", 0, match.start()) + 1
                line_end = source.find("\n", match.end())
                if line_end == -1:
                    line_end = len(source)
                line = source[line_start:line_end].strip()
                symbol = _symbol_from_line(line)
                if symbol:
                    stubs.append(symbol)
                    symbols.add(symbol)
                else:
                    stubs.append(line[:60])
        return stubs, symbols

    # ------------------------------------------------------------------ #
    # Intent coverage
    # ------------------------------------------------------------------ #
    @staticmethod
    def _intent_coverage(
        intent: IntentPackage,
        symbols: Set[str],
        file_changes: List[_FileChange],
    ) -> float:
        if not intent.true_goal or not file_changes:
            return 0.0

        goal_words: Set[str] = set()
        goal_words |= _words(intent.true_goal)
        goal_words |= _words(intent.true_goal_summary)
        goal_words |= {s.lower() for s in symbols}

        changed_paths = {c.path for c in file_changes}
        related_touched = sum(1 for f in intent.related_files if f in changed_paths)
        related_score = (
            min(0.4, related_touched * 0.1) if intent.related_files else 0.2
        )

        matched = sum(1 for w in goal_words if w in symbols)
        symbol_score = min(0.6, matched * 0.1) if goal_words else 0.0

        return round(min(1.0, related_score + symbol_score), 4)


@dataclass
class _Symbol:
    kind: str
    name: str
    line_no: int
    source: str


def _node_tag(node: ast.AST) -> str:
    return f"{type(node).__name__}:{getattr(node, 'name', '?')}"


def _words(text: str) -> Set[str]:
    return {w.lower() for w in re.findall(r"[a-zA-Z]{3,}", text)}


def _symbol_from_line(line: str) -> Optional[str]:
    m = re.match(r"(?:async\s+)?(?:def|class)\s+([A-Za-z_]\w*)", line)
    if m:
        return m.group(1)
    m = re.match(r"(?:from\s+\S+\s+import\s+|import\s+)([A-Za-z_]\w*)", line)
    if m:
        return m.group(1)
    return None

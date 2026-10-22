"""
Semantic diff engine for El Verificador.

Compares actual AST/git changes against the DistilledWorkPackage intent to
answer: did Taskrivet build the API route, or just write `# TODO: Implement route`?

Three properties matter, and all three are testable:

1. **A rewritten function body is a change.** Symbols are compared by their
   real source segment, so a function that was edited is reported as
   ``MODIFIED`` rather than silently treated as unchanged.
2. **A stub is a stub, and prose is not.** Stub detection reads the AST and
   the token stream: a function whose whole body is ``pass``/``...``/
   ``NotImplementedError`` is a stub, and a TODO counts only when it is a
   real comment. The word "bypass" is not a ``pass`` statement, and a TODO
   inside a string literal is not a TODO.
3. **The coverage score measures the code it claims to measure.** Goal words
   are matched against the words that actually appear in the changed
   symbols, and the score is reported together with the tokens that were
   covered and the tokens that were not, so the number can be audited.
"""
from __future__ import annotations

import ast
import io
import keyword
import re
import subprocess
import tokenize
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .schemas import (
    ASTChange,
    ChangeType,
    IntentPackage,
    SemanticDiff,
)

# Maximum source retained per symbol. The correction prompt is bounded, and a
# whole module pasted into it is not evidence.
MAX_SYMBOL_SOURCE = 2000

# Words that carry no intent on their own. They are excluded from the goal
# token set so the coverage score is not diluted by grammar.
STOP_WORDS = frozenset(
    """
    a an and are as at be been but by can for from has have how in into is it
    its of on one only or our so than that the their them then there these
    they this to up was we were what when where which while who will with
    would you your all any also back better between both each even every
    here just like made make more most much no not now off out over own same
    some such take than them too very via want way well without
    """.split()
)

# A TODO/FIXME marker only counts when it is a real comment.
MARKER_PATTERN = re.compile(r"\b(TODO|FIXME|XXX)\b")

# Words that appear in almost any Python file and therefore say nothing about
# whether the intent was implemented.
CODE_NOISE = frozenset(
    set(keyword.kwlist)
    | {
        "self", "cls", "print", "len", "str", "int", "float", "bool", "type",
        "object", "super", "args", "kwargs", "value", "values", "data", "tmp",
        "temp", "none", "true", "false", "elif", "nonlocal", "assert",
    }
)

_WORD_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9]*")

# An identifier a goal names: snake_case, or a dotted path.
_NAMED_IDENTIFIER = re.compile(
    r"\b[A-Za-z_][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+\b"
    r"|\b[A-Za-z_][A-Za-z0-9]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+\b"
)


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
    line_no: int
    source: str
    qualname: str


@dataclass
class _FileFacts:
    """What one changed file contributes to the diff."""

    symbols: list[_Symbol] = field(default_factory=list)
    stub_symbols: list[str] = field(default_factory=list)
    marker_lines: list[str] = field(default_factory=list)
    tokens: set[str] = field(default_factory=set)


@dataclass
class _Coverage:
    """The coverage score and everything it was computed from."""

    score: float
    basis: str
    denominator: set[str]
    covered: set[str]
    uncovered: set[str]
    touched: set[str]
    named_identifiers: list[str]


class SemanticDiffEngine:
    """
    Deterministic AST-aware diff between a base commit and HEAD.

    - Lists changed files via `git diff --numstat`
    - Counts added/removed lines
    - Parses Python AST of changed files at HEAD
    - Detects stub functions and TODO/FIXME comments
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
        ast_changes: list[ASTChange] = []
        todo_stubs: list[str] = []
        all_symbols: set[str] = set()
        code_tokens: set[str] = set()
        marker_lines: list[str] = []

        for change in file_changes:
            if change.status == "D":
                continue
            # Only Python sources are AST-parsed. Reading every changed file
            # would (a) waste time and (b) crash on binary artifacts such as
            # compiled bytecode or images, which is not a verification
            # failure but an unhandled decoder error.
            if not change.path.endswith(".py"):
                continue

            head_source = self._read_file_at(self.head_commit, change.path)
            base_source = (
                self._read_file_at(self.base_commit, change.path)
                if change.status != "A"
                else ""
            )

            facts = self._read_facts(head_source)
            ast_changes.extend(
                self._compare_ast(change.path, base_source, head_source)
            )
            todo_stubs.extend(facts.stub_symbols)
            marker_lines.extend(facts.marker_lines)
            all_symbols.update(symbol.name for symbol in facts.symbols)
            code_tokens.update(facts.tokens)

        coverage = self._coverage(intent, code_tokens, file_changes)

        return SemanticDiff(
            repo_path=str(self.repo_path),
            base_commit=self.base_commit,
            head_commit=self.head_commit,
            files_changed=[c.path for c in file_changes if c.status != "D"],
            added_lines=sum(c.added for c in file_changes),
            removed_lines=sum(c.removed for c in file_changes),
            ast_changes=ast_changes,
            intent_coverage=coverage.score,
            has_todo_stubs=bool(todo_stubs or marker_lines),
            stub_symbols=sorted(set(todo_stubs) | set(marker_lines)),
            coverage_basis=coverage.basis,
            goal_tokens=sorted(coverage.denominator),
            covered_tokens=sorted(coverage.covered),
            uncovered_tokens=sorted(coverage.uncovered),
            related_files_touched=sorted(coverage.touched),
            named_identifiers=coverage.named_identifiers,
        )

    # ------------------------------------------------------------------ #
    # Git
    # ------------------------------------------------------------------ #
    def _changed_files(self) -> list[_FileChange]:
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
                errors="replace",
                cwd=self.repo_path,
                timeout=30,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return []

        changes: list[_FileChange] = []
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
                errors="replace",
                cwd=self.repo_path,
                timeout=15,
            )
            if result.returncode == 0:
                return result.stdout
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        return ""

    # ------------------------------------------------------------------ #
    # Facts about one file
    # ------------------------------------------------------------------ #
    def _read_facts(self, source: str) -> _FileFacts:
        """Collect symbols, stubs, markers, and word tokens from one file."""
        facts = _FileFacts()
        if not source:
            return facts

        tree = self._parse(source)
        if tree is not None:
            facts.symbols = self._top_level_symbols(tree, source)
            facts.stub_symbols = self._stub_symbols(tree)
        facts.marker_lines = self._marker_comments(source)
        for symbol in facts.symbols:
            facts.tokens.update(_words(symbol.name))
            # The body of a symbol carries the vocabulary of what was
            # actually built. A stub and an implementation share a name but
            # not their content, and the score has to tell them apart.
            facts.tokens.update(_content_tokens(symbol.source))
        return facts

    # ------------------------------------------------------------------ #
    # AST comparison
    # ------------------------------------------------------------------ #
    def _compare_ast(
        self, path: str, base_source: str, head_source: str
    ) -> list[ASTChange]:
        changes: list[ASTChange] = []

        base_symbols = self._symbols_by_key(base_source)
        head_symbols = self._symbols_by_key(head_source)

        for key, sym in base_symbols.items():
            if key not in head_symbols:
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

        for key, sym in head_symbols.items():
            if key not in base_symbols:
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
                base_sym = base_symbols[key]
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

    def _symbols_by_key(self, source: str) -> dict[tuple[str, str], _Symbol]:
        """Index a file's symbols by kind and qualified name."""
        tree = self._parse(source)
        if tree is None:
            return {}
        return {
            (symbol.kind, symbol.qualname): symbol
            for symbol in self._top_level_symbols(tree, source)
        }

    @staticmethod
    def _parse(source: str) -> ast.AST | None:
        try:
            return ast.parse(source)
        except (SyntaxError, ValueError):
            return None

    @staticmethod
    def _top_level_symbols(tree: ast.AST, source: str) -> list[_Symbol]:
        """Top-level functions, classes, their methods, and imports.

        Each symbol carries the real source segment so that a rewritten body
        is visible as a change.
        """
        symbols: list[_Symbol] = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                symbols.append(
                    _Symbol(
                        "function",
                        node.name,
                        node.lineno,
                        _source_of(source, node),
                        node.name,
                    )
                )
            elif isinstance(node, ast.ClassDef):
                symbols.append(
                    _Symbol(
                        "class",
                        node.name,
                        node.lineno,
                        _source_of(source, node),
                        node.name,
                    )
                )
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        qualname = f"{node.name}.{item.name}"
                        symbols.append(
                            _Symbol(
                                "method",
                                item.name,
                                item.lineno,
                                _source_of(source, item),
                                qualname,
                            )
                        )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    symbols.append(
                        _Symbol(
                            "import",
                            alias.name,
                            node.lineno,
                            _source_of(source, node),
                            f"import {alias.name}",
                        )
                    )
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for alias in node.names:
                    qualname = f"import {module}.{alias.name}"
                    symbols.append(
                        _Symbol(
                            "import",
                            f"{module}.{alias.name}",
                            node.lineno,
                            _source_of(source, node),
                            qualname,
                        )
                    )
        return symbols

    # ------------------------------------------------------------------ #
    # Stub detection
    # ------------------------------------------------------------------ #
    @staticmethod
    def _stub_symbols(tree: ast.AST) -> list[str]:
        """Functions, methods, and classes whose whole body is a stub.

        A body of ``pass``, ``...``, or ``raise NotImplementedError`` is a
        stub. A body that does real work is not, even if it contains the word
        "pass" inside it.
        """
        stubs: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if _is_stub_body(node.body):
                    stubs.append(node.name)
            elif isinstance(node, ast.ClassDef):
                if _is_stub_body(node.body):
                    stubs.append(node.name)
        return sorted(set(stubs))

    @staticmethod
    def _marker_comments(source: str) -> list[str]:
        """TODO/FIXME markers that are real comments.

        A marker inside a string literal is not a comment, and a marker in a
        docstring is documentation. Only comment tokens count.
        """
        markers: list[str] = []
        try:
            tokens = tokenize.generate_tokens(io.StringIO(source).readline)
            for token in tokens:
                if token.type == tokenize.COMMENT and MARKER_PATTERN.search(
                    token.string
                ):
                    markers.append(token.string.strip()[:60])
        except (tokenize.TokenError, IndentationError, SyntaxError, ValueError):
            pass
        return markers

    # ------------------------------------------------------------------ #
    # Intent coverage
    # ------------------------------------------------------------------ #
    def _coverage(
        self,
        intent: IntentPackage,
        code_tokens: set[str],
        file_changes: list[_FileChange],
    ) -> _Coverage:
        """Score intent coverage, and say how it was computed.

        The score has two parts: how much of the declared scope was touched,
        and how much of the goal the changed code actually provides.

        The second part is measured against the **identifiers the goal names**
        when it names any — ``register_user``, ``validate_payload``,
        ``app/routes/users.py``. Those are the checkable claims in the goal.
        Prose ("add", "implement", "new") is not a claim anyone can check
        against code, and counting it in the denominator would make the score
        depend on how the goal happened to be worded. A goal that names no
        identifiers falls back to its significant words, and the basis says so.
        """
        goal_words = _significant_words(intent.true_goal) | _significant_words(
            intent.true_goal_summary
        )
        # A goal word that is a Python keyword can never be covered by code:
        # it is always noise. Leaving it in the denominator would lower the
        # score of every implementation equally, for no reason.
        goal_words = {token for token in goal_words if token not in CODE_NOISE}

        identifiers = _named_identifiers(intent.true_goal) | _named_identifiers(
            intent.true_goal_summary
        )
        named = _identifier_tokens(intent.true_goal) | _identifier_tokens(
            intent.true_goal_summary
        )
        named = {token for token in named if token not in CODE_NOISE}
        if named:
            denominator = named
            basis = "identifiers named by the goal, present in the changed code"
        else:
            denominator = goal_words
            basis = "goal words present in the changed code (the goal names no identifiers)"
        covered = {token for token in denominator if token in code_tokens}
        uncovered = denominator - covered

        changed_paths = {_normalise_path(c.path) for c in file_changes}
        related = {_normalise_path(f) for f in intent.related_files}
        touched = related & changed_paths

        if related:
            file_score = len(touched) / len(related)
            token_weight, file_weight = 0.6, 0.4
            basis = f"{basis}; related files touched"
        else:
            file_score = 0.0
            token_weight, file_weight = 1.0, 0.0
            basis = f"{basis}; no related files declared"

        token_score = (len(covered) / len(denominator)) if denominator else 0.0
        score = file_weight * file_score + token_weight * token_score
        return _Coverage(
            score=round(min(1.0, max(0.0, score)), 4),
            basis=basis,
            denominator=denominator,
            covered=covered,
            uncovered=uncovered,
            touched=touched,
            named_identifiers=sorted(identifiers),
        )


# ---------------------------------------------------------------------- #
# Module helpers
# ---------------------------------------------------------------------- #
def _source_of(source: str, node: ast.AST) -> str:
    """The real source text of a node, bounded in size."""
    try:
        segment = ast.get_source_segment(source, node)
    except (ValueError, TypeError):
        segment = None
    if segment is None:
        segment = f"{type(node).__name__}:{getattr(node, 'name', '?')}"
    if len(segment) > MAX_SYMBOL_SOURCE:
        return segment[:MAX_SYMBOL_SOURCE] + "\n... [truncated]"
    return segment


def _is_stub_body(body: Sequence[ast.stmt]) -> bool:
    """True when every statement in a body is a stub marker."""
    statements = list(body)
    if statements and _is_docstring(statements[0]):
        statements = statements[1:]
    if not statements:
        return False
    return all(_is_stub_statement(statement) for statement in statements)


def _is_docstring(statement: ast.stmt) -> bool:
    return (
        isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Constant)
        and isinstance(statement.value.value, str)
    )


def _is_stub_statement(statement: ast.stmt) -> bool:
    if isinstance(statement, ast.Pass):
        return True
    if (
        isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Constant)
        and statement.value.value is Ellipsis
    ):
        return True
    if isinstance(statement, ast.Raise):
        return _is_not_implemented(statement.exc)
    return False


def _is_not_implemented(exception: ast.expr | None) -> bool:
    if exception is None:
        return False
    if isinstance(exception, ast.Name):
        return exception.id == "NotImplementedError"
    if isinstance(exception, ast.Call):
        return getattr(exception.func, "id", "") == "NotImplementedError"
    return False


def _significant_words(text: str) -> set[str]:
    """Meaningful words of a goal, reduced to a comparable stem."""
    words = _words(text)
    return {word for word in words if word not in STOP_WORDS and len(word) > 2}


def _named_identifiers(text: str) -> set[str]:
    """The identifiers a goal names, as written.

    ``register_user``, ``validate_payload``, ``app/routes/users.py`` are
    claims that can be checked against code. "Implement", "add", and "new"
    are not.
    """
    return set(_NAMED_IDENTIFIER.findall(text))


def _identifier_tokens(text: str) -> set[str]:
    """Words of the identifiers a goal names, reduced to a comparable stem."""
    tokens: set[str] = set()
    for identifier in _NAMED_IDENTIFIER.findall(text):
        for part in _split_identifier(identifier):
            stem = _stem(part)
            if len(stem) > 2 and stem not in CODE_NOISE:
                tokens.add(stem)
    return tokens


def _content_tokens(source: str) -> set[str]:
    """Words that appear in the code itself, not in its comments.

    Names and string literals count. Comments do not: a TODO comment is a
    marker of unfinished work, and counting its words as evidence of intent
    would let a stub score like an implementation.
    """
    tokens: set[str] = set()
    try:
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type == tokenize.NAME:
                text = token.string
            elif token.type == tokenize.STRING:
                text = _string_content(token.string)
            else:
                continue
            for part in _split_identifier(text):
                stem = _stem(part)
                if len(stem) > 2 and stem not in CODE_NOISE:
                    tokens.add(stem)
    except (tokenize.TokenError, IndentationError, SyntaxError, ValueError):
        pass
    return tokens


def _string_content(literal: str) -> str:
    """The text inside a string literal, without its quotes."""
    text = literal.strip()
    for quote in ('"""', "'''", '"', "'"):
        if text.startswith(quote) and text.endswith(quote) and len(text) >= 2 * len(quote):
            return text[len(quote) : -len(quote)]
    return text


def _words(text: str) -> set[str]:
    """Lowercase, stemmed words of an identifier or a sentence."""
    result: set[str] = set()
    for match in _WORD_PATTERN.findall(text):
        for part in _split_identifier(match):
            stem = _stem(part)
            if len(stem) > 2:
                result.add(stem)
    return result


def _split_identifier(identifier: str) -> list[str]:
    """Split ``create_search_route`` and ``createSearchRoute`` into words."""
    parts = re.split(r"[^A-Za-z0-9]+", identifier)
    words: list[str] = []
    for part in parts:
        if not part:
            continue
        words.extend(re.findall(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+", part))
    return [word.lower() for word in words if word]


def _stem(word: str) -> str:
    """A small, deterministic stemmer.

    Not linguistics: just enough that "results", "return" and "returning"
    compare as the same idea, so a goal sentence can be matched against code.
    """
    for suffix, replacement in (
        ("ingly", ""),
        ("ing", ""),
        ("edly", ""),
        ("ed", ""),
        ("ies", "y"),
        ("sses", "ss"),
        ("es", ""),
        ("s", ""),
    ):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)] + replacement
    return word


def _normalise_path(path: str) -> str:
    """Compare paths the same way the work package and git spell them."""
    cleaned = path.strip().replace("\\", "/")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return cleaned.lstrip("/")

"""
El Verificador - autonomous gatekeeper for the Taskrivet pipeline.

Public API:

    from el_verificador import ElVerificador, VerificationConfig
    from el_verificador.schemas import IntentPackage, VerificationResult

    verifier = ElVerificador(VerificationConfig(dry_run=True))
    result = verifier.verify(work_package, repo_path, base_commit, head_commit)
"""

from .engine import ElVerificador, VerifierDependencies
from .schemas import (
    ASTChange,
    ChangeType,
    CheckKind,
    CheckResult,
    CorrectionPrompt,
    EdgeRecordLogger,
    IntentPackage,
    SemanticDiff,
    SymbolKind,
    VerificationAttempt,
    VerificationAudit,
    VerificationConfig,
    VerificationDecision,
    VerificationResult,
)
from .semantic_diff import SemanticDiffEngine

__all__ = [
    "ElVerificador",
    "VerifierDependencies",
    "ASTChange",
    "ChangeType",
    "CheckKind",
    "CheckResult",
    "CorrectionPrompt",
    "EdgeRecordLogger",
    "IntentPackage",
    "SemanticDiff",
    "SymbolKind",
    "VerificationAttempt",
    "VerificationAudit",
    "VerificationConfig",
    "VerificationDecision",
    "VerificationResult",
    "SemanticDiffEngine",
]

__version__ = "0.1.0"

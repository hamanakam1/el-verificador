"""
El Verificador - autonomous gatekeeper for the Taskrivet pipeline.

Public API:

    from el_verificador import ElVerificador, VerificationConfig
    from el_verificador.schemas import IntentPackage, VerificationResult
    from el_verificador.obligations import decide

    verifier = ElVerificador(VerificationConfig(dry_run=True))
    result = verifier.verify(work_package, repo_path, base_commit, head_commit)
"""

from .acceptance import AcceptanceContract, AcceptanceSource
from .engine import ElVerificador, VerifierDependencies
from .evidence import EvidenceArtifact, EvidenceKind, EvidenceStore
from .obligations import (
    Obligation,
    ObligationEvaluator,
    ObligationKind,
    ObligationStatus,
    decide,
)
from .receipt import ReceiptChain, build_receipt, fingerprint
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
from .ticket_board import FlagOutcome, TicketBoardAdapter

__all__ = [
    "AcceptanceContract",
    "AcceptanceSource",
    "ElVerificador",
    "EvidenceArtifact",
    "EvidenceKind",
    "EvidenceStore",
    "FlagOutcome",
    "Obligation",
    "ObligationEvaluator",
    "ObligationKind",
    "ObligationStatus",
    "ReceiptChain",
    "TicketBoardAdapter",
    "VerifierDependencies",
    "build_receipt",
    "decide",
    "fingerprint",
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

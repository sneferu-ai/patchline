"""FR-044 language pack protocol."""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Protocol


@dataclass
class SyntaxResult:
    valid: bool
    error: Optional[str] = None
    lineno: Optional[int] = None
    offset: Optional[int] = None
    text: Optional[str] = None


class LanguagePack(Protocol):
    """Language packs validate SYNTAX only (FR-044).

    parse_syntax does NOT perform AST equivalence checking, type checking,
    test execution, semantic validation, or runtime behavior verification.
    A 'valid' result does not guarantee the code is correct.
    """

    def language_name(self) -> str:
        """Return the language identifier (e.g., 'python', 'go')."""
        ...

    def file_extensions(self) -> List[str]:
        """Return file extensions this pack handles (e.g., ['.py', '.pyw'])."""
        ...

    def parse_syntax(self, source: str) -> SyntaxResult:
        """Parse source for syntax validity ONLY.

        error/lineno/offset/text are populated from the underlying
        SyntaxError exception when available.
        """
        ...

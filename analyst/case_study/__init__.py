"""Case-specific, HTTP-only analysis helper callable from an analyst tool.

The generic agent decides when this helper is useful. Importing this package
never fetches data or writes artifacts. It does not depend on a web dashboard.
"""

from .pipeline import execute_case_study, run_case_study

__all__ = ["execute_case_study", "run_case_study"]

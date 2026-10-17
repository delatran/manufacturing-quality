"""Command-line entry point for an explicitly requested historical case run."""

import argparse
import json
from pathlib import Path

from .pipeline import execute_case_study

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
result = execute_case_study(args.output)
print(
    json.dumps(
        {"qa": result["qa"], "kpis": result["analysis"]["kpis"], "artifacts": result["artifacts"]},
        indent=2,
    )
)

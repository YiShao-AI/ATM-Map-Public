#!/usr/bin/env python3
"""Verify the recorded runner and selected function fingerprints.

The public repository is intentionally a clean snapshot, so its commit graph
does not contain the private run revision. The evidence record therefore keeps
the exact runner-file hash and selected syntax-tree fingerprints for functions
that made, paginated, budgeted, normalized, and deduplicated provider requests.
This is a limited source-comparability check, not a proof of the complete
dependency closure, private inputs, provider responses, or original commit.
"""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
EVIDENCE = ROOT / "benchmarking" / "evidence" / "market-run-20260904.json"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def function_fingerprints(path: Path, names: set[str]) -> dict[str, str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    nodes = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    missing = names - nodes.keys()
    if missing:
        raise SystemExit("Missing recorded functions: " + ", ".join(sorted(missing)))
    return {
        name: hashlib.sha256(
            ast.dump(nodes[name], include_attributes=False).encode("utf-8")
        ).hexdigest()
        for name in sorted(names)
    }


def main() -> None:
    record = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    provenance = record["source_provenance"]
    expected_runner = provenance["run_source_files_sha256"][
        "benchmarking/run_market_evidence.py"
    ]
    runner = ROOT / "benchmarking" / "run_market_evidence.py"
    if sha256(runner) != expected_runner:
        raise SystemExit("Market evidence runner no longer matches the recorded run")

    expected_functions = provenance["run_critical_function_ast_sha256"]
    actual_functions = function_fingerprints(ROOT / "proxy.py", set(expected_functions))
    mismatches = [
        name for name, expected in expected_functions.items()
        if actual_functions.get(name) != expected
    ]
    if mismatches:
        raise SystemExit(
            "Selected source functions changed since the recorded run: "
            + ", ".join(sorted(mismatches))
        )

    print(
        "PASS: evidence runner and "
        f"{len(expected_functions)} selected source fingerprints match"
    )


if __name__ == "__main__":
    main()

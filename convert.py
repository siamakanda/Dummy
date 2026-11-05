"""Convert a carrier rate file into our template, validate it, and write the result (AGENTS.md M2).

    python convert.py <file> [-c trunk.yaml] [-o out.csv] [--previous prev.csv]

Errors block the output (exit 1); warnings are printed and still allow the file to be written.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from cleaner import read_clean
from mapper import to_template
from validator import has_errors, validate


def load_trunk_config(path: str | None) -> dict:
    """Load an optional YAML trunk config (defaults + header_aliases)."""
    if not path:
        return {}
    with open(path, "r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Convert a carrier rate file to the Dummy template.")
    parser.add_argument("file", help="carrier file (.csv / .xlsx / .zip)")
    parser.add_argument("-c", "--trunk-config", help="optional YAML trunk config")
    parser.add_argument("-o", "--out", help="output CSV path (default <file>.converted.csv)")
    parser.add_argument("--previous", help="a previously converted deck to compare against")
    parser.add_argument("--rate-change-pct", type=float, default=20.0,
                        help="warn when many prefixes change by more than this percent")
    args = parser.parse_args(argv)

    config = load_trunk_config(args.trunk_config)
    result = to_template(read_clean(args.file), config)

    previous = None
    if args.previous:
        previous = to_template(read_clean(args.previous), config)

    issues = validate(result, previous=previous,
                      overrides={"rate_change_pct": args.rate_change_pct})
    for issue in issues:
        print(issue)

    if has_errors(issues):
        print(f"\n{sum(i.level == 'error' for i in issues)} error(s); output NOT written.",
              file=sys.stderr)
        return 1

    out_path = Path(args.out) if args.out else Path(args.file).with_suffix(".converted.csv")
    result.to_csv(out_path, index=False, encoding="utf-8")
    print(f"\nWrote {len(result)} rows -> {out_path}")
    if issues:
        print(f"{len(issues)} warning(s); human approval required before upload.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

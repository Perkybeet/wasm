"""CLI module for WASM."""

from wasm.cli.interactive import InteractiveMode
from wasm.cli.parser import create_parser, parse_args

__all__ = ["InteractiveMode", "create_parser", "parse_args"]

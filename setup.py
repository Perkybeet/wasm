#!/usr/bin/env python3
"""
Minimal setup.py for backwards compatibility.

This file exists for compatibility with older build systems (like Ubuntu Jammy)
that don't fully support pyproject.toml-only builds.

All configuration is in pyproject.toml - this just provides a fallback entry point.
"""
from setuptools import setup

if __name__ == "__main__":
    setup()

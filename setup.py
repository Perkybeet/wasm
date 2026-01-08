#!/usr/bin/env python3
"""
setup.py for backwards compatibility with Ubuntu Jammy (22.04).

This file exists because Jammy's older pybuild/setuptools don't fully support
pyproject.toml-only builds. We explicitly provide the essential metadata here.
"""
from setuptools import setup, find_packages

setup(
    name="wasm-cli",
    version="0.13.2",
    description="Web App System Management - Deploy and manage web applications on Linux servers",
    author="Yago López Prado",
    author_email="yago.lopez.adeje@gmail.com",
    license="WASM-NCSAL",
    python_requires=">=3.10",
    package_dir={"": "src"},
    packages=find_packages(where="src"),
    include_package_data=True,
    install_requires=[
        "inquirer>=3.1.0",
        "Jinja2>=3.1.0",
        "PyYAML>=6.0",
    ],
    entry_points={
        "console_scripts": [
            "wasm=wasm.main:main",
        ],
    },
    classifiers=[
        "Development Status :: 4 - Beta",
        "Environment :: Console",
        "Intended Audience :: Developers",
        "Intended Audience :: System Administrators",
        "License :: Other/Proprietary License",
        "Operating System :: POSIX :: Linux",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
    ],
)

# Copyright (c) 2024-2025 Yago López Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Environment variable manager for WASM.

Handles discovering, prompting, and writing environment variables
for deployed applications, with support for .env.example parsing,
secret auto-generation, and interactive configuration.
"""

import json
import secrets
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Optional, Any

from wasm.core.exceptions import WASMError
from wasm.core.logger import Logger


class EnvConfigError(WASMError):
    """Raised when environment configuration fails."""
    pass


@dataclass
class EnvVariable:
    """Represents a single environment variable."""
    name: str
    default: str = ""
    description: str = ""
    category: str = "General"
    required: bool = False
    secret: bool = False
    shared: bool = False
    value: Optional[str] = None


@dataclass
class EnvConfig:
    """Environment configuration for an application."""
    variables: List[EnvVariable] = field(default_factory=list)
    files: Dict[str, List[str]] = field(default_factory=dict)  # filename -> variable names

    def to_dict(self) -> Dict[str, Any]:
        """
        Serialize to dictionary.

        Returns:
            Dictionary representation.
        """
        return {
            "variables": [asdict(v) for v in self.variables],
            "files": self.files,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "EnvConfig":
        """
        Deserialize from dictionary.

        Args:
            data: Dictionary with variables and files keys.

        Returns:
            EnvConfig instance.
        """
        variables = [EnvVariable(**v) for v in data.get("variables", [])]
        return cls(variables=variables, files=data.get("files", {}))


class EnvManager:
    """
    Manager for application environment variables.

    Discovers variables from .env.example files, prompts users
    for values, auto-generates secrets, and writes .env files.
    """

    # Category detection by prefix
    CATEGORY_PREFIXES = {
        "DATABASE": "Database",
        "DB_": "Database",
        "POSTGRES": "Database",
        "MYSQL": "Database",
        "MONGO": "Database",
        "REDIS": "Redis",
        "JWT": "Authentication",
        "AUTH": "Authentication",
        "SESSION": "Authentication",
        "OAUTH": "Authentication",
        "SMTP": "Email",
        "MAIL": "Email",
        "EMAIL": "Email",
        "AWS": "Cloud",
        "S3_": "Cloud",
        "GCP": "Cloud",
        "AZURE": "Cloud",
        "SENTRY": "Monitoring",
        "LOG": "Logging",
        "API": "API",
        "PORT": "Server",
        "HOST": "Server",
        "NODE_ENV": "Server",
        "APP_": "Application",
        "NEXT_PUBLIC": "Frontend",
        "VITE_": "Frontend",
        "ENCRYPTION": "Security",
        "CORS": "Security",
    }

    # Secret detection patterns
    SECRET_PATTERNS = [
        "PASSWORD", "_PASS", "SECRET", "TOKEN", "API_KEY", "PRIVATE_KEY",
        "ENCRYPTION_KEY", "SIGNING_KEY", "ACCESS_KEY", "SECRET_KEY",
        "CLIENT_SECRET", "WEBHOOK_SECRET",
    ]

    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self.logger = Logger(verbose=verbose)

    def discover(self, app_path: Path) -> List[EnvVariable]:
        """
        Discover environment variables from .env.example files.

        Scans root and subdirectories (apps/*, packages/*) for
        .env.example files.

        Args:
            app_path: Path to the application root.

        Returns:
            List of discovered environment variables.
        """
        variables = []
        seen_names = set()

        # Search paths: root, then subdirectories
        search_paths = [app_path]
        for subdir in ["apps", "packages", "services"]:
            sub_path = app_path / subdir
            if sub_path.is_dir():
                for child in sorted(sub_path.iterdir()):
                    if child.is_dir():
                        search_paths.append(child)

        for search_path in search_paths:
            for env_example in sorted(search_path.glob(".env.example")):
                parsed = self._parse_env_example(env_example)
                for var in parsed:
                    if var.name not in seen_names:
                        variables.append(var)
                        seen_names.add(var.name)

            # Also check .env.template and .env.sample
            for pattern in [".env.template", ".env.sample"]:
                for env_file in sorted(search_path.glob(pattern)):
                    parsed = self._parse_env_example(env_file)
                    for var in parsed:
                        if var.name not in seen_names:
                            variables.append(var)
                            seen_names.add(var.name)

        return variables

    def _parse_env_example(self, path: Path) -> List[EnvVariable]:
        """
        Parse a .env.example file into EnvVariable objects.

        Supports comment-based descriptions and metadata:
            # Comment becomes description
            KEY=default_value
            # Required: true
            REQUIRED_KEY=

        Args:
            path: Path to the .env.example file.

        Returns:
            List of parsed environment variables.
        """
        variables = []
        current_description = ""

        try:
            content = path.read_text(encoding="utf-8")
        except OSError as e:
            self.logger.warning(f"Could not read {path}: {e}")
            return variables

        for line in content.splitlines():
            line = line.strip()

            # Skip empty lines
            if not line:
                current_description = ""
                continue

            # Collect comments as descriptions
            if line.startswith("#"):
                comment = line.lstrip("#").strip()
                if current_description:
                    current_description += " " + comment
                else:
                    current_description = comment
                continue

            # Parse KEY=VALUE
            if "=" not in line:
                current_description = ""
                continue

            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()

            # Remove surrounding quotes from default value
            if len(value) >= 2:
                if (value[0] == '"' and value[-1] == '"') or \
                   (value[0] == "'" and value[-1] == "'"):
                    value = value[1:-1]

            var = EnvVariable(
                name=key,
                default=value,
                description=current_description,
                category=self._detect_category(key),
                required=not bool(value),
                secret=self._is_secret(key),
            )

            variables.append(var)
            current_description = ""

        return variables

    def _detect_category(self, name: str) -> str:
        """
        Detect the category of a variable by its name prefix.

        Args:
            name: Variable name.

        Returns:
            Category string.
        """
        upper_name = name.upper()
        for prefix, category in self.CATEGORY_PREFIXES.items():
            if upper_name.startswith(prefix):
                return category
        return "General"

    def _is_secret(self, name: str) -> bool:
        """
        Determine if a variable is a secret based on its name.

        Args:
            name: Variable name.

        Returns:
            True if the variable appears to be a secret.
        """
        upper_name = name.upper()
        return any(pattern in upper_name for pattern in self.SECRET_PATTERNS)

    @staticmethod
    def generate_secret(length: int = 32) -> str:
        """
        Generate a cryptographically secure random secret.

        Args:
            length: Length of the secret in bytes before encoding.

        Returns:
            URL-safe random string.
        """
        return secrets.token_urlsafe(length)

    def prompt_variables(
        self,
        variables: List[EnvVariable],
        existing_values: Optional[Dict[str, str]] = None,
    ) -> Dict[str, str]:
        """
        Interactively prompt for variable values grouped by category.

        Uses inquirer for interactive input. Falls back to input()
        if inquirer is not available.

        Args:
            variables: List of variables to prompt for.
            existing_values: Existing values to use as defaults.

        Returns:
            Dictionary of variable name -> value.
        """
        existing = existing_values or {}
        result = {}

        # Group by category
        categories: Dict[str, List[EnvVariable]] = {}
        for var in variables:
            cat = var.category
            if cat not in categories:
                categories[cat] = []
            categories[cat].append(var)

        try:
            import inquirer
            has_inquirer = True
        except ImportError:
            has_inquirer = False

        for category, cat_vars in sorted(categories.items()):
            self.logger.info(f"\n  [{category}]")

            for var in cat_vars:
                current = existing.get(var.name, var.default)

                # Auto-generate secrets if no existing value
                if var.secret and not current:
                    generated = self.generate_secret()
                    result[var.name] = generated
                    self.logger.substep(f"{var.name} = [auto-generated]")
                    continue

                desc = f" ({var.description})" if var.description else ""
                prompt_msg = f"  {var.name}{desc}"

                if current:
                    prompt_msg += f" [{current}]"

                if has_inquirer and var.secret:
                    questions = [
                        inquirer.Password(
                            "value",
                            message=f"{var.name}",
                            default=current or "",
                        )
                    ]
                    answers = inquirer.prompt(questions)
                    value = answers["value"] if answers else current
                elif has_inquirer:
                    questions = [
                        inquirer.Text(
                            "value",
                            message=f"{var.name}",
                            default=current or "",
                        )
                    ]
                    answers = inquirer.prompt(questions)
                    value = answers["value"] if answers else current
                else:
                    value = input(f"{prompt_msg}: ").strip()

                result[var.name] = value or current or ""

        return result

    def prompt_non_interactive(
        self,
        variables: List[EnvVariable],
    ) -> Dict[str, str]:
        """
        Fill variable values non-interactively.

        Uses defaults for regular variables and auto-generates secrets.

        Args:
            variables: List of variables.

        Returns:
            Dictionary of variable name -> value.
        """
        result = {}
        for var in variables:
            if var.secret and not var.default:
                result[var.name] = self.generate_secret()
            else:
                result[var.name] = var.default
        return result

    def write_env_files(
        self,
        app_path: Path,
        values: Dict[str, str],
        file_mapping: Optional[Dict[str, List[str]]] = None,
    ) -> List[Path]:
        """
        Write environment variables to .env files.

        Args:
            app_path: Application root path.
            values: Variable name -> value mapping.
            file_mapping: Optional mapping of filename -> variable names.
                If None, writes all variables to a single .env file.

        Returns:
            List of written file paths.
        """
        written = []

        if file_mapping:
            for filename, var_names in file_mapping.items():
                file_path = app_path / filename
                file_values = {k: values[k] for k in var_names if k in values}
                self._write_single_env_file(file_path, file_values)
                written.append(file_path)
        else:
            env_path = app_path / ".env"
            self._write_single_env_file(env_path, values)
            written.append(env_path)

        return written

    def _write_single_env_file(self, path: Path, values: Dict[str, str]) -> None:
        """
        Write a single .env file.

        Values are not quoted for systemd compatibility.

        Args:
            path: Path to write the .env file.
            values: Variable name -> value mapping.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = []
        for key, value in sorted(values.items()):
            # Don't quote values for systemd compatibility
            lines.append(f"{key}={value}")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.logger.debug(f"Wrote env file: {path}")

    def save_config(self, app_path: Path, config: EnvConfig) -> None:
        """
        Persist environment configuration to .wasm/env-config.json.

        Args:
            app_path: Application root path.
            config: Environment configuration to save.
        """
        wasm_dir = app_path / ".wasm"
        wasm_dir.mkdir(parents=True, exist_ok=True)
        config_file = wasm_dir / "env-config.json"
        config_file.write_text(
            json.dumps(config.to_dict(), indent=2),
            encoding="utf-8",
        )

    def load_config(self, app_path: Path) -> Optional[EnvConfig]:
        """
        Load persisted environment configuration.

        Args:
            app_path: Application root path.

        Returns:
            EnvConfig or None if not found.
        """
        config_file = app_path / ".wasm" / "env-config.json"
        if not config_file.exists():
            return None
        try:
            data = json.loads(config_file.read_text(encoding="utf-8"))
            return EnvConfig.from_dict(data)
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            self.logger.warning(f"Failed to load env config: {e}")
            return None

    def mask_value(self, name: str, value: str) -> str:
        """
        Mask a value if it's a secret.

        Shows only the first 4 characters followed by asterisks.

        Args:
            name: Variable name.
            value: Variable value.

        Returns:
            Masked or original value.
        """
        if self._is_secret(name) and len(value) > 4:
            return value[:4] + "****"
        return value

    def get_current_values(self, app_path: Path) -> Dict[str, str]:
        """
        Read current .env file values.

        Strips quotes from values for consistency.

        Args:
            app_path: Application root path.

        Returns:
            Dictionary of current environment variable values.
        """
        values = {}
        env_file = app_path / ".env"
        if not env_file.exists():
            return values

        try:
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip()
                if len(val) >= 2:
                    if (val[0] == '"' and val[-1] == '"') or \
                       (val[0] == "'" and val[-1] == "'"):
                        val = val[1:-1]
                values[key] = val
        except OSError:
            pass

        return values

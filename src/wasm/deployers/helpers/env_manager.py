# Copyright (c) 2024-2025 Yago López Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Environment variable manager for WASM.

Handles discovering, prompting, and writing environment variables
for deployed applications, with support for .env.example parsing,
secret auto-generation, and interactive configuration.

Everything this module writes holds credentials: a deployed ``.env`` carries
``DATABASE_URL``, API keys and the secrets generated here, and
``.wasm/env-config.json`` records the same inventory. Both go out through
:mod:`wasm.core.fs` with :data:`~wasm.core.fs.SECRET_MODE`, so the mode is
applied by the ``os.open`` that creates the file rather than by a ``chmod``
afterwards, and the write lands on a temporary file that is renamed into place,
so a half-written ``.env`` never exists and a symlink planted at the destination
is replaced instead of followed. The application directory itself is left alone:
it is served by the web server and read by the service account, so tightening it
would break the deployment while doing nothing for the secrets, which are
protected by the file mode.
"""

import json
import re
import secrets
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from wasm.core.config import REDACTED
from wasm.core.exceptions import SecurityError, WASMError
from wasm.core.fs import SECRET_DIR_MODE, SECRET_MODE, FileSystem, get_fs
from wasm.core.logger import Logger

#: A credential embedded in a connection string. ``DATABASE_URL`` is the
#: canonical example: nothing in the *name* marks it as a secret, yet the value
#: carries the database password in clear.
#:
#: The user name is optional on purpose. ``redis://:password@host:6379`` is the
#: form redis-py, Heroku Redis and docker-compose all produce, and a pattern
#: that demands a user before the colon lets exactly that one through in clear.
#: The password may not contain ``/``, ``?`` or ``#``: those end the authority
#: in RFC 3986, so refusing them keeps ``http://host:8080/a@b`` from being read
#: as a credential and redacted.
URL_CREDENTIALS = re.compile(r"(?P<prefix>[A-Za-z][A-Za-z0-9+.\-]*://[^:/?#@\s]*:)[^@\s/?#]*@")


def redact_url_credentials(value: str, placeholder: str = REDACTED) -> str:
    """
    Replace the password inside every connection string in a value.

    Args:
        value: Text that may contain one or more URLs.
        placeholder: What to put where the password was.

    Returns:
        The value with every embedded password replaced. Text carrying no
        credential is returned unchanged.
    """
    return URL_CREDENTIALS.sub(lambda match: f"{match.group('prefix')}{placeholder}@", value)


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
    value: str | None = None


@dataclass
class EnvConfig:
    """Environment configuration for an application."""

    variables: list[EnvVariable] = field(default_factory=list)
    files: dict[str, list[str]] = field(default_factory=dict)  # filename -> variable names

    def to_dict(self) -> dict[str, Any]:
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
    def from_dict(cls, data: dict[str, Any]) -> "EnvConfig":
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
    CATEGORY_PREFIXES: ClassVar[dict[str, str]] = {
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
    SECRET_PATTERNS: ClassVar[list[str]] = [
        "PASSWORD",
        "_PASS",
        "SECRET",
        "TOKEN",
        "API_KEY",
        "PRIVATE_KEY",
        "ENCRYPTION_KEY",
        "SIGNING_KEY",
        "ACCESS_KEY",
        "SECRET_KEY",
        "CLIENT_SECRET",
        "WEBHOOK_SECRET",
    ]

    #: Substrings that mark a default as a template placeholder rather than a
    #: real value, matched case-insensitively against the .env.example default.
    #: A secret whose default is a placeholder is regenerated: baking
    #: "your-secret-key-here" into a systemd unit as Environment= overrides the
    #: real .env the application reads at runtime, and the failure surfaces
    #: much later as an authentication error nobody connects to a deploy.
    PLACEHOLDER_PATTERNS = [
        "your-",
        "your_",
        "yourapp",
        "youremail",
        "your.email",
        "user:password",
        "user:pass@",
        "username:password",
        "change-me",
        "changeme",
        "change_me",
        "replace-me",
        "replaceme",
        "replace_me",
        "<",
        "secret-key-here",
        "secret_key_here",
        "todo",
        "fixme",
        "placeholder",
    ]

    def __init__(self, verbose: bool = False, fs: FileSystem | None = None):
        """
        Args:
            verbose: Enable verbose logging.
            fs: Filesystem every write goes through. Defaults to the
                process-wide one, which is what makes ``--dry-run`` and the test
                doubles work without every call site knowing about them.
        """
        self.verbose = verbose
        self.logger = Logger(verbose=verbose)
        self._fs = fs

    @property
    def fs(self) -> FileSystem:
        """
        The filesystem every file this manager writes goes through.

        Returns:
            The injected filesystem, or the process-wide one.
        """
        return self._fs or get_fs()

    def discover(self, app_path: Path) -> list[EnvVariable]:
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

    def _parse_env_example(self, path: Path) -> list[EnvVariable]:
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
        variables: list[EnvVariable] = []
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
                if (value[0] == '"' and value[-1] == '"') or (value[0] == "'" and value[-1] == "'"):
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

    def _is_placeholder(self, value: str) -> bool:
        """
        Determine if a default value looks like a template placeholder.

        Used to decide whether a secret default copied from .env.example is
        safe to keep or should be regenerated. Matching is case-insensitive
        against PLACEHOLDER_PATTERNS.

        Args:
            value: Default value to inspect.

        Returns:
            True if the value matches a known placeholder pattern.
        """
        if not value:
            return False
        lower = value.lower()
        return any(pattern in lower for pattern in self.PLACEHOLDER_PATTERNS)

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
        variables: list[EnvVariable],
        existing_values: dict[str, str] | None = None,
    ) -> dict[str, str]:
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
        categories: dict[str, list[EnvVariable]] = {}
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
        variables: list[EnvVariable],
    ) -> dict[str, str]:
        """
        Fill variable values non-interactively.

        For secrets, regenerates whenever the default is empty or matches a
        known placeholder pattern (e.g. "your-secret-key-here"); otherwise the
        existing default is kept. For non-secret variables with a placeholder
        default a warning is logged so users notice that the unit will be
        deployed with template values, but the default is preserved to keep
        backward compatibility with existing .env.example layouts.

        Args:
            variables: List of variables.

        Returns:
            Dictionary of variable name -> value.
        """
        result = {}
        for var in variables:
            default_is_placeholder = self._is_placeholder(var.default)

            if var.secret and (not var.default or default_is_placeholder):
                result[var.name] = self.generate_secret()
                if default_is_placeholder:
                    self.logger.debug(
                        f"Regenerated secret for {var.name} (placeholder default detected)"
                    )
                continue

            if default_is_placeholder:
                self.logger.warning(
                    f"{var.name} has a placeholder default "
                    f"({var.default!r}); pass --env-file or run "
                    f"'wasm env set' to provide a real value before the "
                    f"application starts."
                )

            result[var.name] = var.default
        return result

    def write_env_files(
        self,
        app_path: Path,
        values: dict[str, str],
        file_mapping: dict[str, list[str]] | None = None,
    ) -> list[Path]:
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

    def _write_single_env_file(self, path: Path, values: dict[str, str]) -> None:
        """
        Write a single .env file, readable by its owner only.

        Values are not quoted for systemd compatibility.

        A symlink at the destination is refused rather than written through.
        The seam would not follow it anyway, because it renames a temporary file
        into place, but a link that appears where an application's ``.env``
        belongs is someone trying to harvest credentials, and continuing past it
        as if it were an ordinary file hides that.

        Args:
            path: Path to write the .env file.
            values: Variable name -> value mapping.

        Raises:
            SecurityError: If the destination is a symlink.
            OSError: If the file cannot be created or written.
        """
        if path.is_symlink():
            raise SecurityError(
                f"Refusing to write secrets through the symlink {path}",
                details=(
                    "Something replaced the file with a symbolic link, which would "
                    "redirect the write. Inspect the directory, remove the link and "
                    "retry."
                ),
            )

        lines = []
        for key, value in sorted(values.items()):
            # Don't quote values for systemd compatibility
            lines.append(f"{key}={value}")
        self.fs.write_text(path, "\n".join(lines) + "\n", mode=SECRET_MODE)
        self.logger.debug(f"Wrote env file: {path}")

    def save_config(self, app_path: Path, config: EnvConfig) -> None:
        """
        Persist environment configuration to .wasm/env-config.json.

        The file records the variable inventory, defaults included, so it is
        written 0600 inside a 0700 directory. Nothing but WASM reads ``.wasm``,
        so tightening that directory costs the deployment nothing.

        Args:
            app_path: Application root path.
            config: Environment configuration to save.

        Raises:
            SecurityError: If the destination is a symlink.
            OSError: If the file cannot be created or written.
        """
        directory = app_path / ".wasm"
        self.fs.make_dir(directory, mode=SECRET_DIR_MODE, parents=True)
        # A .wasm left world readable by an older version is tightened; the
        # directory is ours alone, so there is nothing else to break.
        if directory.is_dir() and directory.stat().st_mode & 0o077:
            self.fs.chmod(directory, SECRET_DIR_MODE)

        target = directory / "env-config.json"
        if target.is_symlink():
            raise SecurityError(
                f"Refusing to write secrets through the symlink {target}",
                details=(
                    "Something replaced the file with a symbolic link, which would "
                    "redirect the write. Inspect the directory, remove the link and "
                    "retry."
                ),
            )

        self.fs.write_text(target, json.dumps(config.to_dict(), indent=2), mode=SECRET_MODE)

    def load_config(self, app_path: Path) -> EnvConfig | None:
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

    def get_current_values(self, app_path: Path) -> dict[str, str]:
        """
        Read current .env file values.

        Strips quotes from values for consistency.

        Args:
            app_path: Application root path.

        Returns:
            Dictionary of current environment variable values.
        """
        values: dict[str, str] = {}
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
                    if (val[0] == '"' and val[-1] == '"') or (val[0] == "'" and val[-1] == "'"):
                        val = val[1:-1]
                values[key] = val
        except OSError:
            pass

        return values

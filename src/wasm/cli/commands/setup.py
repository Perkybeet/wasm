# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Setup commands: prepare the machine, then prove it is ready.

The configuration directory is deliberately owner-only. ``config.yaml`` holds the
MySQL root password, the SMTP account and the OpenAI API key, and WASM is a
root-only tool (it drives systemd, nginx and certbot), so nothing legitimate
reads that directory as another account. The consequence, and it is intentional:
running the web panel or any WASM command as a non-root user will not be able to
read ``/etc/wasm/config.yaml``. That is the privilege model, not a bug to be
fixed by widening the directory.

Two things this module refuses to do, because both were lies the previous
version told:

- **Install by guessing.** Every install used to be ``apt-get``, so on Fedora,
  openSUSE or Arch the wizard reported progress while installing nothing. The
  package manager is detected, and when there is none WASM says so instead of
  failing one opaque command at a time.
- **Report success it did not achieve.** ``setup init`` printed "Setup
  Complete!" and exited 0 even when every single install had failed. The exit
  status and the final message now describe what actually happened.
"""

from __future__ import annotations

import os
import shutil
import sys
from argparse import Namespace
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

import click

from wasm.cli.app import Context, pass_context
from wasm.core.config import (
    DEFAULT_APPS_DIR,
    DEFAULT_CONFIG_PATH,
    DEFAULT_LOG_DIR,
    SECRET_DIR_MODE,
    secure_directory,
)
from wasm.core.exceptions import WASMError
from wasm.core.logger import Logger, set_colors_disabled
from wasm.core.runner import DryRunRunner, SubprocessRunner, set_runner
from wasm.core.utils import command_exists, run_command, run_trusted_installer

if TYPE_CHECKING:
    from wasm.core.dependencies import DependencyChecker, SetupSummary

#: Where a system-wide man page belongs. A module constant so a test can point
#: it somewhere harmless.
MAN_PAGE_DIR = Path("/usr/share/man/man1")

#: Shells WASM can generate completions for, in the order they are offered.
COMPLETION_SHELLS = ("bash", "zsh", "fish")

#: Environment variable Click reads to produce completions for ``wasm``.
COMPLETE_VAR = "_WASM_COMPLETE"


class SetupError(WASMError):
    """The machine cannot be prepared as asked."""


# ---------------------------------------------------------------------------
# Global flags
# ---------------------------------------------------------------------------


def _enable_dry_run(state: Context) -> None:
    """
    Route every external command through the rehearsal runner.

    The root group does this when ``--dry-run`` comes before the subcommand.
    A subcommand has to do it itself when the flag comes after, because by then
    the root callback has already run.

    Args:
        state: The shared context to mark as rehearsing.
    """
    logger = state.logger
    logger.warning("Dry run: no changes will be made to this machine")
    set_runner(
        DryRunRunner(
            SubprocessRunner(),
            on_skip=lambda cmd: logger.info(f"would run: {' '.join(cmd)}"),
        )
    )


def _fold_into_context(attribute: str) -> Callable[[click.Context, click.Parameter, bool], bool]:
    """
    Build the callback that records a global flag on the shared context.

    Args:
        attribute: Name of the :class:`~wasm.cli.app.Context` attribute to set.

    Returns:
        A Click option callback.
    """

    def fold(ctx: click.Context, param: click.Parameter, value: bool) -> bool:
        if not value:
            return value
        state = ctx.ensure_object(Context)
        setattr(state, attribute, True)
        if attribute == "no_color":
            set_colors_disabled(True)
        elif attribute == "dry_run":
            _enable_dry_run(state)
        return value

    return fold


def global_flags(command: Callable[..., Any]) -> Callable[..., Any]:
    """
    Re-offer the root group's flags on a subcommand.

    ``wasm setup init --verbose`` is in scripts, in the published documentation
    and in muscle memory, so the flags have to keep parsing after the
    subcommand name. None of these options owns a value: they are eager, they
    do not reach the command function, and their callbacks only ever switch the
    shared context on. A subcommand therefore cannot undo a flag the user set
    before the subcommand name, which is exactly how ``wasm --dry-run monitor
    scan`` used to run for real.

    Args:
        command: The function being decorated into a Click command.

    Returns:
        The decorated function.
    """
    options = [
        click.option(
            "-v",
            "--verbose",
            is_flag=True,
            is_eager=True,
            expose_value=False,
            callback=_fold_into_context("verbose"),
            help="Show the detail of each step.",
        ),
        click.option(
            "--dry-run",
            is_flag=True,
            is_eager=True,
            expose_value=False,
            callback=_fold_into_context("dry_run"),
            help="Rehearse without changing anything.",
        ),
        click.option(
            "--no-color",
            is_flag=True,
            is_eager=True,
            expose_value=False,
            callback=_fold_into_context("no_color"),
            help="Never emit colour.",
        ),
    ]
    for option in reversed(options):
        command = option(command)
    return command


def _exit(code: int) -> NoReturn:
    """
    Leave the command with a status the calling shell can test.

    Args:
        code: Process exit status.

    Raises:
        click.exceptions.Exit: Always; this is how Click unwinds.
    """
    click.get_current_context().exit(code)


# ---------------------------------------------------------------------------
# Distribution packages
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PackageManager:
    """
    How to install software on one family of distributions.

    Attributes:
        program: Executable that drives the package database.
        refresh: Argument vector that refreshes the package lists.
        install: Argument vector prefix that installs without prompting.
        packages: WASM's name for a package to this family's name for it.
        services: WASM's name for a daemon to this family's unit name.
    """

    program: str
    refresh: tuple[str, ...]
    install: tuple[str, ...]
    packages: dict[str, str]
    services: dict[str, str]

    def package_for(self, name: str) -> str | None:
        """
        Translate a WASM package name into a distribution package name.

        Args:
            name: WASM's name for the package, such as "certbot-nginx".

        Returns:
            The distribution's package name, or None when this family does not
            ship it.
        """
        return self.packages.get(name)

    def service_for(self, name: str) -> str:
        """
        Translate a WASM daemon name into a systemd unit name.

        Args:
            name: WASM's name for the daemon, such as "apache".

        Returns:
            The unit name to hand to systemctl.
        """
        return self.services.get(name, name)


#: Supported package managers, most widely deployed first. Detection is by the
#: presence of the executable, which is also what tells apt-based and dnf-based
#: systems apart on machines that carry both.
PACKAGE_MANAGERS: tuple[PackageManager, ...] = (
    PackageManager(
        program="apt-get",
        refresh=("apt-get", "update"),
        install=("apt-get", "install", "-y"),
        packages={
            "git": "git",
            "curl": "curl",
            "nginx": "nginx",
            "apache": "apache2",
            "certbot": "certbot",
            "certbot-nginx": "python3-certbot-nginx",
            "certbot-apache": "python3-certbot-apache",
            "nodejs": "nodejs",
        },
        services={"nginx": "nginx", "apache": "apache2"},
    ),
    PackageManager(
        program="dnf",
        refresh=("dnf", "makecache"),
        install=("dnf", "install", "-y"),
        packages={
            "git": "git",
            "curl": "curl",
            "nginx": "nginx",
            "apache": "httpd",
            "certbot": "certbot",
            "certbot-nginx": "python3-certbot-nginx",
            "certbot-apache": "python3-certbot-apache",
            "nodejs": "nodejs",
        },
        services={"nginx": "nginx", "apache": "httpd"},
    ),
    PackageManager(
        program="zypper",
        refresh=("zypper", "--non-interactive", "refresh"),
        install=("zypper", "--non-interactive", "install"),
        packages={
            "git": "git",
            "curl": "curl",
            "nginx": "nginx",
            "apache": "apache2",
            "certbot": "certbot",
            "certbot-nginx": "python3-certbot-nginx",
            "certbot-apache": "python3-certbot-apache",
            "nodejs": "nodejs",
        },
        services={"nginx": "nginx", "apache": "apache2"},
    ),
    PackageManager(
        program="pacman",
        refresh=("pacman", "-Sy", "--noconfirm"),
        install=("pacman", "-S", "--noconfirm", "--needed"),
        packages={
            "git": "git",
            "curl": "curl",
            "nginx": "nginx",
            "apache": "apache",
            "certbot": "certbot",
            "certbot-nginx": "certbot-nginx",
            "certbot-apache": "certbot-apache",
            "nodejs": "nodejs",
        },
        services={"nginx": "nginx", "apache": "httpd"},
    ),
)


def detect_package_manager() -> PackageManager | None:
    """
    Find the package manager this machine actually uses.

    Returns:
        The first supported package manager present, or None when WASM cannot
        install software here.
    """
    for manager in PACKAGE_MANAGERS:
        if command_exists(manager.program):
            return manager
    return None


def _install_packages(
    logger: Logger,
    manager: PackageManager,
    label: str,
    names: list[str],
) -> str | None:
    """
    Install one or more packages, naming what went wrong if it fails.

    Args:
        logger: Logger used to report progress.
        manager: The detected package manager.
        label: Human name of what is being installed, for the messages.
        names: WASM package names to translate and install.

    Returns:
        None on success, or a sentence describing the failure.
    """
    resolved = [pkg for pkg in (manager.package_for(name) for name in names) if pkg]
    if not resolved:
        return f"{label} is not packaged for {manager.program}; install it by hand"

    logger.substep(f"Installing {label} ({' '.join(resolved)})...")
    result = run_command([*manager.install, *resolved])
    if result.success:
        logger.success(f"{label} installed")
        return None

    detail = (result.stderr or result.stdout).strip().splitlines()
    reason = detail[-1] if detail else f"exit code {result.exit_code}"
    logger.warning(f"Failed to install {label}: {reason}")
    return f"{label}: {reason}"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _create_config_directory(logger: Logger) -> bool:
    """
    Create the directory that holds config.yaml, owner-only.

    Args:
        logger: Logger used to report progress.

    Returns:
        True if the directory exists and is private afterwards.
    """
    config_dir = DEFAULT_CONFIG_PATH.parent
    logger.substep(f"Creating config directory: {config_dir}")
    try:
        secure_directory(config_dir)
    except OSError as e:
        logger.warning(f"Failed to create config directory: {e}")
        return False

    if config_dir.stat().st_mode & 0o077:
        logger.warning(
            f"{config_dir} is readable by other accounts; it holds credentials. "
            f"Fix it with: chmod {SECRET_DIR_MODE:o} {config_dir}"
        )
        return False

    logger.success(f"Created {config_dir}")
    return True


def _detect_shell() -> str | None:
    """
    Work out which shell invoked WASM.

    Returns:
        "bash", "zsh", "fish", or None when $SHELL says something else.
    """
    shell_path = os.environ.get("SHELL", "")
    for shell in COMPLETION_SHELLS:
        if shell in shell_path:
            return shell
    return None


def _prompts_possible() -> bool:
    """
    Decide whether the wizard may ask questions.

    Returns:
        True when both ends of the terminal are attached, so a prompt would be
        seen and could be answered.
    """
    return sys.stdin.isatty() and sys.stdout.isatty()


# ---------------------------------------------------------------------------
# setup completions
# ---------------------------------------------------------------------------


def completion_source(shell: str) -> str:
    """
    Generate the completion script for a shell from the command tree.

    Generated, never handwritten: the tree has 108 subcommands and a
    handwritten script goes stale the first time one is added.

    Args:
        shell: "bash", "zsh" or "fish".

    Returns:
        The script to install.

    Raises:
        SetupError: If Click cannot generate completions for that shell.
    """
    from click.shell_completion import get_completion_class

    completion_cls = get_completion_class(shell)
    if completion_cls is None:
        raise SetupError(
            f"Cannot generate completions for {shell}",
            details=f"Supported shells: {', '.join(COMPLETION_SHELLS)}",
        )

    from wasm.cli.app import cli as root_command

    return completion_cls(root_command, {}, "wasm", COMPLETE_VAR).source()


def _completion_target(shell: str, user_only: bool) -> Path:
    """
    Work out where a shell looks for the completion file.

    Args:
        shell: "bash", "zsh" or "fish".
        user_only: Install under the invoking user's home instead of system-wide.

    Returns:
        The file to write.
    """
    home = Path.home()
    if shell == "bash":
        if user_only:
            return home / ".local/share/bash-completion/completions/wasm"
        return Path("/etc/bash_completion.d/wasm")
    if shell == "zsh":
        if user_only:
            return home / ".zsh/completions/_wasm"
        system_dir = Path("/usr/share/zsh/site-functions")
        if not system_dir.exists():
            system_dir = Path("/usr/local/share/zsh/site-functions")
        return system_dir / "_wasm"
    if user_only:
        return home / ".config/fish/completions/wasm.fish"
    return Path("/usr/share/fish/vendor_completions.d/wasm.fish")


def _completion_instructions(shell: str, target: Path, user_only: bool) -> list[str]:
    """
    Say what the user has to do for the new file to take effect.

    Args:
        shell: "bash", "zsh" or "fish".
        target: Where the script was written.
        user_only: Whether it went into the user's home.

    Returns:
        Lines to print, in order.
    """
    if shell == "bash":
        lines = ["Start a new shell, or run: source " + str(target)]
        if user_only:
            lines.append("Requires bash-completion; on most systems it is already installed.")
        return lines
    if shell == "zsh":
        lines = []
        if user_only:
            lines.append(f"Add to ~/.zshrc: fpath=({target.parent} $fpath)")
        lines.append("Then run: autoload -Uz compinit && compinit")
        return lines
    return ["Completions are live in new shells, or run: exec fish"]


def _run_completions(logger: Logger, shell: str | None, user_only: bool, to_stdout: bool) -> int:
    """
    Generate shell completions and install or print them.

    Args:
        logger: Logger used to report progress.
        shell: Target shell, or None to detect it from $SHELL.
        user_only: Install under the invoking user's home instead of system-wide.
        to_stdout: Print the script instead of writing it anywhere.

    Returns:
        Exit code.
    """
    if not shell:
        shell = _detect_shell()
        if not shell:
            logger.error(
                "Could not tell which shell you use",
                details=f"Name it: wasm setup completions --shell {'|'.join(COMPLETION_SHELLS)}",
            )
            return 1

    script = completion_source(shell)

    if to_stdout:
        click.echo(script)
        return 0

    logger.header("WASM Shell Completions")
    logger.key_value("Shell", shell)

    target = _completion_target(shell, user_only)

    if not user_only and os.geteuid() != 0:
        logger.error(
            f"Writing {target} needs root",
            details="Run this with sudo, or use --user-only to install it for yourself.",
        )
        return 1

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(script, encoding="utf-8")
    except OSError as e:
        logger.error(
            f"Could not write {target}: {e}",
            details="Run this with sudo, or use --user-only to install it for yourself.",
        )
        return 1

    logger.success(f"Installed {shell} completions to {target}")
    for line in _completion_instructions(shell, target, user_only):
        logger.info(line)
    return 0


# ---------------------------------------------------------------------------
# setup init
# ---------------------------------------------------------------------------


def _report_current_state(logger: Logger, summary: SetupSummary) -> None:
    """
    Print what is already installed, before anything is changed.

    Args:
        logger: Logger used to report progress.
        summary: System summary from the dependency checker.
    """
    from wasm.core.utils import get_system_info

    logger.blank()
    logger.info("Current System Status:")

    sys_info = get_system_info()
    logger.key_value("  OS", sys_info.get("os", "Unknown"))
    logger.key_value("  Kernel", sys_info.get("kernel", "Unknown"))

    webserver = summary["webserver"]
    logger.key_value("  Web Server", webserver if webserver else "not installed")

    if summary["nodejs"]["installed"]:
        logger.key_value("  Node.js", str(summary["nodejs"]["version"]))
        installed_pms = [
            pm for pm, info in summary["nodejs"]["package_managers"].items() if info["installed"]
        ]
        logger.key_value("  Package Managers", ", ".join(installed_pms) if installed_pms else "npm")
    else:
        logger.key_value("  Node.js", "not installed")

    if summary["python"]["installed"]:
        logger.key_value("  Python", str(summary["python"]["version"]))
    else:
        logger.key_value("  Python", "not installed")

    logger.key_value("  Git", "installed" if command_exists("git") else "not installed")
    logger.key_value("  Certbot", "installed" if command_exists("certbot") else "not installed")
    logger.blank()


def normalise_webserver(name: str | None) -> str:
    """
    Translate a detected web server into the name the rest of WASM uses.

    The dependency checker reports the package name it found, "apache2" on
    Debian and "httpd" on Fedora, while the deployers compare against "apache".
    Writing the package name into config.yaml is how a machine ends up
    configured for a web server nothing recognises.

    Args:
        name: What was detected, or None when nothing was.

    Returns:
        "nginx" or "apache".
    """
    if name in ("apache2", "httpd", "apache"):
        return "apache"
    return "nginx"


def _default_choices(summary: SetupSummary) -> dict[str, Any]:
    """
    Build the choices the wizard makes when nobody is there to answer.

    Args:
        summary: System summary from the dependency checker.

    Returns:
        The same shape the prompts return.
    """
    return {
        "install_git": not command_exists("git"),
        "install_webserver": summary["webserver"] is None,
        "webserver_choice": normalise_webserver(summary["webserver"]),
        "install_nodejs": not summary["nodejs"]["installed"],
        "package_managers": ["npm"],
        "install_certbot": not command_exists("certbot"),
        "ssl_email": "",
    }


def _interactive_setup_prompts(summary: SetupSummary) -> dict[str, Any] | None:
    """
    Ask the operator what to install.

    Uses questionary rather than inquirer: inquirer has no package in Debian or
    Ubuntu, so on the distributions WASM targets most this wizard was never
    interactive at all.

    Args:
        summary: System summary from the dependency checker.

    Returns:
        Configuration choices, or None if the operator cancelled.
    """
    import questionary

    answers = _default_choices(summary)

    if not command_exists("git"):
        answers["install_git"] = questionary.confirm(
            "Git is not installed. Install it now?", default=True
        ).ask()
        if answers["install_git"] is None:
            return None

    if summary["webserver"] is None:
        install_webserver = questionary.confirm(
            "No web server found. Install one?", default=True
        ).ask()
        if install_webserver is None:
            return None
        answers["install_webserver"] = install_webserver

        if install_webserver:
            choice = questionary.select(
                "Which web server?",
                choices=[
                    questionary.Choice("Nginx (recommended)", value="nginx"),
                    questionary.Choice("Apache", value="apache"),
                ],
                default="nginx",
            ).ask()
            if choice is None:
                return None
            answers["webserver_choice"] = choice

    if not summary["nodejs"]["installed"]:
        install_nodejs = questionary.confirm(
            "Node.js is not installed. Install it? Required for JavaScript apps.", default=True
        ).ask()
        if install_nodejs is None:
            return None
        answers["install_nodejs"] = install_nodejs

    selected = [
        pm for pm, info in summary["nodejs"]["package_managers"].items() if info["installed"]
    ]
    package_managers = questionary.checkbox(
        "Which package managers should be available? Space to toggle, Enter to confirm.",
        choices=[
            questionary.Choice("npm (ships with Node.js)", value="npm", checked=True),
            questionary.Choice(
                "pnpm (fast, disk efficient)", value="pnpm", checked="pnpm" in selected
            ),
            questionary.Choice("yarn", value="yarn", checked="yarn" in selected),
            questionary.Choice("bun", value="bun", checked="bun" in selected),
        ],
    ).ask()
    if package_managers is None:
        return None
    answers["package_managers"] = package_managers or ["npm"]

    if not command_exists("certbot"):
        install_certbot = questionary.confirm(
            "Certbot is not installed. Install it? Required for HTTPS certificates.", default=True
        ).ask()
        if install_certbot is None:
            return None
        answers["install_certbot"] = install_certbot

    ssl_email = questionary.text(
        "Email for expiry notices from Let's Encrypt (Enter to skip)", default=""
    ).ask()
    if ssl_email is None:
        return None
    answers["ssl_email"] = ssl_email.strip()

    return answers


def _install_node_package_manager(logger: Logger, pm: str) -> str | None:
    """
    Install one Node.js package manager.

    Args:
        logger: Logger used to report progress.
        pm: Package manager name.

    Returns:
        None on success, or a sentence describing the failure.
    """
    if pm == "bun":
        # Bun is not packaged by any distribution WASM targets; its own
        # installer is on the trusted whitelist.
        result = run_trusted_installer("https://bun.sh/install")
    else:
        result = run_command(["npm", "install", "-g", pm])

    if result.success:
        logger.success(f"{pm} installed")
        return None

    reason = (result.stderr or result.stdout).strip().splitlines()
    logger.warning(f"Failed to install {pm}: {reason[-1] if reason else result.exit_code}")
    return f"{pm}: {reason[-1] if reason else f'exit code {result.exit_code}'}"


def _install_dependencies(
    logger: Logger, manager: PackageManager, choices: dict[str, Any]
) -> list[str]:
    """
    Install everything the operator asked for.

    Args:
        logger: Logger used to report progress.
        manager: The detected package manager.
        choices: Answers from the prompts, or the non-interactive defaults.

    Returns:
        One sentence per failure. Empty when everything was installed.
    """
    failures: list[str] = []

    logger.substep(f"Refreshing package lists with {manager.program}...")
    refresh = run_command(list(manager.refresh))
    if not refresh.success:
        # Not fatal: the installs below may still succeed from a stale index,
        # and saying so is more useful than stopping.
        logger.warning(f"Could not refresh package lists ({manager.program})")

    if choices.get("install_git") and not command_exists("git"):
        failure = _install_packages(logger, manager, "Git", ["git"])
        if failure:
            failures.append(failure)

    webserver = choices.get("webserver_choice", "nginx")
    if choices.get("install_webserver"):
        failure = _install_packages(logger, manager, webserver, [webserver])
        if failure:
            failures.append(failure)
        else:
            unit = manager.service_for(webserver)
            run_command(["systemctl", "enable", unit])
            run_command(["systemctl", "start", unit])

    if choices.get("install_certbot") and not command_exists("certbot"):
        plugin = f"certbot-{webserver}"
        packages = ["certbot"]
        if manager.package_for(plugin):
            packages.append(plugin)
        failure = _install_packages(logger, manager, "Certbot", packages)
        if failure:
            failures.append(failure)

    return failures


def _install_node_environment(
    logger: Logger, manager: PackageManager, choices: dict[str, Any]
) -> list[str]:
    """
    Install Node.js and the requested package managers.

    Args:
        logger: Logger used to report progress.
        manager: The detected package manager.
        choices: Answers from the prompts, or the non-interactive defaults.

    Returns:
        One sentence per failure. Empty when everything was installed.
    """
    failures: list[str] = []

    if choices.get("install_nodejs") and not command_exists("node"):
        logger.substep("Installing Node.js 20.x LTS...")
        setup_result = run_trusted_installer("https://deb.nodesource.com/setup_20.x")
        if setup_result.success:
            failure = _install_packages(logger, manager, "Node.js", ["nodejs"])
            if failure:
                failures.append(failure)
        else:
            logger.warning("Could not add the NodeSource repository")
            failures.append("Node.js: the NodeSource repository could not be added")

    for pm in choices.get("package_managers", ["npm"]):
        if pm == "npm":
            continue  # npm ships with Node.js
        if command_exists(pm):
            logger.substep(f"{pm} already installed")
            continue
        if not command_exists("npm") and pm != "bun":
            failures.append(f"{pm}: npm is not available to install it")
            logger.warning(f"Cannot install {pm}: npm is not available")
            continue
        logger.substep(f"Installing {pm}...")
        failure = _install_node_package_manager(logger, pm)
        if failure:
            failures.append(failure)

    return failures


def _create_directories(logger: Logger) -> list[str]:
    """
    Create the directories a deployment writes into.

    Args:
        logger: Logger used to report progress.

    Returns:
        One sentence per failure. Empty when all directories exist.
    """
    failures: list[str] = []

    # Served content and logs are deliberately world-readable: the web server's
    # account has to traverse the first, and an operator should be able to tail
    # the second without sudo. The config directory is not, and secure_directory
    # is what enforces that.
    for label, path in (("apps", DEFAULT_APPS_DIR), ("log", DEFAULT_LOG_DIR)):
        logger.substep(f"Creating {label} directory: {path}")
        try:
            path.mkdir(parents=True, exist_ok=True)
            os.chmod(path, 0o755)  # noqa: S103
        except OSError as e:
            logger.warning(f"Failed to create {label} directory: {e}")
            failures.append(f"{path}: {e}")
        else:
            logger.success(f"Created {path}")

    if not _create_config_directory(logger):
        failures.append(f"{DEFAULT_CONFIG_PATH.parent}: could not be created privately")

    return failures


def _write_config(logger: Logger, choices: dict[str, Any]) -> list[str]:
    """
    Record the operator's choices in config.yaml.

    Args:
        logger: Logger used to report progress.
        choices: Answers from the prompts, or the non-interactive defaults.

    Returns:
        One sentence per failure. Empty when the file was written.
    """
    from wasm.core.config import Config

    existed = DEFAULT_CONFIG_PATH.exists()
    config = Config()

    ssl_email = choices.get("ssl_email", "")
    if ssl_email:
        config.set("ssl.email", ssl_email)
    config.set("webserver", choices.get("webserver_choice", "nginx"))
    config.set("nodejs.package_managers", choices.get("package_managers", ["npm"]))

    # save() reports failure by returning False, having logged the reason. An
    # unchecked call is how a wizard ends up announcing a configuration file it
    # never managed to write.
    if not config.save():
        logger.warning(f"Could not save {DEFAULT_CONFIG_PATH}")
        return [f"{DEFAULT_CONFIG_PATH}: could not be written, see the error above"]

    logger.success(f"{'Updated' if existed else 'Created'} {DEFAULT_CONFIG_PATH}")
    return []


def _install_man_page(logger: Logger) -> None:
    """
    Copy the man page into place, if one shipped with this install.

    A missing man page is not a setup failure, so nothing here is reported as
    one.

    Args:
        logger: Logger used to report progress.
    """
    destination = MAN_PAGE_DIR / "wasm.1"
    sources = [
        Path(__file__).resolve().parents[4] / "man" / "wasm.1",
        Path("/usr/local/share/man/man1/wasm.1"),
        Path("/usr/share/man/man1/wasm.1"),
    ]
    source = next((path for path in sources if path.exists()), None)

    if source is None:
        logger.debug("No man page shipped with this install, skipping")
        return
    if source == destination:
        logger.debug("Man page already installed")
        return

    logger.substep("Installing man page...")
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(source, destination)
        os.chmod(destination, 0o644)
    except OSError as e:
        logger.debug(f"Could not install the man page: {e}")
        return

    run_command(["mandb", "-q"])
    logger.success("Man page installed (man wasm)")


def _report_final_state(logger: Logger, checker: DependencyChecker, failures: list[str]) -> None:
    """
    Say what the machine looks like now, and what did not work.

    Args:
        logger: Logger used to report progress.
        checker: Dependency checker, re-queried after the installs.
        failures: One sentence per thing that failed.
    """
    logger.blank()
    if failures:
        logger.header("Setup finished with problems")
    else:
        logger.header("Setup complete")
    logger.blank()

    final_summary = checker.get_setup_summary()
    logger.info("Final System Status:")
    if final_summary["webserver"]:
        logger.key_value("  Web Server", str(final_summary["webserver"]))
    if command_exists("node"):
        logger.key_value("  Node.js", str(checker.get_version("node")))
        installed = [pm for pm in ("npm", "pnpm", "yarn", "bun") if command_exists(pm)]
        logger.key_value("  Package Managers", ", ".join(installed))
    if command_exists("certbot"):
        logger.key_value("  SSL (Certbot)", "ready")
    if command_exists("git"):
        logger.key_value("  Git", "ready")

    logger.blank()
    if failures:
        logger.error(f"{len(failures)} step(s) did not complete:")
        for failure in failures:
            logger.list_item(failure)
        logger.blank()
        logger.info("Fix the causes above and run 'sudo wasm setup init' again.")
        logger.blank()
        return

    logger.info("Next steps:")
    logger.info("  1. Deploy your first app: wasm create -d example.com -s <git-url> -t nextjs")
    logger.info("  2. Set up SSH for Git: wasm setup ssh --generate")
    logger.info("  3. Install shell completions: wasm setup completions")
    logger.info("  4. Run diagnostics: wasm setup doctor")
    logger.blank()


def _run_init(logger: Logger, assume_defaults: bool) -> int:
    """
    Prepare this machine for deployments.

    Args:
        logger: Logger used to report progress.
        assume_defaults: Install the defaults without asking anything.

    Returns:
        Exit code. Non-zero when any step failed, so a provisioning script that
        checks the status sees the truth.
    """
    if os.geteuid() != 0:
        logger.error(
            "Initial setup needs root",
            details="Run: sudo wasm setup init",
        )
        return 1

    manager = detect_package_manager()
    if manager is None:
        supported = ", ".join(pm.program for pm in PACKAGE_MANAGERS)
        logger.error(
            "No supported package manager on this system",
            details=(
                f"WASM installs software with one of: {supported}. "
                "Install nginx, git, certbot and Node.js by hand, then run "
                "'wasm setup doctor' to confirm."
            ),
        )
        return 1

    logger.header("WASM Initial Setup")
    logger.info("This prepares the machine for deploying web applications.")
    logger.key_value("Package manager", manager.program)
    logger.blank()

    from wasm.core.dependencies import DependencyChecker

    checker = DependencyChecker(verbose=logger.verbose)

    logger.step(1, 6, "Analyzing system requirements")
    summary = checker.get_setup_summary()
    _report_current_state(logger, summary)

    if assume_defaults:
        logger.step(2, 6, "Using default configuration")
        choices = _default_choices(summary)
    else:
        logger.step(2, 6, "Configuration options")
        answers = _interactive_setup_prompts(summary)
        if answers is None:
            logger.info("Setup cancelled")
            return 130
        choices = answers

    failures: list[str] = []

    logger.step(3, 6, "Installing system dependencies")
    failures += _install_dependencies(logger, manager, choices)

    logger.step(4, 6, "Setting up the Node.js environment")
    failures += _install_node_environment(logger, manager, choices)

    logger.step(5, 6, "Creating WASM directories")
    failures += _create_directories(logger)

    logger.step(6, 6, "Writing the configuration file")
    failures += _write_config(logger, choices)
    _install_man_page(logger)

    _report_final_state(logger, checker, failures)
    return 1 if failures else 0


# ---------------------------------------------------------------------------
# setup permissions
# ---------------------------------------------------------------------------


def _run_permissions(logger: Logger) -> int:
    """
    Check that WASM can write where it needs to.

    Args:
        logger: Logger used to report progress.

    Returns:
        Exit code.
    """
    logger.header("WASM Permissions Check")
    logger.blank()

    issues: list[str] = []

    for label, path in (("Apps", DEFAULT_APPS_DIR), ("Log", DEFAULT_LOG_DIR)):
        if not path.exists():
            logger.warning(f"{label} directory does not exist: {path}")
            issues.append(str(path))
        elif os.access(path, os.W_OK):
            logger.success(f"{label} directory writable: {path}")
        else:
            logger.warning(f"{label} directory not writable: {path}")
            issues.append(str(path))

    # The config directory holds credentials, so "too open" is as much a problem
    # as "not readable": WASM runs as root and nothing else needs to read it.
    config_dir = DEFAULT_CONFIG_PATH.parent
    if config_dir.exists():
        if not os.access(config_dir, os.R_OK):
            logger.warning(f"Config directory not readable: {config_dir}")
            issues.append(str(config_dir))
        elif config_dir.stat().st_mode & 0o077:
            logger.warning(
                f"Config directory exposes credentials to other accounts: {config_dir} "
                f"(expected mode {SECRET_DIR_MODE:o})"
            )
            issues.append(str(config_dir))
        else:
            logger.success(f"Config directory readable: {config_dir}")

    nginx_available = Path("/etc/nginx/sites-available")
    if nginx_available.exists():
        if os.access(nginx_available, os.W_OK):
            logger.success("Nginx sites-available writable")
        else:
            logger.info("Nginx sites-available requires root")

    systemd_dir = Path("/etc/systemd/system")
    if systemd_dir.exists():
        if os.access(systemd_dir, os.W_OK):
            logger.success("Systemd directory writable")
        else:
            logger.info("Systemd directory requires root")

    logger.blank()

    if issues:
        logger.warning("Some directories need to be created or have their permissions fixed")
        logger.info("Run: sudo wasm setup init")
    else:
        logger.success("All permissions OK")
        logger.info("Note: changing nginx or systemd still requires root")

    return 0


# ---------------------------------------------------------------------------
# setup ssh
# ---------------------------------------------------------------------------


def _print_public_key(logger: Logger, public_key: str) -> None:
    """
    Show a public key with the lines that make it easy to copy.

    Args:
        logger: Logger used to report progress.
        public_key: The key material.
    """
    logger.blank()
    click.echo("-" * 70)
    click.echo(public_key)
    click.echo("-" * 70)
    logger.blank()


def _run_ssh(
    logger: Logger, generate: bool, key_type: str, show: bool, test_host: str | None
) -> int:
    """
    Set up or inspect the SSH key WASM clones private repositories with.

    Args:
        logger: Logger used to report progress.
        generate: Create a key when none exists.
        key_type: Key algorithm to generate.
        show: Print the public key.
        test_host: Host to open a test connection to, such as github.com.

    Returns:
        Exit code.
    """
    from wasm.validators.ssh import (
        generate_ssh_key,
        get_all_ssh_keys,
        get_public_key,
        get_ssh_directory,
        ssh_key_exists,
        test_ssh_connection,
    )

    logger.header("WASM SSH Setup")
    logger.blank()

    key_exists, key_path = ssh_key_exists()
    logger.key_value("SSH Directory", str(get_ssh_directory()))

    if key_exists:
        logger.key_value("SSH Key Found", str(key_path))
        all_keys = get_all_ssh_keys()
        if len(all_keys) > 1:
            logger.key_value("Additional Keys", ", ".join(str(k.name) for k in all_keys[1:]))
    else:
        logger.key_value("SSH Key Found", "None")

    logger.blank()

    if not key_exists:
        if not generate:
            logger.error(
                "No SSH key on this system",
                details=(
                    "Create one with: wasm setup ssh --generate\n"
                    f"Or by hand with: ssh-keygen -t {key_type}"
                ),
            )
            return 1

        logger.step(1, 3, "Generating SSH key")
        success, new_key_path, message = generate_ssh_key(
            key_type=key_type,
            comment=f"wasm@{os.uname().nodename}",
        )
        if not success or new_key_path is None:
            logger.error(message)
            return 1

        logger.success(f"SSH key generated: {new_key_path}")
        key_path = new_key_path
        key_exists = True

    public_key = get_public_key(key_path)

    if show or generate:
        if public_key:
            logger.info("Your public SSH key:")
            _print_public_key(logger, public_key)
            logger.info("Add this key to your Git provider:")
            logger.blank()
            logger.info("  GitHub:    https://github.com/settings/keys")
            logger.info("  GitLab:    https://gitlab.com/-/user_settings/ssh_keys")
            logger.info("  Bitbucket: https://bitbucket.org/account/settings/ssh-keys/")
            logger.blank()
        else:
            logger.warning("Could not read the public key")

    if test_host:
        logger.info(f"Testing SSH connection to {test_host}...")
        success, message = test_ssh_connection(test_host)
        if success:
            logger.success(f"SSH connection to {test_host} works")
            return 0

        logger.error(
            f"SSH connection to {test_host} failed: {message}",
            details=f"Add the public key below to the account you clone with on {test_host}.",
        )
        if public_key:
            _print_public_key(logger, public_key)
        return 1

    if not show:
        logger.success("SSH key is configured")
        logger.blank()
        logger.info("Useful commands:")
        logger.info("  wasm setup ssh --show              Show your public key")
        logger.info("  wasm setup ssh --test github.com   Test the connection")

    return 0


# ---------------------------------------------------------------------------
# setup doctor
# ---------------------------------------------------------------------------


def _run_doctor(logger: Logger) -> int:
    """
    Check everything a deployment depends on and say how to fix what is broken.

    Args:
        logger: Logger used to report progress.

    Returns:
        Exit code. Non-zero when something is missing that deployments need.
    """
    logger.header("WASM System Diagnostics")
    logger.blank()

    from wasm.core.dependencies import DependencyChecker

    checker = DependencyChecker(verbose=logger.verbose)
    manager = detect_package_manager()
    install_hint = " ".join(manager.install) if manager else "your package manager"

    issues = 0
    warnings = 0

    logger.section("Core Dependencies")
    for command in ("git", "curl"):
        if command_exists(command):
            logger.success(f"{command}: {checker.get_version(command) or 'OK'}")
        else:
            logger.error(f"{command}: not installed")
            logger.info(f"  Fix: sudo {install_hint} {command}")
            issues += 1
    logger.blank()

    logger.section("Web Server")
    if command_exists("nginx"):
        logger.success(f"nginx: {checker.get_version('nginx', '-v')}")
        warnings += _report_unit_state(logger, "nginx")
    elif command_exists("apache2") or command_exists("httpd"):
        unit = "apache2" if command_exists("apache2") else "httpd"
        logger.success(f"{unit}: installed")
        warnings += _report_unit_state(logger, unit)
    else:
        logger.error("Web server: not installed")
        logger.info(f"  Fix: sudo {install_hint} nginx")
        issues += 1
    logger.blank()

    logger.section("Node.js Environment")
    if command_exists("node"):
        logger.success(f"node: {checker.get_version('node')}")
        if command_exists("npm"):
            logger.success(f"npm: {checker.get_version('npm')}")
        else:
            logger.error("npm: not installed, although it ships with Node.js")
            issues += 1
        for pm in ("pnpm", "yarn", "bun"):
            if command_exists(pm):
                logger.success(f"{pm}: {checker.get_version(pm)}")
            else:
                logger.info(f"{pm}: not installed (optional)")
    else:
        logger.error("node: not installed")
        logger.info("  Fix: sudo wasm setup init")
        issues += 1
    logger.blank()

    logger.section("Python Environment")
    if command_exists("python3"):
        logger.success(f"python3: {checker.get_version('python3')}")
        if command_exists("pip3"):
            logger.success(f"pip3: {checker.get_version('pip3')}")
        else:
            logger.warning("pip3: not installed")
            logger.info(f"  Fix: sudo {install_hint} python3-pip")
            warnings += 1
    else:
        logger.warning("python3: not installed, needed for Python apps")
        warnings += 1
    logger.blank()

    logger.section("SSL/TLS (Certbot)")
    if command_exists("certbot"):
        logger.success(f"certbot: {checker.get_version('certbot')}")
    else:
        logger.warning("certbot: not installed")
        logger.info(f"  Fix: sudo {install_hint} certbot")
        warnings += 1
    logger.blank()

    logger.section("WASM Configuration")
    for label, path in (("Apps directory", DEFAULT_APPS_DIR), ("Log directory", DEFAULT_LOG_DIR)):
        if path.exists():
            logger.success(f"{label}: {path}")
        else:
            logger.error(f"{label}: {path} not found")
            logger.info("  Fix: sudo wasm setup init")
            issues += 1

    if DEFAULT_CONFIG_PATH.exists():
        logger.success(f"Config file: {DEFAULT_CONFIG_PATH}")
    else:
        logger.warning(f"Config file: {DEFAULT_CONFIG_PATH} not found")
        logger.info("  Fix: sudo wasm setup init")
        warnings += 1
    logger.blank()

    logger.section("SSH Configuration")
    from wasm.validators.ssh import ssh_key_exists, test_ssh_connection

    key_exists, key_path = ssh_key_exists()
    if key_exists:
        logger.success(f"SSH key: {key_path}")
        success, _ = test_ssh_connection("github.com")
        if success:
            logger.success("GitHub SSH: connected")
        else:
            logger.warning("GitHub SSH: not configured")
            logger.info("  Add your key to GitHub: https://github.com/settings/keys")
    else:
        logger.warning("SSH key: not found")
        logger.info("  Fix: wasm setup ssh --generate")
        warnings += 1
    logger.blank()

    logger.section("Summary")
    if not issues and not warnings:
        logger.success("All checks passed. This machine is ready for deployments.")
    elif not issues:
        logger.warning(f"{warnings} warning(s). Deployments will work, some features will not.")
    else:
        logger.error(f"{issues} issue(s) and {warnings} warning(s) found.")
        logger.info("Run 'sudo wasm setup init' to fix most of these automatically.")
    logger.blank()

    return 0 if issues == 0 else 1


def _report_unit_state(logger: Logger, unit: str) -> int:
    """
    Say whether a systemd unit is running.

    Args:
        logger: Logger used to report progress.
        unit: Systemd unit name.

    Returns:
        1 if the unit is not running, so the caller can count it as a warning.
    """
    result = run_command(["systemctl", "is-active", unit])
    if result.stdout.strip() == "active":
        logger.success(f"  {unit} service: running")
        return 0
    logger.warning(f"  {unit} service: not running")
    logger.info(f"  Fix: sudo systemctl start {unit}")
    return 1


# ---------------------------------------------------------------------------
# argparse bridge
# ---------------------------------------------------------------------------


def handle_setup(args: Namespace) -> int:
    """
    Dispatch a setup action parsed by argparse.

    Kept while the argparse parser is still wired up; both entry points call the
    same functions, so there is one implementation of each action.

    Args:
        args: Parsed arguments.

    Returns:
        Exit code.
    """
    logger = Logger(verbose=getattr(args, "verbose", False))
    action = getattr(args, "action", None)

    try:
        if action == "completions":
            return _run_completions(
                logger,
                shell=getattr(args, "shell", None),
                user_only=getattr(args, "user_only", False),
                to_stdout=False,
            )
        if action == "init":
            return _run_init(logger, assume_defaults=not _prompts_possible())
        if action == "permissions":
            return _run_permissions(logger)
        if action == "ssh":
            return _run_ssh(
                logger,
                generate=getattr(args, "generate", False),
                key_type=getattr(args, "key_type", "ed25519"),
                show=getattr(args, "show", False),
                test_host=getattr(args, "test_host", None),
            )
        if action == "doctor":
            return _run_doctor(logger)
    except WASMError as e:
        logger.error(str(e), details=e.details or "")
        return 1

    print(f"Unknown action: {action}", file=sys.stderr)
    return 1


# ---------------------------------------------------------------------------
# Click commands
# ---------------------------------------------------------------------------


@click.group("setup")
@global_flags
def cli() -> None:
    """Prepare this server, and check that it stayed prepared."""


@cli.command("init")
@click.option(
    "-y",
    "--yes",
    is_flag=True,
    help="Take the defaults instead of asking. Use this in provisioning scripts.",
)
@global_flags
@pass_context
def init(ctx: Context, yes: bool) -> None:
    """
    Install what deployments need and create WASM's directories.

    Needs root: it installs packages, writes to /etc and to /var.
    """
    _exit(_run_init(ctx.logger, assume_defaults=yes or not _prompts_possible()))


@cli.command("completions")
@click.option(
    "-s",
    "--shell",
    type=click.Choice(COMPLETION_SHELLS),
    help="Shell to generate for. Detected from $SHELL when omitted.",
)
@click.option(
    "-u",
    "--user-only",
    is_flag=True,
    help="Install for the current user instead of system-wide. Needs no root.",
)
@click.option(
    "--stdout",
    "to_stdout",
    is_flag=True,
    help="Print the script instead of installing it, to pipe or inspect.",
)
@global_flags
@pass_context
def completions(ctx: Context, shell: str | None, user_only: bool, to_stdout: bool) -> None:
    """
    Install tab completion for wasm.

    The script is generated from the command tree, so it never falls behind the
    commands it completes.
    """
    _exit(_run_completions(ctx.logger, shell=shell, user_only=user_only, to_stdout=to_stdout))


@cli.command("permissions")
@global_flags
@pass_context
def permissions(ctx: Context) -> None:
    """Check that WASM can write to the directories it owns."""
    _exit(_run_permissions(ctx.logger))


@cli.command("ssh")
@click.option("-g", "--generate", is_flag=True, help="Create a key if this machine has none.")
@click.option(
    "-t",
    "--type",
    "key_type",
    type=click.Choice(["ed25519", "rsa", "ecdsa"]),
    default="ed25519",
    show_default=True,
    help="Algorithm to generate the key with.",
)
@click.option("-T", "--test", "test_host", metavar="HOST", help="Try to connect to a Git host.")
@click.option("-S", "--show", is_flag=True, help="Print the public key to add to your Git host.")
@global_flags
@pass_context
def ssh(ctx: Context, generate: bool, key_type: str, test_host: str | None, show: bool) -> None:
    """
    Set up the SSH key WASM clones private repositories with.

    With no options it reports what is already configured.
    """
    _exit(
        _run_ssh(
            ctx.logger,
            generate=generate,
            key_type=key_type,
            show=show,
            test_host=test_host,
        )
    )


@cli.command("doctor")
@global_flags
@pass_context
def doctor(ctx: Context) -> None:
    """
    Diagnose this machine and say how to fix what is wrong.

    Exits non-zero when something deployments depend on is missing.
    """
    _exit(_run_doctor(ctx.logger))

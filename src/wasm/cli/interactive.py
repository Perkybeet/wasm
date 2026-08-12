"""
Interactive mode.

Prompts come from :mod:`wasm.cli.prompts`, which is built on questionary. The
previous implementation used inquirer, which is not packaged in Debian or
Ubuntu, so interactive mode never worked on the distributions most WASM users
run.
"""

from wasm.cli import prompts
from wasm.core.exceptions import WASMError
from wasm.core.logger import Logger
from wasm.validators.domain import check_domain
from wasm.validators.port import check_port
from wasm.validators.source import is_valid_source


class InteractiveMode:
    """
    Interactive mode handler.
    """

    def __init__(self, verbose: bool = False):
        """
        Initialize interactive mode.

        Args:
            verbose: Enable verbose output.
        """
        self.verbose = verbose
        self.logger = Logger(verbose=verbose)

        if not prompts.AVAILABLE:
            raise WASMError(
                "Interactive mode needs questionary, which is missing",
                details=(
                    "It is a hard dependency, so this means a broken install.\n"
                    "  pip install --force-reinstall wasm-cli\n"
                    "  or, on a distribution package: apt install python3-questionary"
                ),
            )

    def run(self) -> int:
        """
        Run interactive mode.

        Returns:
            Exit code.
        """
        self.logger.header("WASM Interactive Mode")
        self.logger.info("Answer the prompts to configure your operation")
        self.logger.blank()

        try:
            # Select action type
            action_type = self._prompt_action_type()

            if action_type == "webapp":
                return self._webapp_flow()
            elif action_type == "site":
                return self._site_flow()
            elif action_type == "service":
                return self._service_flow()
            elif action_type == "cert":
                return self._cert_flow()
            else:
                self.logger.warning("No action selected")
                return 0

        except KeyboardInterrupt:
            self.logger.blank()
            self.logger.info("Aborted")
            return 130

    def _prompt_action_type(self) -> str:
        """Prompt for action type."""
        questions = [
            prompts.List(
                "action_type",
                message="What would you like to do?",
                choices=[
                    ("🚀 Deploy/Manage Web Application", "webapp"),
                    ("🌐 Manage Site (web server configurations)", "site"),
                    ("⚙️  Manage Service (systemd services)", "service"),
                    ("🔒 Manage Certificate (SSL/TLS)", "cert"),
                ],
            ),
        ]

        answers = prompts.prompt(questions)
        return answers["action_type"] if answers else None

    def _webapp_flow(self) -> int:
        """Handle webapp interactive flow."""
        questions = [
            prompts.List(
                "action",
                message="What action would you like to perform?",
                choices=[
                    ("Create a new web application", "create"),
                    ("List deployed applications", "list"),
                    ("Show application status", "status"),
                    ("Update an application", "update"),
                    ("Restart an application", "restart"),
                    ("Stop an application", "stop"),
                    ("Start an application", "start"),
                    ("View application logs", "logs"),
                    ("Delete an application", "delete"),
                ],
            ),
        ]

        answers = prompts.prompt(questions)
        if not answers:
            return 0

        action = answers["action"]

        if action == "create":
            return self._webapp_create()
        elif action == "list":
            return self._run_webapp_command("list")
        elif action == "logs":
            domain = self._prompt_domain("Enter application domain")
            return self._webapp_logs(domain)
        elif action == "update":
            domain = self._prompt_domain("Enter application domain")
            return self._webapp_update(domain)
        elif action in ["status", "restart", "stop", "start"]:
            domain = self._prompt_domain("Enter application domain")
            return self._run_webapp_command(action, domain)
        elif action == "delete":
            domain = self._prompt_domain("Enter application domain")
            return self._run_webapp_command("delete", domain, force=True, keep_files=False)

        return 0

    def _run_webapp_command(self, action: str, domain: str | None = None, **kwargs) -> int:
        """Run a webapp command with arguments."""
        from argparse import Namespace

        args_dict = {
            "verbose": self.verbose,
            "action": action,
            **kwargs,
        }

        if domain:
            args_dict["domain"] = domain

        args = Namespace(**args_dict)

        from wasm.cli.commands.webapp import handle_webapp

        return handle_webapp(args)

    def _webapp_create(self) -> int:
        """Handle webapp create flow."""
        questions = [
            prompts.List(
                "type",
                message="Select application type",
                choices=[
                    ("⚡ Next.js", "nextjs"),
                    ("🟢 Node.js (Express, Fastify, etc.)", "nodejs"),
                    ("⚛️  Vite (React, Vue, Svelte)", "vite"),
                    ("🐍 Python (Django, Flask, FastAPI)", "python"),
                    ("📄 Static Site (HTML, Hugo, Jekyll)", "static"),
                    ("🔍 Auto-detect", "auto"),
                ],
            ),
            prompts.Text(
                "domain",
                message="Enter target domain",
                validate=lambda _, x: check_domain(x) or "Invalid domain name",
            ),
            prompts.Text(
                "source",
                message="Enter source (Git URL or path)",
                validate=lambda _, x: is_valid_source(x) or "Invalid source",
            ),
            prompts.Text(
                "port",
                message="Application port (leave empty for auto)",
                default="",
                validate=lambda _, x: x == "" or check_port(x) or "Invalid port",
            ),
            prompts.List(
                "webserver",
                message="Select web server",
                choices=[
                    ("Nginx", "nginx"),
                    ("Apache", "apache"),
                ],
                default="nginx",
            ),
            prompts.Text(
                "branch",
                message="Git branch (leave empty for default)",
                default="",
            ),
            prompts.Confirm(
                "ssl",
                message="Configure SSL certificate?",
                default=True,
            ),
            prompts.Text(
                "env_file",
                message="Path to environment file (leave empty to skip)",
                default="",
            ),
        ]

        answers = prompts.prompt(questions)
        if not answers:
            return 0

        # Ask about www if SSL enabled and domain is a base domain
        from wasm.validators.domain import should_include_www

        include_www = False
        if answers["ssl"] and should_include_www(answers["domain"]):
            www_questions = [
                prompts.Confirm(
                    "include_www",
                    message=f"Include www.{answers['domain']} in certificate and web server config?",
                    default=True,
                ),
            ]
            www_answers = prompts.prompt(www_questions)
            include_www = www_answers.get("include_www", True) if www_answers else False

        # Build arguments
        from argparse import Namespace

        args = Namespace(
            verbose=self.verbose,
            action="create",
            domain=answers["domain"],
            source=answers["source"],
            type=answers["type"],
            port=int(answers["port"]) if answers["port"] else None,
            webserver=answers["webserver"],
            branch=answers["branch"] or None,
            no_ssl=not answers["ssl"],
            env_file=answers["env_file"] or None,
            www=include_www,
        )

        from wasm.cli.commands.webapp import handle_webapp

        return handle_webapp(args)

    def _webapp_logs(self, domain: str) -> int:
        """Handle webapp logs flow with interactive prompts."""
        questions = [
            prompts.Text(
                "lines",
                message="Number of log lines to show",
                default="50",
                validate=lambda _, x: (x.isdigit() and int(x) > 0) or "Must be a positive number",
            ),
            prompts.Confirm(
                "follow",
                message="Follow log output in real time?",
                default=False,
            ),
        ]

        answers = prompts.prompt(questions)
        if not answers:
            return 0

        return self._run_webapp_command(
            "logs",
            domain,
            lines=int(answers["lines"]),
            follow=answers["follow"],
        )

    def _webapp_update(self, domain: str) -> int:
        """Handle webapp update flow with interactive prompts."""
        questions = [
            prompts.Text(
                "branch",
                message="Git branch to update from (leave empty for default)",
                default="",
            ),
        ]

        answers = prompts.prompt(questions)
        if not answers:
            return 0

        return self._run_webapp_command(
            "update",
            domain,
            branch=answers["branch"] or None,
        )

    def _site_flow(self) -> int:
        """Handle site interactive flow."""
        questions = [
            prompts.List(
                "action",
                message="What action would you like to perform?",
                choices=[
                    ("Create a new site configuration", "create"),
                    ("List all sites", "list"),
                    ("Enable a site", "enable"),
                    ("Disable a site", "disable"),
                    ("Show site configuration", "show"),
                    ("Delete a site", "delete"),
                ],
            ),
        ]

        answers = prompts.prompt(questions)
        if not answers:
            return 0

        action = answers["action"]

        if action == "create":
            return self._site_create()
        elif action == "list":
            return self._run_command("site", "list", webserver="all")
        elif action in ["enable", "disable", "show", "delete"]:
            domain = self._prompt_domain("Enter site domain")
            if action == "delete":
                return self._run_command("site", action, domain, force=True)
            return self._run_command("site", action, domain)

        return 0

    def _site_create(self) -> int:
        """Handle site create flow."""
        questions = [
            prompts.Text(
                "domain",
                message="Enter domain name",
                validate=lambda _, x: check_domain(x) or "Invalid domain name",
            ),
            prompts.List(
                "webserver",
                message="Select web server",
                choices=[
                    ("Nginx", "nginx"),
                    ("Apache", "apache"),
                ],
            ),
            prompts.List(
                "template",
                message="Select configuration template",
                choices=[
                    ("Reverse Proxy", "proxy"),
                    ("Static Site", "static"),
                ],
            ),
            prompts.Text(
                "port",
                message="Backend port (for proxy)",
                default="3000",
                validate=lambda _, x: check_port(x) or "Invalid port",
            ),
            prompts.Confirm(
                "ssl",
                message="Configure SSL certificate?",
                default=True,
            ),
        ]

        answers = prompts.prompt(questions)
        if not answers:
            return 0

        # Ask about www if SSL enabled and domain is a base domain
        from wasm.validators.domain import should_include_www

        include_www = False
        if answers["ssl"] and should_include_www(answers["domain"]):
            www_questions = [
                prompts.Confirm(
                    "include_www",
                    message=f"Include www.{answers['domain']} in certificate and web server config?",
                    default=True,
                ),
            ]
            www_answers = prompts.prompt(www_questions)
            include_www = www_answers.get("include_www", True) if www_answers else False

        from argparse import Namespace

        args = Namespace(
            verbose=self.verbose,
            action="create",
            domain=answers["domain"],
            webserver=answers["webserver"],
            template=answers["template"],
            port=int(answers["port"]),
            no_ssl=not answers["ssl"],
            www=include_www,
        )

        from wasm.cli.commands.site import handle_site

        return handle_site(args)

    def _service_flow(self) -> int:
        """Handle service interactive flow."""
        questions = [
            prompts.List(
                "action",
                message="What action would you like to perform?",
                choices=[
                    ("Create a new service", "create"),
                    ("List managed services", "list"),
                    ("Show service status", "status"),
                    ("Start a service", "start"),
                    ("Stop a service", "stop"),
                    ("Restart a service", "restart"),
                    ("View service logs", "logs"),
                    ("Delete a service", "delete"),
                ],
            ),
        ]

        answers = prompts.prompt(questions)
        if not answers:
            return 0

        action = answers["action"]

        if action == "create":
            return self._service_create()
        elif action == "list":
            return self._run_command("service", "list")
        elif action == "logs":
            name = self._prompt_text("Enter service name")
            return self._service_logs(name)
        elif action in ["status", "start", "stop", "restart", "delete"]:
            name = self._prompt_text("Enter service name")
            if action == "delete":
                return self._run_command("service", action, name, force=True)
            return self._run_command("service", action, name)

        return 0

    def _service_create(self) -> int:
        """Handle service create flow."""
        questions = [
            prompts.Text(
                "name",
                message="Enter service name",
                validate=lambda _, x: len(x) > 0 or "Name required",
            ),
            prompts.Text(
                "command",
                message="Enter command to run",
                validate=lambda _, x: len(x) > 0 or "Command required",
            ),
            prompts.Text(
                "directory",
                message="Enter working directory",
                validate=lambda _, x: len(x) > 0 or "Directory required",
            ),
            prompts.Text(
                "user",
                message="User to run as",
                default="www-data",
            ),
            prompts.Text(
                "description",
                message="Service description",
                default="",
            ),
        ]

        answers = prompts.prompt(questions)
        if not answers:
            return 0

        from argparse import Namespace

        args = Namespace(
            verbose=self.verbose,
            action="create",
            name=answers["name"],
            exec_command=answers["command"],
            directory=answers["directory"],
            user=answers["user"],
            description=answers["description"] or None,
        )

        from wasm.cli.commands.service import handle_service

        return handle_service(args)

    def _service_logs(self, name: str) -> int:
        """Handle service logs flow with interactive prompts."""
        questions = [
            prompts.Text(
                "lines",
                message="Number of log lines to show",
                default="50",
                validate=lambda _, x: (x.isdigit() and int(x) > 0) or "Must be a positive number",
            ),
            prompts.Confirm(
                "follow",
                message="Follow log output in real time?",
                default=False,
            ),
        ]

        answers = prompts.prompt(questions)
        if not answers:
            return 0

        return self._run_command(
            "service",
            "logs",
            name,
            lines=int(answers["lines"]),
            follow=answers["follow"],
        )

    def _cert_flow(self) -> int:
        """Handle cert interactive flow."""
        questions = [
            prompts.List(
                "action",
                message="What action would you like to perform?",
                choices=[
                    ("Obtain a new certificate", "create"),
                    ("List all certificates", "list"),
                    ("Show certificate info", "info"),
                    ("Renew certificates", "renew"),
                    ("Revoke a certificate", "revoke"),
                    ("Delete a certificate", "delete"),
                ],
            ),
        ]

        answers = prompts.prompt(questions)
        if not answers:
            return 0

        action = answers["action"]

        if action == "create":
            return self._cert_create()
        elif action == "list":
            return self._run_command("cert", "list")
        elif action == "renew":
            return self._cert_renew()
        elif action in ["info", "revoke", "delete"]:
            domain = self._prompt_domain("Enter domain name")
            if action == "delete":
                return self._run_command("cert", action, domain, force=True)
            elif action == "revoke":
                return self._run_command("cert", action, domain, delete=True)
            return self._run_command("cert", action, domain)

        return 0

    def _cert_create(self) -> int:
        """Handle cert create flow."""
        questions = [
            prompts.Text(
                "domain",
                message="Enter primary domain",
                validate=lambda _, x: check_domain(x) or "Invalid domain",
            ),
            prompts.Text(
                "additional",
                message="Additional domains (comma separated, or leave empty)",
                default="",
            ),
            prompts.Text(
                "email",
                message="Email for registration (leave empty for default)",
                default="",
            ),
            prompts.List(
                "method",
                message="Certificate obtention method",
                choices=[
                    ("Nginx plugin", "nginx"),
                    ("Apache plugin", "apache"),
                    ("Standalone", "standalone"),
                    ("Webroot", "webroot"),
                ],
            ),
            prompts.Confirm(
                "dry_run",
                message="Dry run (test without obtaining)?",
                default=False,
            ),
        ]

        answers = prompts.prompt(questions)
        if not answers:
            return 0

        # Parse domains
        domains = [answers["domain"]]
        if answers["additional"]:
            additional = [d.strip() for d in answers["additional"].split(",")]
            domains.extend(additional)

        from argparse import Namespace

        args = Namespace(
            verbose=self.verbose,
            action="create",
            domain=domains,
            email=answers["email"] or None,
            webroot=None,
            standalone=answers["method"] == "standalone",
            nginx=answers["method"] == "nginx",
            apache=answers["method"] == "apache",
            dry_run=answers["dry_run"],
        )

        if answers["method"] == "webroot":
            webroot = self._prompt_text("Enter webroot path")
            args.webroot = webroot

        from wasm.cli.commands.cert import handle_cert

        return handle_cert(args)

    def _cert_renew(self) -> int:
        """Handle cert renew flow."""
        questions = [
            prompts.List(
                "scope",
                message="What to renew?",
                choices=[
                    ("All certificates", "all"),
                    ("Specific certificate", "specific"),
                ],
            ),
            prompts.Confirm(
                "force",
                message="Force renewal?",
                default=False,
            ),
            prompts.Confirm(
                "dry_run",
                message="Dry run?",
                default=False,
            ),
        ]

        answers = prompts.prompt(questions)
        if not answers:
            return 0

        domain = None
        if answers["scope"] == "specific":
            domain = self._prompt_domain("Enter domain name")

        from argparse import Namespace

        args = Namespace(
            verbose=self.verbose,
            action="renew",
            domain=domain,
            force=answers["force"],
            dry_run=answers["dry_run"],
        )

        from wasm.cli.commands.cert import handle_cert

        return handle_cert(args)

    def _prompt_domain(self, message: str) -> str:
        """Prompt for a domain name."""
        questions = [
            prompts.Text(
                "domain",
                message=message,
                validate=lambda _, x: check_domain(x) or "Invalid domain",
            ),
        ]
        answers = prompts.prompt(questions)
        return answers["domain"] if answers else None

    def _prompt_text(self, message: str, default: str = "") -> str:
        """Prompt for text input."""
        questions = [
            prompts.Text(
                "value",
                message=message,
                default=default,
            ),
        ]
        answers = prompts.prompt(questions)
        return answers["value"] if answers else default

    def _run_command(self, resource: str, action: str, target: str | None = None, **kwargs) -> int:
        """Run a command with arguments."""
        from argparse import Namespace

        args_dict = {
            "verbose": self.verbose,
            "action": action,
            **kwargs,
        }

        # Add target based on resource type
        if target:
            if resource in ["webapp", "site", "cert"]:
                args_dict["domain"] = target
            elif resource == "service":
                args_dict["name"] = target

        args = Namespace(**args_dict)

        if resource == "webapp":
            from wasm.cli.commands.webapp import handle_webapp

            return handle_webapp(args)
        elif resource == "site":
            from wasm.cli.commands.site import handle_site

            return handle_site(args)
        elif resource == "service":
            from wasm.cli.commands.service import handle_service

            return handle_service(args)
        elif resource == "cert":
            from wasm.cli.commands.cert import handle_cert

            return handle_cert(args)

        return 1

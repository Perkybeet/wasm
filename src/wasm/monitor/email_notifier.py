"""
Email delivery for monitor observations.

Three properties matter here, and each maps to a defect this module used to
have:

- **Every socket has a deadline.** The monitor loop is single threaded. An SMTP
  connection without a timeout does not fail, it hangs, and the daemon stops
  monitoring forever while systemd still reports it as active.
- **Credentials never cross a plaintext session.** Authenticating over an
  unencrypted connection puts the password on the wire; the notifier refuses.
- **The password never reaches a log.** Server replies are echoed into error
  details, and some servers echo back what was sent, so details are redacted.
"""

from __future__ import annotations

import smtplib
import socket
import ssl
from dataclasses import dataclass, field
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape
from typing import Any

from wasm.core.config import Config
from wasm.core.exceptions import EmailError
from wasm.core.logger import Logger
from wasm.monitor.models import SEVERITY_WARNING, ProcessObservation

#: Deadline for every SMTP socket operation, in seconds. Long enough for a slow
#: relay, short enough that a scan loop recovers within one interval.
DEFAULT_SMTP_TIMEOUT = 30

#: Beyond this a "timeout" stops protecting the scan loop it exists to protect.
MAX_SMTP_TIMEOUT = 120

_REDACTED = "***"


@dataclass
class SMTPConfig:
    """
    Connection settings for the outgoing mail server.

    Attributes:
        host: SMTP server hostname.
        port: SMTP server port.
        username: Account used to authenticate, empty for anonymous relays.
        password: Password for that account.
        use_ssl: Connect with implicit TLS (SMTPS, usually port 465).
        use_tls: Connect in the clear and upgrade with STARTTLS (usually 587).
        from_address: Envelope sender. Defaults to the username.
        timeout: Socket deadline in seconds.
    """

    host: str
    port: int
    username: str = ""
    password: str = ""
    use_ssl: bool = True
    use_tls: bool = False
    from_address: str | None = None
    timeout: int = DEFAULT_SMTP_TIMEOUT

    def __post_init__(self) -> None:
        """
        Normalise the sender and reject a deadline that is not one.

        Raises:
            EmailError: When the timeout is missing, zero or negative.
        """
        if not self.from_address:
            self.from_address = self.username
        if not self.timeout or self.timeout <= 0:
            raise EmailError(
                "SMTP timeout must be a positive number of seconds",
                details=(
                    "A missing or zero timeout hangs the monitor loop forever. "
                    f"Set monitor.smtp.timeout to a value between 1 and {MAX_SMTP_TIMEOUT}."
                ),
            )
        self.timeout = min(int(self.timeout), MAX_SMTP_TIMEOUT)

    @property
    def secrets(self) -> tuple[str, ...]:
        """Values that must never appear in a log or an error message."""
        return tuple(value for value in (self.password,) if value)


@dataclass
class EmailContent:
    """
    A rendered message, ready to be handed to the server.

    Attributes:
        subject: Message subject.
        text: Plain text body.
        html: HTML body.
        headers: Extra headers to set.
    """

    subject: str
    text: str
    html: str
    headers: dict[str, str] = field(default_factory=dict)


class EmailNotifier:
    """Sends monitor observations by email."""

    def __init__(
        self,
        smtp_config: SMTPConfig | None = None,
        recipients: list[str] | None = None,
        verbose: bool = False,
    ) -> None:
        """
        Args:
            smtp_config: Server settings. Loaded from the global config if None.
            recipients: Destination addresses. Loaded from the config if None.
            verbose: Enable verbose logging.
        """
        self.logger = Logger(verbose=verbose)
        self.config = Config()
        self.config.reload()

        self.smtp_config = smtp_config or self._load_smtp_config()
        self.recipients = recipients if recipients is not None else self._load_recipients()

    def _load_smtp_config(self) -> SMTPConfig:
        """
        Build the server settings from the global configuration.

        Returns:
            The SMTP settings.

        Raises:
            EmailError: When the configured timeout is not positive.
        """
        return SMTPConfig(
            host=self.config.get("monitor.smtp.host", ""),
            port=int(self.config.get("monitor.smtp.port", 465)),
            username=self.config.get("monitor.smtp.username", ""),
            password=self.config.get("monitor.smtp.password", ""),
            use_ssl=bool(self.config.get("monitor.smtp.use_ssl", True)),
            use_tls=bool(self.config.get("monitor.smtp.use_tls", False)),
            from_address=self.config.get("monitor.smtp.from_address", ""),
            timeout=int(self.config.get("monitor.smtp.timeout", DEFAULT_SMTP_TIMEOUT)),
        )

    def _load_recipients(self) -> list[str]:
        """
        Read the destination addresses from the global configuration.

        Returns:
            The configured recipients, possibly empty.
        """
        recipients = self.config.get("monitor.email_recipients", [])
        if isinstance(recipients, str):
            return [recipients]
        return list(recipients or [])

    def _redact(self, text: str) -> str:
        """
        Strip credentials out of a message before it is logged or raised.

        Args:
            text: Text that may quote a server reply.

        Returns:
            The text with every known secret replaced.
        """
        for secret in self.smtp_config.secrets:
            text = text.replace(secret, _REDACTED)
        return text

    @property
    def is_configured(self) -> bool:
        """True when there is a server to talk to and someone to talk about."""
        return bool(self.smtp_config.host and self.recipients)

    def _create_connection(self) -> smtplib.SMTP:
        """
        Open an authenticated connection to the mail server.

        Returns:
            The connected client.

        Raises:
            EmailError: When the transport is insecure or the server refuses.
        """
        config = self.smtp_config
        needs_login = bool(config.username or config.password)

        if needs_login and not (config.use_ssl or config.use_tls):
            raise EmailError(
                "Refusing to send SMTP credentials over an unencrypted connection",
                details=(
                    "Set monitor.smtp.use_ssl (port 465) or monitor.smtp.use_tls (port 587). "
                    "Only an anonymous local relay may run without encryption."
                ),
            )

        context = ssl.create_default_context()

        try:
            if config.use_ssl:
                server: smtplib.SMTP = smtplib.SMTP_SSL(
                    config.host,
                    config.port,
                    context=context,
                    timeout=config.timeout,
                )
            else:
                server = smtplib.SMTP(config.host, config.port, timeout=config.timeout)
                if config.use_tls:
                    server.starttls(context=context)

            if needs_login:
                server.login(config.username, config.password)
            return server

        except smtplib.SMTPAuthenticationError as exc:
            raise EmailError(
                "SMTP authentication failed",
                details=self._redact(f"Check monitor.smtp.username and password: {exc}"),
            ) from exc
        except smtplib.SMTPConnectError as exc:
            raise EmailError(
                "Failed to connect to the SMTP server",
                details=self._redact(f"{config.host}:{config.port} - {exc}"),
            ) from exc
        except (smtplib.SMTPException, ssl.SSLError, TimeoutError, OSError) as exc:
            raise EmailError(
                "Failed to establish an SMTP connection",
                details=self._redact(
                    f"{config.host}:{config.port} (timeout {config.timeout}s) - {exc}"
                ),
            ) from exc

    def _send(self, content: EmailContent) -> bool:
        """
        Deliver a rendered message.

        Args:
            content: The message to send.

        Returns:
            True when the server accepted the message.

        Raises:
            EmailError: When the message could not be delivered.
        """
        message = MIMEMultipart("alternative")
        message["Subject"] = content.subject
        message["From"] = self.smtp_config.from_address or self.smtp_config.username
        message["To"] = ", ".join(self.recipients)
        for header, value in content.headers.items():
            message[header] = value
        message.attach(MIMEText(content.text, "plain"))
        message.attach(MIMEText(content.html, "html"))

        server = self._create_connection()
        try:
            server.sendmail(
                self.smtp_config.from_address or self.smtp_config.username,
                self.recipients,
                message.as_string(),
            )
        except (smtplib.SMTPException, TimeoutError, OSError) as exc:
            raise EmailError(
                "Failed to send the notification email",
                details=self._redact(str(exc)),
            ) from exc
        finally:
            try:
                server.quit()
            except (smtplib.SMTPException, OSError) as exc:
                self.logger.debug(
                    f"SMTP connection did not close cleanly: {self._redact(str(exc))}"
                )

        self.logger.debug(f"Sent '{content.subject}' to {len(self.recipients)} recipient(s)")
        return True

    def _hostname(self) -> str:
        """
        Return the machine name used in subjects and bodies.

        Returns:
            The hostname, or "unknown" when it cannot be resolved.
        """
        try:
            return socket.gethostname()
        except OSError:
            return "unknown"

    def render_observations(self, observations: list[ProcessObservation]) -> EmailContent:
        """
        Render an observation report.

        Args:
            observations: What the scan noticed.

        Returns:
            The message to send.
        """
        hostname = self._hostname()
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        warnings = sum(1 for o in observations if o.severity == SEVERITY_WARNING)

        subject = f"[WASM] {len(observations)} process observation(s) on {hostname}"

        lines = [
            "WASM monitor - process observations",
            "=" * 60,
            "",
            f"Server: {hostname}",
            f"Time:   {timestamp}",
            f"Noted:  {len(observations)} process(es), {warnings} of them as warnings",
            "",
            "The monitor reports only. No process was signalled and no file was",
            "touched. Review each entry before taking any action.",
            "",
            "-" * 60,
        ]
        for observation in observations:
            process = observation.process
            lines.extend(
                [
                    "",
                    f"[{observation.severity.upper()}] {process.name} (PID {process.pid})",
                    f"  Signal:  {observation.signal}",
                    f"  User:    {process.user}",
                    f"  CPU:     {process.cpu_percent:.1f}%",
                    f"  Memory:  {process.memory_percent:.1f}%",
                    f"  Detail:  {observation.detail}",
                    f"  Command: {process.command}",
                ]
            )
            if process.parent_pid:
                lines.append(f"  Parent:  {process.parent_name or '?'} (PID {process.parent_pid})")

        rows = "".join(
            f"""
        <tr>
            <td>{o.severity.upper()}</td>
            <td>{_escape(o.process.name)} (PID {o.process.pid})</td>
            <td>{o.process.user}</td>
            <td>{o.process.cpu_percent:.1f}%</td>
            <td>{o.process.memory_percent:.1f}%</td>
            <td>{_escape(o.signal)}: {_escape(o.detail)}<br>
                <code>{_escape(o.process.command)}</code></td>
        </tr>"""
            for o in observations
        )

        html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>{subject}</title></head>
<body style="font-family: system-ui, sans-serif; color: #222;">
    <h2>WASM monitor - process observations</h2>
    <p><strong>Server:</strong> {hostname}<br>
       <strong>Time:</strong> {timestamp}<br>
       <strong>Noted:</strong> {len(observations)} process(es), {warnings} as warnings</p>
    <p>The monitor reports only. No process was signalled and no file was touched.
       Review each entry before taking any action.</p>
    <table border="1" cellpadding="6" cellspacing="0" style="border-collapse: collapse;">
        <tr><th>Severity</th><th>Process</th><th>User</th><th>CPU</th><th>Memory</th><th>Why</th></tr>{rows}
    </table>
</body>
</html>"""

        return EmailContent(subject=subject, text="\n".join(lines), html=html)

    def send_observation_alert(self, observations: list[ProcessObservation]) -> bool:
        """
        Email a set of observations.

        Args:
            observations: What the scan noticed.

        Returns:
            True when the report was sent, False when there was nothing to send
            or no working configuration.

        Raises:
            EmailError: When delivery fails.
        """
        if not observations:
            return False
        if not self.is_configured:
            self.logger.debug("SMTP or recipients not configured, skipping notification")
            return False

        return self._send(self.render_observations(observations))

    def send_test_email(self) -> bool:
        """
        Send a message that proves the configuration works.

        Returns:
            True when the message was accepted.

        Raises:
            EmailError: When delivery fails.
        """
        hostname = self._hostname()
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        text = (
            "WASM monitor - test email\n"
            "=========================\n\n"
            f"Server: {hostname}\n"
            f"Time:   {timestamp}\n\n"
            "Receiving this means monitor notifications are configured correctly."
        )
        html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>WASM monitor test email</title></head>
<body style="font-family: system-ui, sans-serif; color: #222;">
    <h2>WASM monitor - test email</h2>
    <p><strong>Server:</strong> {hostname}<br>
       <strong>Time:</strong> {timestamp}</p>
    <p>Receiving this means monitor notifications are configured correctly.</p>
</body>
</html>"""

        return self._send(
            EmailContent(
                subject=f"[WASM] Test email - {hostname}",
                text=text,
                html=html,
            )
        )


def _escape(value: Any) -> str:
    """
    Escape a value for inclusion in the HTML body.

    Command lines come from other users on the machine and must not be able to
    inject markup into a report an administrator opens.

    Args:
        value: The value to render.

    Returns:
        The escaped string.
    """
    return escape(str(value), quote=True)

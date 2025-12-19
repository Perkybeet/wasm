"""
Email notification system for WASM process monitoring.

Sends alerts and reports via SMTP when suspicious or malicious
processes are detected.
"""

import smtplib
import ssl
from dataclasses import dataclass
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Dict, List, Optional

from wasm.core.config import Config
from wasm.core.exceptions import EmailError
from wasm.core.logger import Logger


@dataclass
class SMTPConfig:
    """SMTP server configuration."""
    
    host: str
    port: int
    username: str
    password: str
    use_ssl: bool = True
    use_tls: bool = False
    from_address: Optional[str] = None
    
    def __post_init__(self):
        if not self.from_address:
            self.from_address = self.username


@dataclass
class ThreatReport:
    """Report of detected threat."""
    
    process_name: str
    pid: int
    user: str
    cpu_percent: float
    memory_percent: float
    command: str
    threat_level: str  # "suspicious", "malicious"
    confidence: float
    reason: str
    parent_pid: Optional[int] = None
    parent_name: Optional[str] = None
    action_taken: Optional[str] = None
    timestamp: Optional[datetime] = None
    
    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now()


class EmailNotifier:
    """
    Email notification system for process monitoring alerts.
    
    Sends formatted HTML emails when threats are detected and
    after mitigation actions are completed.
    """
    
    def __init__(
        self,
        smtp_config: Optional[SMTPConfig] = None,
        recipients: Optional[List[str]] = None,
        verbose: bool = False,
    ):
        """
        Initialize email notifier.
        
        Args:
            smtp_config: SMTP server configuration. If None, loads from config.
            recipients: List of email recipients. If None, loads from config.
            verbose: Enable verbose logging.
        """
        self.logger = Logger(verbose=verbose)
        self.config = Config()
        # Always reload config to get latest values
        self.config.reload()
        
        if smtp_config:
            self.smtp_config = smtp_config
        else:
            self.smtp_config = self._load_smtp_config()
        
        if recipients:
            self.recipients = recipients
        else:
            self.recipients = self._load_recipients()
    
    def _load_smtp_config(self) -> SMTPConfig:
        """Load SMTP configuration from global config."""
        return SMTPConfig(
            host=self.config.get("monitor.smtp.host", ""),
            port=self.config.get("monitor.smtp.port", 465),
            username=self.config.get("monitor.smtp.username", ""),
            password=self.config.get("monitor.smtp.password", ""),
            use_ssl=self.config.get("monitor.smtp.use_ssl", True),
            use_tls=self.config.get("monitor.smtp.use_tls", False),
            from_address=self.config.get("monitor.smtp.from_address", ""),
        )
    
    def _load_recipients(self) -> List[str]:
        """Load email recipients from global config."""
        recipients = self.config.get("monitor.email_recipients", [])
        if isinstance(recipients, str):
            return [recipients]
        return recipients or []
    
    def _create_connection(self) -> smtplib.SMTP:
        """
        Create SMTP connection.
        
        Returns:
            Connected SMTP object.
            
        Raises:
            EmailError: If connection fails.
        """
        try:
            if self.smtp_config.use_ssl:
                context = ssl.create_default_context()
                server = smtplib.SMTP_SSL(
                    self.smtp_config.host,
                    self.smtp_config.port,
                    context=context,
                )
            else:
                server = smtplib.SMTP(
                    self.smtp_config.host,
                    self.smtp_config.port,
                )
                if self.smtp_config.use_tls:
                    server.starttls()
            
            server.login(self.smtp_config.username, self.smtp_config.password)
            return server
            
        except smtplib.SMTPAuthenticationError as e:
            raise EmailError(
                "SMTP authentication failed",
                details=f"Check username and password: {e}",
            )
        except smtplib.SMTPConnectError as e:
            raise EmailError(
                "Failed to connect to SMTP server",
                details=f"Host: {self.smtp_config.host}:{self.smtp_config.port} - {e}",
            )
        except Exception as e:
            raise EmailError(
                "Failed to establish SMTP connection",
                details=str(e),
            )
    
    def _generate_threat_html(self, reports: List[ThreatReport], is_final: bool = False) -> str:
        """
        Generate HTML content for threat report email.
        
        Args:
            reports: List of threat reports.
            is_final: Whether this is the final mitigation report.
            
        Returns:
            HTML formatted email content.
        """
        hostname = self._get_hostname()
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        if is_final:
            title = "🛡️ WASM Security Monitor - Mitigation Report"
            subtitle = "Actions taken to neutralize detected threats"
            color = "#28a745"
        else:
            # Check if any malicious
            has_malicious = any(r.threat_level == "malicious" for r in reports)
            if has_malicious:
                title = "🚨 WASM Security Alert - MALICIOUS PROCESS DETECTED"
                color = "#dc3545"
            else:
                title = "⚠️ WASM Security Alert - Suspicious Activity Detected"
                color = "#ffc107"
            subtitle = "Immediate attention may be required"
        
        html = f"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif;
            line-height: 1.6;
            color: #333;
            margin: 0;
            padding: 0;
            background-color: #f4f4f4;
        }}
        .wrapper {{
            width: 100%;
            background-color: #f4f4f4;
            padding: 20px 0;
        }}
        .container {{
            width: 600px;
            max-width: 600px;
            margin: 0 auto;
            background-color: #ffffff;
        }}
        .header {{
            background: {color};
            color: white;
            padding: 20px;
            text-align: center;
            width: 560px;
        }}
        .header h1 {{
            margin: 0;
            font-size: 22px;
        }}
        .header p {{
            margin: 10px 0 0;
            opacity: 0.9;
            font-size: 14px;
        }}
        .content {{
            background: #f8f9fa;
            padding: 20px;
            width: 560px;
        }}
        .info-box {{
            background: white;
            padding: 15px;
            margin-bottom: 20px;
            border-left: 4px solid {color};
        }}
        .threat-card {{
            background: white;
            border: 1px solid #ddd;
            margin-bottom: 15px;
            overflow: hidden;
        }}
        .threat-header {{
            padding: 12px 15px;
            font-weight: bold;
            border-bottom: 1px solid #ddd;
        }}
        .threat-malicious {{
            background: #f8d7da;
            color: #721c24;
        }}
        .threat-suspicious {{
            background: #fff3cd;
            color: #856404;
        }}
        .threat-neutralized {{
            background: #d4edda;
            color: #155724;
        }}
        .threat-body {{
            padding: 15px;
        }}
        .threat-body table {{
            width: 100%;
            border-collapse: collapse;
        }}
        .threat-body td {{
            padding: 5px 0;
            vertical-align: top;
        }}
        .threat-body td:first-child {{
            font-weight: bold;
            width: 140px;
            color: #666;
        }}
        .command-box {{
            background: #2d2d2d;
            color: #f8f8f2;
            padding: 10px;
            border-radius: 4px;
            font-family: 'Courier New', monospace;
            font-size: 12px;
            word-break: break-all;
            margin-top: 10px;
        }}
        .badge {{
            display: inline-block;
            padding: 3px 8px;
            border-radius: 12px;
            font-size: 12px;
            font-weight: bold;
        }}
        .badge-danger {{
            background: #dc3545;
            color: white;
        }}
        .badge-warning {{
            background: #ffc107;
            color: #333;
        }}
        .badge-success {{
            background: #28a745;
            color: white;
        }}
        .footer {{
            text-align: center;
            padding: 20px;
            color: #666;
            font-size: 12px;
            width: 560px;
            background: #ffffff;
        }}
    </style>
</head>
<body>
    <div class="wrapper">
        <table cellpadding="0" cellspacing="0" border="0" width="600" align="center" style="margin: 0 auto;">
            <tr>
                <td>
                    <div class="container">
                        <div class="header">
                            <h1>{title}</h1>
                            <p>{subtitle}</p>
                        </div>
                        <div class="content">
                            <div class="info-box">
                                <strong>🖥️ Server:</strong> {hostname}<br>
                                <strong>🕐 Time:</strong> {timestamp}<br>
                                <strong>📊 Threats detected:</strong> {len(reports)}
                            </div>
"""
        
        for report in reports:
            if is_final and report.action_taken:
                threat_class = "threat-neutralized"
                level_badge = '<span class="badge badge-success">NEUTRALIZED</span>'
            elif report.threat_level == "malicious":
                threat_class = "threat-malicious"
                level_badge = '<span class="badge badge-danger">MALICIOUS</span>'
            else:
                threat_class = "threat-suspicious"
                level_badge = '<span class="badge badge-warning">SUSPICIOUS</span>'
            
            html += f"""
                            <div class="threat-card">
                                <div class="threat-header {threat_class}">
                                    {level_badge} {report.process_name} (PID: {report.pid})
                                </div>
                                <div class="threat-body">
                                    <table>
                                        <tr>
                                            <td>User:</td>
                                            <td>{report.user}</td>
                                        </tr>
                                        <tr>
                                            <td>CPU Usage:</td>
                                            <td>{report.cpu_percent:.1f}%</td>
                                        </tr>
                                        <tr>
                                            <td>Memory Usage:</td>
                                            <td>{report.memory_percent:.1f}%</td>
                                        </tr>
                                        <tr>
                                            <td>Confidence:</td>
                                            <td>{report.confidence * 100:.0f}%</td>
                                        </tr>
                                        <tr>
                                            <td>Reason:</td>
                                            <td>{report.reason}</td>
                                        </tr>
"""
            
            if report.parent_pid and report.parent_name:
                html += f"""
                                        <tr>
                                            <td>Parent Process:</td>
                                            <td>{report.parent_name} (PID: {report.parent_pid})</td>
                                        </tr>
"""
            
            if is_final and report.action_taken:
                html += f"""
                                        <tr>
                                            <td>Action Taken:</td>
                                            <td><strong>{report.action_taken}</strong></td>
                                        </tr>
"""
            
            html += f"""
                                    </table>
                                    <div class="command-box">{report.command}</div>
                                </div>
                            </div>
"""
        
        html += """
                        </div>
                        <div class="footer">
                            <p>This is an automated message from WASM Security Monitor.<br>
                            Please do not reply to this email.</p>
                        </div>
                    </div>
                </td>
            </tr>
        </table>
    </div>
</body>
</html>
"""
        return html
    
    def _generate_threat_text(self, reports: List[ThreatReport], is_final: bool = False) -> str:
        """
        Generate plain text content for threat report email.
        
        Args:
            reports: List of threat reports.
            is_final: Whether this is the final mitigation report.
            
        Returns:
            Plain text formatted email content.
        """
        hostname = self._get_hostname()
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        if is_final:
            title = "WASM Security Monitor - Mitigation Report"
        else:
            has_malicious = any(r.threat_level == "malicious" for r in reports)
            if has_malicious:
                title = "WASM Security Alert - MALICIOUS PROCESS DETECTED"
            else:
                title = "WASM Security Alert - Suspicious Activity Detected"
        
        lines = [
            "=" * 60,
            title,
            "=" * 60,
            "",
            f"Server: {hostname}",
            f"Time: {timestamp}",
            f"Threats detected: {len(reports)}",
            "",
            "-" * 60,
        ]
        
        for report in reports:
            lines.extend([
                "",
                f"[{report.threat_level.upper()}] {report.process_name} (PID: {report.pid})",
                f"  User: {report.user}",
                f"  CPU: {report.cpu_percent:.1f}% | Memory: {report.memory_percent:.1f}%",
                f"  Confidence: {report.confidence * 100:.0f}%",
                f"  Reason: {report.reason}",
            ])
            
            if report.parent_pid and report.parent_name:
                lines.append(f"  Parent: {report.parent_name} (PID: {report.parent_pid})")
            
            if is_final and report.action_taken:
                lines.append(f"  Action: {report.action_taken}")
            
            lines.extend([
                f"  Command: {report.command}",
                "",
            ])
        
        lines.extend([
            "-" * 60,
            "",
            "This is an automated message from WASM Security Monitor.",
        ])
        
        return "\n".join(lines)
    
    def _get_hostname(self) -> str:
        """Get system hostname."""
        import socket
        try:
            return socket.gethostname()
        except Exception:
            return "unknown"
    
    def send_threat_alert(
        self,
        reports: List[ThreatReport],
        is_final: bool = False,
    ) -> bool:
        """
        Send threat alert email.
        
        Args:
            reports: List of threat reports.
            is_final: Whether this is the final mitigation report.
            
        Returns:
            True if email sent successfully.
            
        Raises:
            EmailError: If sending fails.
        """
        if not self.recipients:
            self.logger.warning("No email recipients configured")
            return False
        
        if not self.smtp_config.host or not self.smtp_config.username:
            self.logger.warning("SMTP not configured")
            return False
        
        # Determine subject
        hostname = self._get_hostname()
        if is_final:
            subject = f"[WASM] Mitigation Complete - {hostname}"
        else:
            has_malicious = any(r.threat_level == "malicious" for r in reports)
            if has_malicious:
                subject = f"[WASM] 🚨 CRITICAL: Malicious Process Detected - {hostname}"
            else:
                subject = f"[WASM] ⚠️ Warning: Suspicious Activity - {hostname}"
        
        # Create message
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = self.smtp_config.from_address
        msg["To"] = ", ".join(self.recipients)
        
        # Attach both plain text and HTML versions
        text_content = self._generate_threat_text(reports, is_final)
        html_content = self._generate_threat_html(reports, is_final)
        
        msg.attach(MIMEText(text_content, "plain"))
        msg.attach(MIMEText(html_content, "html"))
        
        try:
            self.logger.debug(f"Connecting to SMTP server: {self.smtp_config.host}")
            server = self._create_connection()
            
            self.logger.debug(f"Sending email to: {', '.join(self.recipients)}")
            server.sendmail(
                self.smtp_config.from_address,
                self.recipients,
                msg.as_string(),
            )
            server.quit()
            
            self.logger.success("Alert email sent successfully")
            return True
            
        except EmailError:
            raise
        except Exception as e:
            raise EmailError(
                "Failed to send email",
                details=str(e),
            )
    
    def send_test_email(self) -> bool:
        """
        Send a test email to verify configuration.
        
        Returns:
            True if test email sent successfully.
        """
        hostname = self._get_hostname()
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"[WASM] Test Email - {hostname}"
        msg["From"] = self.smtp_config.from_address
        msg["To"] = ", ".join(self.recipients)
        
        text = f"""
WASM Security Monitor - Test Email
===================================

This is a test email from WASM Security Monitor.

Server: {hostname}
Time: {timestamp}

If you received this email, your notification system is configured correctly.
"""
        
        html = f"""
<!DOCTYPE html>
<html>
<head>
    <style>
        body {{ font-family: Arial, sans-serif; padding: 20px; }}
        .box {{ background: #d4edda; border: 1px solid #c3e6cb; padding: 20px; border-radius: 8px; }}
    </style>
</head>
<body>
    <div class="box">
        <h2>✅ WASM Security Monitor - Test Email</h2>
        <p>This is a test email from WASM Security Monitor.</p>
        <p><strong>Server:</strong> {hostname}<br>
        <strong>Time:</strong> {timestamp}</p>
        <p>If you received this email, your notification system is configured correctly.</p>
    </div>
</body>
</html>
"""
        
        msg.attach(MIMEText(text, "plain"))
        msg.attach(MIMEText(html, "html"))
        
        try:
            server = self._create_connection()
            server.sendmail(
                self.smtp_config.from_address,
                self.recipients,
                msg.as_string(),
            )
            server.quit()
            return True
        except Exception as e:
            raise EmailError(
                "Failed to send test email",
                details=str(e),
            )

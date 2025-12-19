"""
Process monitor for WASM.

Main monitoring system that scans processes, uses AI analysis,
takes mitigation actions, and sends notifications.
"""

import os
import signal
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from wasm.core.config import Config, SYSTEMD_DIR
from wasm.core.exceptions import MonitorError, ServiceError
from wasm.core.logger import Logger
from wasm.core.utils import run_command, run_command_sudo, write_file
from wasm.monitor.ai_analyzer import AIProcessAnalyzer, AnalysisResult, ProcessInfo
from wasm.monitor.email_notifier import EmailNotifier, ThreatReport


@dataclass
class MonitorConfig:
    """Process monitor configuration."""
    
    enabled: bool = False
    scan_interval: int = 30  # Local scan every 30 seconds (was 3600)
    ai_interval: int = 3600  # AI analysis every hour
    cpu_threshold: float = 80.0
    memory_threshold: float = 80.0
    auto_terminate: bool = True
    terminate_malicious_only: bool = True
    use_ai: bool = True
    dry_run: bool = False
    log_file: Optional[Path] = None


class ProcessMonitor:
    """
    Main process monitoring system.
    
    Periodically scans system processes, uses AI analysis to detect
    threats, takes mitigation actions, and sends email notifications.
    """
    
    SERVICE_NAME = "wasm-monitor"
    
    def __init__(
        self,
        config: Optional[MonitorConfig] = None,
        verbose: bool = False,
    ):
        """
        Initialize process monitor.
        
        Args:
            config: Monitor configuration. If None, loads from global config.
            verbose: Enable verbose logging.
        """
        self.verbose = verbose
        self.logger = Logger(verbose=verbose)
        self.global_config = Config()
        
        if config:
            self.config = config
        else:
            self.config = self._load_config()
        
        self.analyzer = AIProcessAnalyzer(verbose=verbose)
        self.notifier = EmailNotifier(verbose=verbose)
        
        self._running = False
        self._terminated_pids: Set[int] = set()
    
    def _load_config(self) -> MonitorConfig:
        """Load monitor configuration from global config."""
        return MonitorConfig(
            enabled=self.global_config.get("monitor.enabled", False),
            scan_interval=self.global_config.get("monitor.scan_interval", 30),
            ai_interval=self.global_config.get("monitor.ai_interval", 3600),
            cpu_threshold=self.global_config.get("monitor.cpu_threshold", 80.0),
            memory_threshold=self.global_config.get("monitor.memory_threshold", 80.0),
            auto_terminate=self.global_config.get("monitor.auto_terminate", True),
            terminate_malicious_only=self.global_config.get(
                "monitor.terminate_malicious_only", True
            ),
            use_ai=self.global_config.get("monitor.use_ai", True),
            dry_run=self.global_config.get("monitor.dry_run", False),
            log_file=Path(self.global_config.get(
                "monitor.log_file", "/var/log/wasm/monitor.log"
            )),
        )
    
    def _get_processes(self) -> List[ProcessInfo]:
        """
        Get list of all running processes.
        
        Returns:
            List of ProcessInfo objects.
        """
        processes = []
        
        try:
            import psutil
            
            for proc in psutil.process_iter([
                'pid', 'name', 'username', 'cpu_percent', 'memory_percent',
                'cmdline', 'create_time', 'ppid', 'status', 'num_threads',
                'cwd'
            ]):
                try:
                    info = proc.info
                    cmdline = info.get('cmdline') or []
                    command = ' '.join(cmdline) if cmdline else info.get('name', '')
                    
                    # Get parent info
                    parent_pid = info.get('ppid')
                    parent_name = None
                    if parent_pid:
                        try:
                            parent = psutil.Process(parent_pid)
                            parent_name = parent.name()
                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                            pass
                    
                    # Get connections separately (not all systems support this)
                    connections = []
                    try:
                        for conn in proc.net_connections():
                            connections.append({
                                'local': f"{conn.laddr.ip}:{conn.laddr.port}" if conn.laddr else "",
                                'remote': f"{conn.raddr.ip}:{conn.raddr.port}" if conn.raddr else "",
                                'status': conn.status,
                            })
                    except (psutil.AccessDenied, psutil.NoSuchProcess, AttributeError):
                        pass
                    
                    # Get open files separately
                    open_files = []
                    try:
                        for f in proc.open_files():
                            open_files.append(f.path)
                    except (psutil.AccessDenied, psutil.NoSuchProcess, AttributeError):
                        pass
                    
                    processes.append(ProcessInfo(
                        pid=info.get('pid', 0),
                        name=info.get('name', ''),
                        user=info.get('username', ''),
                        cpu_percent=info.get('cpu_percent', 0.0) or 0.0,
                        memory_percent=info.get('memory_percent', 0.0) or 0.0,
                        command=command,
                        create_time=info.get('create_time'),
                        parent_pid=parent_pid,
                        parent_name=parent_name,
                        status=info.get('status', 'running'),
                        num_threads=info.get('num_threads', 1),
                        connections=connections,
                        open_files=open_files,
                        cwd=info.get('cwd', ''),
                    ))
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    continue
                    
        except ImportError:
            # Fallback to ps command if psutil is not available
            self.logger.warning("psutil not available, using fallback method")
            processes = self._get_processes_fallback()
        
        return processes
    
    def _get_processes_fallback(self) -> List[ProcessInfo]:
        """
        Get processes using ps command (fallback method).
        
        Returns:
            List of ProcessInfo objects.
        """
        processes = []
        
        result = run_command([
            "ps", "aux", "--no-headers"
        ])
        
        if not result.success:
            return processes
        
        for line in result.stdout.strip().split('\n'):
            if not line:
                continue
            
            parts = line.split(None, 10)
            if len(parts) < 11:
                continue
            
            try:
                processes.append(ProcessInfo(
                    pid=int(parts[1]),
                    name=parts[10].split()[0] if parts[10] else "",
                    user=parts[0],
                    cpu_percent=float(parts[2]),
                    memory_percent=float(parts[3]),
                    command=parts[10],
                ))
            except (ValueError, IndexError):
                continue
        
        return processes
    
    def _terminate_process(
        self,
        pid: int,
        force: bool = False,
    ) -> bool:
        """
        Terminate a process.
        
        Args:
            pid: Process ID to terminate.
            force: Use SIGKILL instead of SIGTERM.
            
        Returns:
            True if terminated successfully.
        """
        if self.config.dry_run:
            self.logger.info(f"[DRY RUN] Would terminate process {pid}")
            return True
        
        sig = signal.SIGKILL if force else signal.SIGTERM
        
        try:
            os.kill(pid, sig)
            self._terminated_pids.add(pid)
            
            # Wait a moment and check if killed
            time.sleep(0.5)
            
            try:
                os.kill(pid, 0)  # Check if still running
                if not force:
                    # Still running, try SIGKILL
                    os.kill(pid, signal.SIGKILL)
                    time.sleep(0.5)
            except OSError:
                pass  # Process is gone
            
            return True
            
        except OSError as e:
            self.logger.error(f"Failed to terminate process {pid}: {e}")
            return False
    
    def _terminate_process_tree(self, pid: int) -> List[int]:
        """
        Terminate a process and all its children.
        
        Args:
            pid: Root process ID to terminate.
            
        Returns:
            List of terminated PIDs.
        """
        terminated = []
        
        try:
            import psutil
            
            try:
                parent = psutil.Process(pid)
                children = parent.children(recursive=True)
                
                # Terminate children first
                for child in reversed(children):
                    if self._terminate_process(child.pid):
                        terminated.append(child.pid)
                
                # Terminate parent
                if self._terminate_process(pid):
                    terminated.append(pid)
                    
            except psutil.NoSuchProcess:
                pass
                
        except ImportError:
            # Fallback: just terminate the process
            if self._terminate_process(pid):
                terminated.append(pid)
        
        return terminated
    
    def _find_malicious_files(
        self,
        process: ProcessInfo,
    ) -> List[Path]:
        """
        Find files associated with a malicious process.
        
        Args:
            process: Process information.
            
        Returns:
            List of suspicious file paths.
        """
        files = []
        
        # Check /proc for executable path
        proc_exe = Path(f"/proc/{process.pid}/exe")
        if proc_exe.exists():
            try:
                exe_path = proc_exe.resolve()
                if exe_path.exists() and str(exe_path).startswith(('/tmp', '/var/tmp', '/dev/shm')):
                    files.append(exe_path)
            except (OSError, PermissionError):
                pass
        
        # Check working directory
        if process.cwd and process.cwd.startswith(('/tmp', '/var/tmp', '/dev/shm')):
            cwd = Path(process.cwd)
            if cwd.exists():
                files.append(cwd)
        
        # Check open files
        for f in process.open_files:
            if f.startswith(('/tmp', '/var/tmp', '/dev/shm')):
                fp = Path(f)
                if fp.exists():
                    files.append(fp)
        
        return files
    
    def _cleanup_malicious_files(
        self,
        files: List[Path],
    ) -> List[str]:
        """
        Remove malicious files.
        
        Args:
            files: List of files to remove.
            
        Returns:
            List of removed file paths.
        """
        removed = []
        
        if self.config.dry_run:
            for f in files:
                self.logger.info(f"[DRY RUN] Would remove: {f}")
                removed.append(str(f))
            return removed
        
        for f in files:
            try:
                if f.is_dir():
                    import shutil
                    shutil.rmtree(f)
                else:
                    f.unlink()
                removed.append(str(f))
                self.logger.debug(f"Removed malicious file: {f}")
            except (OSError, PermissionError) as e:
                self.logger.warning(f"Failed to remove {f}: {e}")
        
        return removed
    
    def _check_for_persistence(
        self,
        process: ProcessInfo,
    ) -> List[Dict[str, str]]:
        """
        Check for persistence mechanisms related to the process.
        
        Args:
            process: Process information.
            
        Returns:
            List of persistence findings.
        """
        findings = []
        
        # Check crontabs
        for user in ['root', process.user]:
            result = run_command_sudo(["crontab", "-u", user, "-l"])
            if result.success:
                for line in result.stdout.split('\n'):
                    if process.name.lower() in line.lower():
                        findings.append({
                            'type': 'crontab',
                            'user': user,
                            'content': line,
                        })
        
        # Check systemd user services
        user_service_dir = Path(f"/home/{process.user}/.config/systemd/user")
        if user_service_dir.exists():
            for service_file in user_service_dir.glob("*.service"):
                try:
                    content = service_file.read_text()
                    if process.name.lower() in content.lower():
                        findings.append({
                            'type': 'systemd_user',
                            'path': str(service_file),
                            'content': content[:200],
                        })
                except (OSError, PermissionError):
                    pass
        
        # Check /etc/rc.local
        rc_local = Path("/etc/rc.local")
        if rc_local.exists():
            try:
                content = rc_local.read_text()
                if process.name.lower() in content.lower():
                    findings.append({
                        'type': 'rc_local',
                        'path': str(rc_local),
                        'content': content[:200],
                    })
            except (OSError, PermissionError):
                pass
        
        return findings
    
    def _mitigate_threat(
        self,
        result: AnalysisResult,
    ) -> ThreatReport:
        """
        Take action to mitigate a detected threat.
        
        Args:
            result: Analysis result for the threat.
            
        Returns:
            ThreatReport with actions taken.
        """
        process = result.process
        actions_taken = []
        
        # Only terminate if:
        # 1. auto_terminate is enabled
        # 2. threat_level is "malicious" (100% confirmed threat)
        # Suspicious processes are NEVER auto-terminated, only reported
        is_malicious = result.threat_level == "malicious"
        should_terminate = (
            self.config.auto_terminate and
            is_malicious and
            not self.config.dry_run
        )
        
        if should_terminate:
            self.logger.warning(f"MALICIOUS PROCESS DETECTED - Taking action on PID {process.pid}")
            
            if result.recommended_action == "terminate_tree":
                terminated = self._terminate_process_tree(process.pid)
                if terminated:
                    actions_taken.append(
                        f"TERMINATED process tree ({len(terminated)} processes)"
                    )
            else:
                if self._terminate_process(process.pid):
                    actions_taken.append("TERMINATED process")
            
            # Find and remove malicious files
            malicious_files = self._find_malicious_files(process)
            if malicious_files:
                removed = self._cleanup_malicious_files(malicious_files)
                if removed:
                    actions_taken.append(f"Removed {len(removed)} malicious files")
            
            # Check for persistence
            persistence = self._check_for_persistence(process)
            if persistence:
                actions_taken.append(
                    f"Found {len(persistence)} persistence mechanisms (manual review needed)"
                )
        elif is_malicious and self.config.dry_run:
            actions_taken.append("DRY RUN - Would terminate (malicious)")
        elif is_malicious and not self.config.auto_terminate:
            actions_taken.append("Auto-terminate disabled - manual action required (MALICIOUS)")
        else:
            # Suspicious but not malicious - only monitor
            actions_taken.append("Monitoring only (suspicious, not confirmed malicious)")
        
        return ThreatReport(
            process_name=process.name,
            pid=process.pid,
            user=process.user,
            cpu_percent=process.cpu_percent,
            memory_percent=process.memory_percent,
            command=process.command,
            threat_level=result.threat_level,
            confidence=result.confidence,
            reason=result.reason,
            parent_pid=process.parent_pid,
            parent_name=process.parent_name,
            action_taken="; ".join(actions_taken) if actions_taken else None,
        )
    
    def scan_once(
        self,
        force_ai: bool = False,
        analyze_all: bool = False,
    ) -> List[ThreatReport]:
        """
        Perform a single scan for threats.
        
        Args:
            force_ai: Force AI analysis even if no suspicious processes found locally.
            analyze_all: Analyze ALL processes with AI (expensive).
            
        Returns:
            List of threat reports.
        """
        self.logger.info("Starting process scan...")
        
        # Get all processes
        processes = self._get_processes()
        self.logger.debug(f"Found {len(processes)} running processes")
        
        # Analyze for threats
        results = self.analyzer.analyze_processes(
            processes,
            use_ai=self.config.use_ai,
            force_ai=force_ai,
            analyze_all=analyze_all,
        )
        
        if not results:
            self.logger.success("No threats detected")
            return []
        
        self.logger.warning(f"Detected {len(results)} potential threats")
        
        # Separate malicious from suspicious
        malicious_results = [r for r in results if r.threat_level == "malicious"]
        suspicious_results = [r for r in results if r.threat_level == "suspicious"]
        
        # Create initial reports for ALL detected threats (warning report)
        initial_reports = [
            ThreatReport(
                process_name=r.process.name,
                pid=r.process.pid,
                user=r.process.user,
                cpu_percent=r.process.cpu_percent,
                memory_percent=r.process.memory_percent,
                command=r.process.command,
                threat_level=r.threat_level,
                confidence=r.confidence,
                reason=r.reason,
                parent_pid=r.process.parent_pid,
                parent_name=r.process.parent_name,
            )
            for r in results
        ]
        
        # STEP 1: Send initial WARNING report (always, for all threats)
        self.logger.info("Sending initial warning report...")
        try:
            self.notifier.send_threat_alert(initial_reports, is_final=False)
        except Exception as e:
            self.logger.error(f"Failed to send initial alert: {e}")
        
        # STEP 2: Only mitigate MALICIOUS processes (not suspicious)
        final_reports = []
        mitigation_performed = False
        
        for result in malicious_results:
            self.logger.info(
                f"Processing MALICIOUS threat: {result.process.name} "
                f"(PID: {result.process.pid})"
            )
            report = self._mitigate_threat(result)
            final_reports.append(report)
            
            if report.action_taken and "TERMINATED" in report.action_taken:
                mitigation_performed = True
            
            if report.action_taken:
                self.logger.info(f"  Action: {report.action_taken}")
        
        # Log suspicious processes (no action taken)
        for result in suspicious_results:
            self.logger.info(
                f"Suspicious process (monitoring only): {result.process.name} "
                f"(PID: {result.process.pid})"
            )
            # Create report for suspicious (no action)
            final_reports.append(ThreatReport(
                process_name=result.process.name,
                pid=result.process.pid,
                user=result.process.user,
                cpu_percent=result.process.cpu_percent,
                memory_percent=result.process.memory_percent,
                command=result.process.command,
                threat_level=result.threat_level,
                confidence=result.confidence,
                reason=result.reason,
                parent_pid=result.process.parent_pid,
                parent_name=result.process.parent_name,
                action_taken="Monitoring only (suspicious, not confirmed malicious)",
            ))
        
        # STEP 3: Send MITIGATION report only if we actually terminated something
        if mitigation_performed:
            self.logger.info("Sending mitigation report...")
            try:
                self.notifier.send_threat_alert(final_reports, is_final=True)
            except Exception as e:
                self.logger.error(f"Failed to send mitigation report: {e}")
        else:
            self.logger.info("No malicious processes terminated - skipping mitigation report")
        
        return final_reports
    
    def run(self) -> None:
        """
        Run the monitor continuously.
        
        Performs local pattern-matching scans every scan_interval seconds (default 30s),
        and full AI analysis every ai_interval seconds (default 3600s = 1 hour).
        """
        self._running = True
        
        scan_interval = self.config.scan_interval
        ai_interval = self.config.ai_interval
        
        self.logger.info(
            f"Starting process monitor (local scan: {scan_interval}s, AI: {ai_interval}s)"
        )
        
        # Track time since last AI analysis
        time_since_ai = 0
        
        while self._running:
            try:
                # Determine if we should use AI this scan
                use_ai_this_scan = time_since_ai >= ai_interval
                
                if use_ai_this_scan:
                    self.logger.info("Running full scan with AI analysis...")
                    self.scan_once(force_ai=False, analyze_all=False)
                    time_since_ai = 0
                else:
                    # Local pattern matching only (quick scan)
                    self.logger.debug("Running quick local scan...")
                    
                    # Get processes and do quick pattern check
                    processes = self._get_processes()
                    
                    # Only do pattern matching, no AI
                    threats = []
                    for process in processes:
                        quick_result = self.analyzer._quick_check(process)
                        if quick_result:
                            threats.append(quick_result)
                    
                    if threats:
                        self.logger.warning(
                            f"Quick scan found {len(threats)} potential threats - "
                            "triggering full AI scan"
                        )
                        # Trigger immediate AI scan
                        self.scan_once(force_ai=True, analyze_all=False)
                        time_since_ai = 0
                    else:
                        self.logger.debug("Quick scan: No threats detected")
                        time_since_ai += scan_interval
                        
            except Exception as e:
                self.logger.error(f"Scan error: {e}")
            
            # Wait for next scan
            for _ in range(scan_interval):
                if not self._running:
                    break
                time.sleep(1)
        
        self.logger.info("Process monitor stopped")
    
    def stop(self) -> None:
        """Stop the monitor."""
        self._running = False
    
    def install_service(self) -> bool:
        """
        Install the monitor as a systemd service.
        
        Returns:
            True if installed successfully.
        """
        import shutil
        import sys
        
        # Find the wasm executable path
        wasm_path = shutil.which("wasm")
        if not wasm_path:
            # Fallback to python -m wasm
            python_path = sys.executable
            wasm_path = f"{python_path} -m wasm"
        
        service_content = f"""# WASM Process Monitor Service
# Generated by WASM

[Unit]
Description=WASM AI-Powered Process Monitor
Documentation=https://github.com/Perkybeet/wasm
After=network.target

[Service]
Type=simple
User=root
Group=root

# Command
ExecStart={wasm_path} monitor run

# Restart policy
Restart=always
RestartSec=30

# Security
NoNewPrivileges=false
PrivateTmp=true

# Logging
StandardOutput=journal
StandardError=journal
SyslogIdentifier={self.SERVICE_NAME}

[Install]
WantedBy=multi-user.target
"""
        
        service_file = SYSTEMD_DIR / f"{self.SERVICE_NAME}.service"
        
        try:
            # Write service file using write_file with sudo
            if not write_file(service_file, service_content, sudo=True, mode=0o644):
                raise MonitorError(
                    "Failed to create service file",
                    details=f"Could not write to {service_file}",
                )
            
            # Reload systemd
            run_command_sudo(["systemctl", "daemon-reload"])
            
            self.logger.success(f"Monitor service installed: {service_file}")
            return True
            
        except Exception as e:
            raise MonitorError(
                "Failed to install monitor service",
                details=str(e),
            )
    
    def enable_service(self) -> bool:
        """
        Enable and start the monitor service.
        
        Returns:
            True if enabled successfully.
        """
        try:
            result = run_command_sudo([
                "systemctl", "enable", "--now", self.SERVICE_NAME
            ])
            
            if result.success:
                self.logger.success("Monitor service enabled and started")
                return True
            else:
                raise MonitorError(
                    "Failed to enable monitor service",
                    details=result.stderr,
                )
        except Exception as e:
            raise MonitorError(
                "Failed to enable monitor service",
                details=str(e),
            )
    
    def disable_service(self) -> bool:
        """
        Disable and stop the monitor service.
        
        Returns:
            True if disabled successfully.
        """
        try:
            result = run_command_sudo([
                "systemctl", "disable", "--now", self.SERVICE_NAME
            ])
            
            if result.success:
                self.logger.success("Monitor service disabled and stopped")
                return True
            else:
                raise MonitorError(
                    "Failed to disable monitor service",
                    details=result.stderr,
                )
        except Exception as e:
            raise MonitorError(
                "Failed to disable monitor service",
                details=str(e),
            )
    
    def start_service(self) -> bool:
        """
        Start the monitor service (without enabling on boot).
        
        Returns:
            True if started successfully.
        """
        import subprocess
        try:
            # Use subprocess directly for reliability
            result = subprocess.run(
                ["systemctl", "start", self.SERVICE_NAME],
                capture_output=True,
                text=True,
                timeout=30
            )
            
            if result.returncode == 0:
                self.logger.success("Monitor service started")
                return True
            else:
                raise MonitorError(
                    "Failed to start monitor service",
                    details=result.stderr or result.stdout,
                )
        except subprocess.TimeoutExpired:
            raise MonitorError(
                "Failed to start monitor service",
                details="Command timed out",
            )
        except Exception as e:
            raise MonitorError(
                "Failed to start monitor service",
                details=str(e),
            )
    
    def stop_service(self) -> bool:
        """
        Stop the monitor service (without disabling on boot).
        
        Returns:
            True if stopped successfully.
        """
        import subprocess
        try:
            # Use subprocess directly for reliability
            result = subprocess.run(
                ["systemctl", "stop", self.SERVICE_NAME],
                capture_output=True,
                text=True,
                timeout=30
            )
            
            if result.returncode == 0:
                self.logger.success("Monitor service stopped")
                return True
            else:
                raise MonitorError(
                    "Failed to stop monitor service",
                    details=result.stderr or result.stdout,
                )
        except subprocess.TimeoutExpired:
            raise MonitorError(
                "Failed to stop monitor service",
                details="Command timed out",
            )
        except Exception as e:
            raise MonitorError(
                "Failed to stop monitor service",
                details=str(e),
            )
    
    def uninstall_service(self) -> bool:
        """
        Uninstall the monitor service.
        
        Returns:
            True if uninstalled successfully.
        """
        try:
            # Stop and disable first
            run_command_sudo([
                "systemctl", "disable", "--now", self.SERVICE_NAME
            ])
            
            # Remove service file
            service_file = SYSTEMD_DIR / f"{self.SERVICE_NAME}.service"
            if service_file.exists():
                run_command_sudo(["rm", str(service_file)])
            
            # Reload systemd
            run_command_sudo(["systemctl", "daemon-reload"])
            
            self.logger.success("Monitor service uninstalled")
            return True
            
        except Exception as e:
            raise MonitorError(
                "Failed to uninstall monitor service",
                details=str(e),
            )
    
    def get_service_status(self) -> Dict[str, Any]:
        """
        Get monitor service status.
        
        Returns:
            Status dictionary.
        """
        service_file = SYSTEMD_DIR / f"{self.SERVICE_NAME}.service"
        
        status = {
            "installed": service_file.exists(),
            "enabled": False,
            "active": False,
            "pid": None,
            "uptime": None,
        }
        
        if not status["installed"]:
            return status
        
        # Check if enabled - use subprocess directly for reliability
        import subprocess
        try:
            result = subprocess.run(
                ["systemctl", "is-enabled", self.SERVICE_NAME],
                capture_output=True,
                text=True,
                timeout=10
            )
            enabled_status = result.stdout.strip().lower()
            status["enabled"] = enabled_status == "enabled"
        except Exception:
            pass
        
        # Check if active
        try:
            result = subprocess.run(
                ["systemctl", "is-active", self.SERVICE_NAME],
                capture_output=True,
                text=True,
                timeout=10
            )
            active_status = result.stdout.strip().lower()
            status["active"] = active_status in ("active", "activating")
        except Exception:
            pass
        
        # Get detailed status for PID and uptime
        try:
            result = subprocess.run(
                ["systemctl", "show", self.SERVICE_NAME,
                 "--property=MainPID,ActiveEnterTimestamp,ActiveState"],
                capture_output=True,
                text=True,
                timeout=10
            )
            
            if result.returncode == 0:
                for line in result.stdout.strip().split('\n'):
                    if '=' not in line:
                        continue
                    key, value = line.split('=', 1)
                    value = value.strip()
                    
                    if key == "MainPID" and value and value != "0":
                        try:
                            status["pid"] = int(value)
                        except ValueError:
                            pass
                    elif key == "ActiveEnterTimestamp" and value:
                        status["uptime"] = value
                    elif key == "ActiveState":
                        # Double-check active state
                        if value.lower() in ("active", "activating"):
                            status["active"] = True
        except Exception:
            pass
        
        # As a final fallback, check if the process is actually running
        if not status["pid"] and status["active"]:
            try:
                import psutil
                for proc in psutil.process_iter(['pid', 'cmdline']):
                    try:
                        cmdline = proc.info.get('cmdline') or []
                        if cmdline and 'wasm' in ' '.join(cmdline) and 'monitor' in ' '.join(cmdline) and 'run' in ' '.join(cmdline):
                            status["pid"] = proc.info['pid']
                            break
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        continue
            except ImportError:
                pass
        
        return status

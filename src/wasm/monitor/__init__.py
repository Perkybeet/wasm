"""
Process monitoring module for WASM.

Provides AI-powered process monitoring to detect and neutralize
suspicious or malicious processes on the system.
"""

from wasm.monitor.ai_analyzer import AIProcessAnalyzer
from wasm.monitor.email_notifier import EmailNotifier
from wasm.monitor.process_monitor import (
    DEFAULT_AI_INTERVAL,
    DEFAULT_CPU_THRESHOLD,
    DEFAULT_MEMORY_THRESHOLD,
    DEFAULT_SCAN_INTERVAL,
    MonitorConfig,
    ProcessMonitor,
)
from wasm.monitor.threat_store import ThreatStore

__all__ = [
    "DEFAULT_AI_INTERVAL",
    "DEFAULT_CPU_THRESHOLD",
    "DEFAULT_MEMORY_THRESHOLD",
    "DEFAULT_SCAN_INTERVAL",
    "AIProcessAnalyzer",
    "EmailNotifier",
    "MonitorConfig",
    "ProcessMonitor",
    "ThreatStore",
]

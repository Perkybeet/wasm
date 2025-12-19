"""
AI-powered process analyzer for WASM.

Uses OpenAI API to analyze running processes and detect suspicious
or malicious behavior based on process characteristics.
"""

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from wasm.core.config import Config
from wasm.core.exceptions import AIAnalysisError
from wasm.core.logger import Logger


@dataclass
class ProcessInfo:
    """Information about a running process."""
    
    pid: int
    name: str
    user: str
    cpu_percent: float
    memory_percent: float
    command: str
    create_time: Optional[float] = None
    parent_pid: Optional[int] = None
    parent_name: Optional[str] = None
    status: str = "running"
    num_threads: int = 1
    connections: List[Dict] = field(default_factory=list)
    open_files: List[str] = field(default_factory=list)
    cwd: str = ""


@dataclass
class AnalysisResult:
    """Result of AI process analysis."""
    
    process: ProcessInfo
    is_threat: bool
    threat_level: str  # "safe", "suspicious", "malicious"
    confidence: float
    reason: str
    recommended_action: str  # "none", "monitor", "terminate", "terminate_tree"
    related_processes: List[int] = field(default_factory=list)


class AIProcessAnalyzer:
    """
    AI-powered process analyzer using OpenAI API.
    
    Analyzes process information to detect cryptocurrency miners,
    backdoors, reverse shells, and other malicious activity.
    """
    
    # Known malicious process patterns (pre-filter)
    KNOWN_MALICIOUS_PATTERNS = [
        r"xmrig",           # Monero miner
        r"minerd",          # Generic miner
        r"cpuminer",        # CPU miner
        r"cgminer",         # GPU miner
        r"bfgminer",        # Mining software
        r"ethminer",        # Ethereum miner
        r"ccminer",         # Cryptocurrency miner
        r"cryptonight",     # Mining algorithm
        r"stratum",         # Mining pool protocol
        r"kworker.*mining", # Hidden miner
        r"\.hidden",        # Hidden processes
        r"kdevtmpfsi",      # Known crypto miner
        r"kinsing",         # Crypto-jacking malware
        r"kerberods",       # Linux backdoor
        r"watchdogs",       # Malware dropper
        r"b64_hidden",      # Base64 obfuscation
    ]
    
    # Suspicious command patterns
    SUSPICIOUS_PATTERNS = [
        r"curl.*\|.*sh",            # Pipe curl to shell
        r"wget.*\|.*sh",            # Pipe wget to shell
        r"bash.*-c.*base64",        # Base64 encoded commands
        r"nc\s+-.*-e",              # Netcat with execution
        r"ncat.*-e",                # Ncat with execution
        r"/dev/tcp/",               # Bash TCP connection
        r"python.*-c.*socket",      # Python reverse shell
        r"perl.*socket",            # Perl reverse shell
        r"ruby.*socket",            # Ruby reverse shell
        r"php.*fsockopen",          # PHP reverse shell
        r"socat.*exec",             # Socat execution
        r"mkfifo.*nc",              # Named pipe with netcat
        r"rm\s+-rf\s+/",            # Dangerous rm command
        r"chmod\s+777",             # Overly permissive chmod
        r"iptables.*DROP",          # Firewall manipulation
        r"crontab.*http",           # Cron download
        r"/tmp/\.",                 # Hidden files in /tmp
        r"\.\/\.",                  # Hidden executable
    ]
    
    # Known safe process patterns (whitelist)
    SAFE_PATTERNS = [
        r"^systemd",
        r"^sshd$",
        r"^nginx",
        r"^apache2",
        r"^postgres",
        r"^mysql",
        r"^node$",
        r"^python3?$",
        r"^php-fpm",
        r"^redis-server",
        r"^mongod",
        r"^dockerd",
        r"^containerd",
        r"^cron$",
        r"^rsyslogd",
        r"^snapd",
        r"^wasm",           # Our own tool
        r"^wasm-",          # Our own services
        r"^next-server",    # Next.js server
        r"^npm$",
        r"^npx$",
        r"^pnpm$",
        r"^yarn$",
        r"^bun$",
        r"^deno$",
        r"^code$",          # VS Code
        r"^code-server",
        r"^cursor",
        r"^electron",
        r"^chrome",
        r"^firefox",
        r"^gnome-",
        r"^kde",
        r"^pipewire",
        r"^pulseaudio",
        r"^Xorg",
        r"^gdm",
        r"^lightdm",
        r"^journald",
        r"^networkmanager",
        r"^dbus",
        r"^polkit",
        r"^udisk",
        r"^gvfs",
        r"^tracker",
        r"^evolution",
        r"^gnome-shell",
        r"^plasmashell",
        r"^sh$",            # Normal shell
        r"^bash$",
        r"^zsh$",
        r"^fish$",
        r"^sudo$",
        r"^su$",
        r"^login$",
        r"^getty",
        r"^agetty",
        r"^pm2",
        r"^supervisor",
        r"^gunicorn",
        r"^uvicorn",
        r"^celery",
        r"^jupyter",
    ]
    
    # Default OpenAI model
    DEFAULT_MODEL = "gpt-4o-mini"
    
    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        verbose: bool = False,
    ):
        """
        Initialize AI process analyzer.
        
        Args:
            api_key: OpenAI API key. If None, loads from config.
            model: OpenAI model to use. If None, uses default.
            verbose: Enable verbose logging.
        """
        self.logger = Logger(verbose=verbose)
        self.config = Config()
        
        self.api_key = api_key or self.config.get("monitor.openai.api_key", "")
        self.model = model or self.config.get("monitor.openai.model", self.DEFAULT_MODEL)
        
        if not self.api_key:
            self.logger.warning("OpenAI API key not configured - will use pattern matching only")
    
    def _quick_check(self, process: ProcessInfo) -> Optional[AnalysisResult]:
        """
        Perform quick pattern-based check before AI analysis.
        
        Args:
            process: Process to check.
            
        Returns:
            AnalysisResult if threat detected, None otherwise.
        """
        command_lower = process.command.lower()
        name_lower = process.name.lower()
        
        # Check against known malicious patterns
        for pattern in self.KNOWN_MALICIOUS_PATTERNS:
            if re.search(pattern, command_lower) or re.search(pattern, name_lower):
                return AnalysisResult(
                    process=process,
                    is_threat=True,
                    threat_level="malicious",
                    confidence=0.95,
                    reason=f"Matches known malicious pattern: {pattern}",
                    recommended_action="terminate_tree",
                )
        
        # Check against suspicious patterns
        for pattern in self.SUSPICIOUS_PATTERNS:
            if re.search(pattern, command_lower):
                return AnalysisResult(
                    process=process,
                    is_threat=True,
                    threat_level="suspicious",
                    confidence=0.80,
                    reason=f"Matches suspicious pattern: {pattern}",
                    recommended_action="terminate",
                )
        
        # Check for high CPU usage with suspicious name
        if process.cpu_percent > 80:
            # Unknown process consuming lots of CPU
            is_safe = any(
                re.search(pattern, process.name.lower())
                for pattern in self.SAFE_PATTERNS
            )
            if not is_safe:
                return AnalysisResult(
                    process=process,
                    is_threat=True,
                    threat_level="suspicious",
                    confidence=0.60,
                    reason=f"High CPU usage ({process.cpu_percent:.1f}%) from unknown process",
                    recommended_action="monitor",
                )
        
        return None
    
    def _analyze_with_ai(self, processes: List[ProcessInfo]) -> List[AnalysisResult]:
        """
        Use OpenAI API to analyze processes.
        
        Args:
            processes: List of processes to analyze.
            
        Returns:
            List of analysis results.
            
        Raises:
            AIAnalysisError: If API call fails.
        """
        if not self.api_key:
            return []
        
        try:
            import httpx
        except ImportError:
            try:
                import requests as httpx
            except ImportError:
                self.logger.warning("HTTP library not available for AI analysis")
                return []
        
        # Prepare process data for analysis
        process_data = []
        for p in processes:
            process_data.append({
                "pid": p.pid,
                "name": p.name,
                "user": p.user,
                "cpu_percent": p.cpu_percent,
                "memory_percent": p.memory_percent,
                "command": p.command[:500],  # Limit command length
                "num_threads": p.num_threads,
                "status": p.status,
                "cwd": p.cwd,
                "has_network_connections": len(p.connections) > 0,
                "connection_count": len(p.connections),
            })
        
        prompt = self._build_analysis_prompt(process_data)
        
        try:
            if hasattr(httpx, 'Client'):
                # Using httpx
                with httpx.Client(timeout=60.0) as client:
                    response = client.post(
                        "https://api.openai.com/v1/chat/completions",
                        headers={
                            "Authorization": f"Bearer {self.api_key}",
                            "Content-Type": "application/json",
                        },
                        json={
                            "model": self.model,
                            "messages": [
                                {
                                    "role": "system",
                                    "content": self._get_system_prompt(),
                                },
                                {
                                    "role": "user",
                                    "content": prompt,
                                },
                            ],
                            "temperature": 0.1,
                            "max_tokens": 2000,
                        },
                    )
            else:
                # Using requests
                response = httpx.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self.model,
                        "messages": [
                            {
                                "role": "system",
                                "content": self._get_system_prompt(),
                            },
                            {
                                "role": "user",
                                "content": prompt,
                            },
                        ],
                        "temperature": 0.1,
                        "max_tokens": 2000,
                    },
                    timeout=60,
                )
            
            if response.status_code != 200:
                raise AIAnalysisError(
                    f"OpenAI API error: {response.status_code}",
                    details=response.text,
                )
            
            result = response.json()
            content = result["choices"][0]["message"]["content"]
            
            return self._parse_ai_response(content, processes)
            
        except AIAnalysisError:
            raise
        except Exception as e:
            raise AIAnalysisError(
                "Failed to analyze processes with AI",
                details=str(e),
            )
    
    def _get_system_prompt(self) -> str:
        """Get the system prompt for AI analysis."""
        return """You are a security expert analyzing Linux processes for REAL security threats.

IMPORTANT: Be EXTREMELY conservative. Only flag processes that are CLEARLY malicious.

What IS malicious (flag these):
- Cryptocurrency miners: xmrig, minerd, cpuminer, kdevtmpfsi, kinsing
- Reverse shells: nc -e, ncat with execution, /dev/tcp connections, python/perl/ruby socket shells
- Known malware: kerberods, watchdogs, tsunami, mirai variants
- Processes running from /tmp with hidden names (starting with .)
- Base64 encoded command execution
- Curl/wget piped directly to shell (curl | sh)

What is NOT malicious (NEVER flag these):
- Normal web servers: nginx, apache, node, next-server, npm, python/gunicorn/uvicorn
- Development tools: code, cursor, electron apps, IDEs
- System services: systemd, journald, dbus, polkitd, networkmanager
- Desktop apps: gnome-shell, chrome, firefox, any GUI application  
- Package managers: npm, yarn, pnpm, pip, apt
- Shells running normal commands: sh, bash, zsh with standard arguments
- Our own monitoring tool: wasm, wasm-monitor
- High memory usage alone is NOT suspicious for web apps

ONLY return processes you are >90% confident are ACTUALLY malicious.
For "suspicious", only use it if there are CONCRETE indicators of malicious behavior.

Response format - JSON array ONLY:
[
  {
    "pid": 1234,
    "threat_level": "malicious",
    "confidence": 0.95,
    "reason": "XMRig cryptocurrency miner - matches known mining pool stratum protocol",
    "recommended_action": "terminate_tree"
  }
]

If NO clear threats found, return empty array: []

Remember: False positives are WORSE than missing a threat. Only flag what you're CERTAIN about."""
    
    def _build_analysis_prompt(self, process_data: List[Dict]) -> str:
        """Build the user prompt for process analysis."""
        return f"""Analyze these {len(process_data)} processes for REAL security threats.

Process list:
```json
{json.dumps(process_data, indent=2)}
```

ONLY flag processes that match KNOWN malware patterns:
- Cryptocurrency miners (xmrig, minerd, cpuminer, stratum connections)
- Active reverse shells (nc -e, /dev/tcp, socket-based shells)
- Known Linux malware families
- Processes with hidden names in /tmp or /dev/shm

DO NOT flag:
- Normal Node.js/Next.js servers (even with high memory)
- Python web frameworks (Django, Flask, FastAPI)
- Standard system processes
- Development tools and IDEs
- The wasm monitoring tool itself

Return ONLY confirmed threats as JSON array. Empty array [] if nothing malicious found."""
    
    def _parse_ai_response(
        self,
        content: str,
        processes: List[ProcessInfo],
    ) -> List[AnalysisResult]:
        """
        Parse AI response into AnalysisResult objects.
        
        Args:
            content: AI response content.
            processes: Original process list.
            
        Returns:
            List of analysis results.
        """
        results = []
        
        # Extract JSON from response
        try:
            # Try to find JSON array in response
            json_match = re.search(r'\[[\s\S]*\]', content)
            if json_match:
                data = json.loads(json_match.group())
            else:
                data = json.loads(content)
        except json.JSONDecodeError:
            self.logger.warning("Failed to parse AI response as JSON")
            return results
        
        # Create lookup for processes
        process_map = {p.pid: p for p in processes}
        
        for item in data:
            pid = item.get("pid")
            if pid and pid in process_map:
                threat_level = item.get("threat_level", "suspicious")
                if threat_level in ["suspicious", "malicious"]:
                    results.append(AnalysisResult(
                        process=process_map[pid],
                        is_threat=True,
                        threat_level=threat_level,
                        confidence=item.get("confidence", 0.7),
                        reason=item.get("reason", "Flagged by AI analysis"),
                        recommended_action=item.get("recommended_action", "monitor"),
                    ))
        
        return results
    
    def analyze_processes(
        self,
        processes: List[ProcessInfo],
        use_ai: bool = True,
        force_ai: bool = False,
        analyze_all: bool = False,
    ) -> List[AnalysisResult]:
        """
        Analyze a list of processes for threats.
        
        Performs quick pattern matching first, then uses AI for
        deeper analysis of remaining processes.
        
        Args:
            processes: List of processes to analyze.
            use_ai: Whether to use AI analysis.
            force_ai: Force AI analysis even if no suspicious processes found.
            analyze_all: Analyze ALL processes with AI (expensive).
            
        Returns:
            List of analysis results for detected threats.
        """
        results = []
        remaining_processes = []
        
        # First pass: Quick pattern matching
        for process in processes:
            quick_result = self._quick_check(process)
            if quick_result:
                results.append(quick_result)
                self.logger.debug(
                    f"Quick check flagged: {process.name} ({process.pid}) - "
                    f"{quick_result.threat_level}"
                )
            else:
                remaining_processes.append(process)
        
        # Second pass: AI analysis for remaining suspicious candidates
        if use_ai and self.api_key and remaining_processes:
            candidates = []
            
            if analyze_all:
                # Analyze ALL processes (expensive)
                candidates = remaining_processes
                self.logger.info(f"Force analyzing ALL {len(candidates)} processes with AI")
            elif force_ai or results:
                # Filter to processes worth analyzing - be very selective
                for p in remaining_processes:
                    # Skip if matches any safe pattern (unless analyze_all)
                    is_safe = any(
                        re.search(pattern, p.name.lower())
                        for pattern in self.SAFE_PATTERNS
                    )
                    if is_safe:
                        continue
                    
                    # Only analyze if there's a concrete reason to be suspicious:
                    # 1. Very high CPU (could be miner)
                    # 2. Running from suspicious locations (/tmp, /var/tmp, /dev/shm)
                    # 3. Has suspicious characters in name (hidden files)
                    # 4. Unknown process with network connections
                    suspicious_location = any(
                        loc in (p.cwd or "")
                        for loc in ["/tmp", "/var/tmp", "/dev/shm"]
                    )
                    suspicious_name = p.name.startswith(".") or "hidden" in p.name.lower()
                    high_cpu_unknown = p.cpu_percent > 50 and not is_safe
                    has_many_connections = len(p.connections) > 20
                    
                    if suspicious_location or suspicious_name or high_cpu_unknown or has_many_connections:
                        candidates.append(p)
                
                # If force_ai is set but no candidates yet, take top CPU/memory processes
                if force_ai and not candidates:
                    # Get top 10 processes by CPU and memory
                    sorted_by_cpu = sorted(
                        remaining_processes,
                        key=lambda p: p.cpu_percent,
                        reverse=True
                    )[:10]
                    sorted_by_mem = sorted(
                        remaining_processes,
                        key=lambda p: p.memory_percent,
                        reverse=True
                    )[:10]
                    
                    # Combine and deduplicate
                    seen = set()
                    for p in sorted_by_cpu + sorted_by_mem:
                        if p.pid not in seen:
                            candidates.append(p)
                            seen.add(p.pid)
                    
                    self.logger.info(f"Force AI enabled: analyzing top {len(candidates)} resource consumers")
            else:
                # Normal mode: Only analyze if there's a concrete reason
                for p in remaining_processes:
                    is_safe = any(
                        re.search(pattern, p.name.lower())
                        for pattern in self.SAFE_PATTERNS
                    )
                    if is_safe:
                        continue
                    
                    suspicious_location = any(
                        loc in (p.cwd or "")
                        for loc in ["/tmp", "/var/tmp", "/dev/shm"]
                    )
                    suspicious_name = p.name.startswith(".") or "hidden" in p.name.lower()
                    high_cpu_unknown = p.cpu_percent > 50 and not is_safe
                    has_many_connections = len(p.connections) > 20
                    
                    if suspicious_location or suspicious_name or high_cpu_unknown or has_many_connections:
                        candidates.append(p)
            
            if candidates:
                self.logger.debug(f"Sending {len(candidates)} processes to AI for analysis")
                try:
                    ai_results = self._analyze_with_ai(candidates)
                    results.extend(ai_results)
                except AIAnalysisError as e:
                    self.logger.warning(f"AI analysis failed: {e}")
        
        return results
        
        return results
    
    def get_analysis_summary(self, results: List[AnalysisResult]) -> Dict[str, Any]:
        """
        Generate a summary of analysis results.
        
        Args:
            results: List of analysis results.
            
        Returns:
            Summary dictionary.
        """
        return {
            "total_threats": len(results),
            "malicious_count": sum(1 for r in results if r.threat_level == "malicious"),
            "suspicious_count": sum(1 for r in results if r.threat_level == "suspicious"),
            "avg_confidence": (
                sum(r.confidence for r in results) / len(results)
                if results else 0
            ),
            "recommended_terminations": sum(
                1 for r in results
                if r.recommended_action in ["terminate", "terminate_tree"]
            ),
            "timestamp": datetime.now().isoformat(),
        }

# WASM Process Monitor

AI-powered security monitoring for Linux servers. Automatically detects and neutralizes suspicious processes like cryptocurrency miners, reverse shells, and other malicious activity.

## Features

- **AI-Powered Analysis**: Uses OpenAI GPT models to analyze process behavior
- **Pattern Matching**: Quick detection of known malware signatures (XMRig, kinsing, etc.)
- **Automatic Mitigation**: Terminates malicious processes and cleans up associated files
- **Email Alerts**: Sends detailed reports when threats are detected
- **Persistence Detection**: Identifies crontab entries and other persistence mechanisms
- **Parent Process Tracking**: Can terminate entire process trees

## Installation

1. Install the monitor dependencies:
   ```bash
   pip install wasm-cli[monitor]
   # or
   pip install psutil httpx
   ```

2. Configure the monitor in `/etc/wasm/config.yaml`:
   ```yaml
   monitor:
     enabled: true
     scan_interval: 3600  # Every hour
     
     # AI Analysis
     use_ai: true
     openai:
       api_key: "your-openai-api-key"
       model: "gpt-4o-mini"
     
     # Email notifications
     smtp:
       host: "smtp.example.com"
       port: 465
       username: "alerts@example.com"
       password: "your-password"
       use_ssl: true
     
     email_recipients:
       - "admin@example.com"
   ```

3. Install and enable the systemd service:
   ```bash
   sudo wasm monitor install
   sudo wasm monitor enable
   ```

## Commands

```bash
# Show monitor status
wasm monitor status

# Run a single scan (manual)
wasm monitor scan
wasm monitor scan --dry-run  # Don't terminate, just report

# Run continuously in foreground
wasm monitor run

# Manage the systemd service
wasm monitor install    # Install service file
wasm monitor enable     # Enable and start
wasm monitor disable    # Disable and stop
wasm monitor uninstall  # Remove service

# Test email configuration
wasm monitor test-email

# Show current configuration
wasm monitor config
```

## Configuration Options

| Option | Default | Description |
|--------|---------|-------------|
| `enabled` | `false` | Enable/disable the monitor |
| `scan_interval` | `3600` | Seconds between scans |
| `cpu_threshold` | `80.0` | CPU % to flag for analysis |
| `memory_threshold` | `80.0` | Memory % to flag for analysis |
| `auto_terminate` | `true` | Automatically kill threats |
| `terminate_malicious_only` | `true` | Only kill "malicious" (not "suspicious") |
| `use_ai` | `true` | Use OpenAI for deep analysis |
| `dry_run` | `false` | Report only, don't terminate |

## How It Works

1. **Process Collection**: Gathers information about all running processes including CPU/memory usage, command line, network connections, and open files.

2. **Quick Pattern Check**: Immediately flags processes matching known malware patterns:
   - Cryptocurrency miners (XMRig, minerd, cpuminer, etc.)
   - Reverse shells (netcat with execution, python socket shells, etc.)
   - Obfuscated commands (base64 encoded, curl|sh patterns)

3. **AI Analysis**: Sends suspicious processes to OpenAI for deeper behavioral analysis.

4. **Threat Response**:
   - Sends initial alert email with detected threats
   - Terminates malicious processes (if auto_terminate is enabled)
   - Cleans up associated files in /tmp, /var/tmp, /dev/shm
   - Sends final report with actions taken

5. **Persistence Check**: Looks for persistence mechanisms:
   - Crontab entries
   - User systemd services
   - /etc/rc.local entries

## Email Notifications

The monitor sends two types of emails:

### Initial Alert
Sent immediately when threats are detected. Contains:
- Server hostname and timestamp
- List of suspicious/malicious processes
- Process details (PID, user, CPU, memory, command)
- Threat confidence level and reason

### Mitigation Report
Sent after taking action. Contains:
- All information from initial alert
- Actions taken for each threat
- Files removed
- Persistence mechanisms found

## Known Malware Patterns

The monitor immediately flags these patterns as malicious:
- `xmrig`, `minerd`, `cpuminer`, `cgminer` - Cryptocurrency miners
- `kdevtmpfsi`, `kinsing` - Known Linux malware
- `kerberods`, `watchdogs` - Backdoors and droppers

Suspicious patterns that trigger AI analysis:
- `curl|sh`, `wget|sh` - Remote code execution
- `nc -e`, `ncat -e` - Reverse shells
- `/dev/tcp/` - Bash network connections
- `python -c "...socket..."` - Script-based shells

## Example: Detecting a Crypto Miner

When a Monero miner is detected, you'll receive an email like:

```
🚨 WASM Security Alert - MALICIOUS PROCESS DETECTED

Server: web-server-01
Time: 2024-01-15 14:30:00
Threats detected: 2

[MALICIOUS] xmrig (PID: 12345)
  User: www-data
  CPU: 98.5% | Memory: 2.3%
  Confidence: 95%
  Reason: Matches known malicious pattern: xmrig
  Command: /tmp/.hidden/xmrig -o pool.minexmr.com:4444 -u ...
```

And after mitigation:

```
🛡️ WASM Security Monitor - Mitigation Report

[NEUTRALIZED] xmrig (PID: 12345)
  Action: Terminated process tree (3 processes); Removed 2 malicious files
```

## Troubleshooting

### Monitor not detecting processes
- Ensure `psutil` is installed: `pip install psutil`
- Check permissions: the monitor needs root access

### AI analysis not working
- Verify your OpenAI API key is correct
- Check internet connectivity
- Try a different model if rate limited

### Emails not sending
- Test with `wasm monitor test-email`
- Verify SMTP credentials
- Check firewall allows outbound port 465/587

### False positives
- Add legitimate processes to safe patterns in configuration
- Use `dry_run: true` initially to review detections
- Set `terminate_malicious_only: true` to be conservative

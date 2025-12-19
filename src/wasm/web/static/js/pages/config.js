/**
 * WASM Web Dashboard - Configuration Page
 */

import { api } from '../core/api.js';
import { showToast, confirm } from '../core/ui.js';

// Track current tab
let currentTab = 'general';

/**
 * Initialize tab switching
 */
export function initTabs() {
    const tabButtons = document.querySelectorAll('[data-config-tab]');
    tabButtons.forEach(btn => {
        btn.addEventListener('click', () => switchTab(btn.dataset.configTab));
    });
}

/**
 * Switch between settings tabs
 */
export function switchTab(tabName) {
    currentTab = tabName;
    
    // Update tab buttons
    document.querySelectorAll('[data-config-tab]').forEach(btn => {
        const isActive = btn.dataset.configTab === tabName;
        btn.classList.toggle('bg-indigo-500/20', isActive);
        btn.classList.toggle('text-indigo-400', isActive);
        btn.classList.toggle('border-b-2', isActive);
        btn.classList.toggle('border-indigo-500', isActive);
        btn.classList.toggle('text-slate-400', !isActive);
    });
    
    // Show/hide tab content
    document.querySelectorAll('.config-tab-content').forEach(content => {
        const contentTabName = content.id.replace('config-tab-', '');
        content.classList.toggle('hidden', contentTabName !== tabName);
    });
}

/**
 * Convert config object to YAML string
 */
function configToYaml(config) {
    const lines = [];
    
    function writeValue(value, indent = 0) {
        const prefix = '  '.repeat(indent);
        if (value === null || value === undefined) {
            return 'null';
        }
        if (typeof value === 'boolean') {
            return value ? 'true' : 'false';
        }
        if (typeof value === 'number') {
            return String(value);
        }
        if (typeof value === 'string') {
            if (value.includes('\n') || value.includes(':') || value.includes('#') || value.startsWith(' ') || value.startsWith('"')) {
                return `"${value.replace(/"/g, '\\"').replace(/\n/g, '\\n')}"`;
            }
            return value || '""';
        }
        if (Array.isArray(value)) {
            if (value.length === 0) return '[]';
            return value.map(v => `\n${prefix}- ${writeValue(v, indent)}`).join('');
        }
        return null; // Object handled separately
    }
    
    function writeObject(obj, indent = 0) {
        const prefix = '  '.repeat(indent);
        for (const [key, value] of Object.entries(obj)) {
            if (value === undefined) continue;
            if (typeof value === 'object' && value !== null && !Array.isArray(value)) {
                lines.push(`${prefix}${key}:`);
                writeObject(value, indent + 1);
            } else {
                const val = writeValue(value, indent + 1);
                if (val !== null && val.startsWith('\n')) {
                    lines.push(`${prefix}${key}:${val}`);
                } else {
                    lines.push(`${prefix}${key}: ${val}`);
                }
            }
        }
    }
    
    writeObject(config);
    return lines.join('\n');
}

/**
 * Parse YAML string to config object (basic parser)
 */
function yamlToConfig(yaml) {
    const lines = yaml.split('\n');
    const result = {};
    const stack = [{ obj: result, indent: -1 }];
    
    for (let line of lines) {
        // Skip comments and empty lines
        if (line.trim().startsWith('#') || !line.trim()) continue;
        
        const indent = line.search(/\S/);
        const content = line.trim();
        
        // Handle array items
        if (content.startsWith('- ')) {
            const value = content.slice(2).trim();
            const parent = stack[stack.length - 1];
            const lastKey = Object.keys(parent.obj).pop();
            if (!Array.isArray(parent.obj[lastKey])) {
                parent.obj[lastKey] = [];
            }
            parent.obj[lastKey].push(parseValue(value));
            continue;
        }
        
        const colonIndex = content.indexOf(':');
        if (colonIndex === -1) continue;
        
        const key = content.slice(0, colonIndex).trim();
        const value = content.slice(colonIndex + 1).trim();
        
        // Pop stack until we find parent
        while (stack.length > 1 && stack[stack.length - 1].indent >= indent) {
            stack.pop();
        }
        
        const parent = stack[stack.length - 1].obj;
        
        if (value === '' || value === null) {
            // Nested object
            parent[key] = {};
            stack.push({ obj: parent[key], indent });
        } else {
            parent[key] = parseValue(value);
        }
    }
    
    return result;
}

function parseValue(value) {
    if (value === 'true') return true;
    if (value === 'false') return false;
    if (value === 'null' || value === '~') return null;
    if (value === '[]') return [];
    if (/^-?\d+$/.test(value)) return parseInt(value, 10);
    if (/^-?\d+\.\d+$/.test(value)) return parseFloat(value);
    if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) {
        return value.slice(1, -1);
    }
    return value;
}

/**
 * Validate YAML syntax
 */
export function validateYaml() {
    const textarea = document.getElementById('config-raw-yaml');
    const statusEl = document.getElementById('yaml-status');
    if (!textarea || !statusEl) return;
    
    const yaml = textarea.value;
    try {
        yamlToConfig(yaml);
        statusEl.textContent = '✓ Valid YAML';
        statusEl.className = 'text-sm text-green-600 dark:text-green-400';
        return true;
    } catch (error) {
        statusEl.textContent = `✗ Invalid: ${error.message}`;
        statusEl.className = 'text-sm text-red-600 dark:text-red-400';
        return false;
    }
}

/**
 * Format YAML in textarea
 */
export function formatYaml() {
    const textarea = document.getElementById('config-raw-yaml');
    if (!textarea) return;
    
    try {
        const config = yamlToConfig(textarea.value);
        textarea.value = configToYaml(config);
        showToast('YAML formatted', 'success');
        validateYaml();
    } catch (error) {
        showToast(`Cannot format invalid YAML: ${error.message}`, 'error');
    }
}

/**
 * Load configuration
 */
export async function load() {
    try {
        const data = await api.getConfig();
        const config = data.config;

        // General
        setValue('config-apps-dir', config.apps_directory);
        setValue('config-webserver', config.webserver);
        setValue('config-service-user', config.service_user);
        setValue('config-service-group', config.service_group);

        // Backup
        const backup = config.backup || {};
        setValue('config-backup-dir', backup.directory);
        setValue('config-max-backups', backup.max_per_app || 10);

        // SSL
        const ssl = config.ssl || {};
        setChecked('config-ssl-enabled', ssl.enabled !== false);
        setValue('config-ssl-provider', ssl.provider || 'certbot');
        setValue('config-ssl-email', ssl.email);

        // Node.js - only package managers are configurable
        const nodejs = config.nodejs || {};
        const pms = nodejs.package_managers || ['npm'];
        setChecked('config-pm-npm', pms.includes('npm'));
        setChecked('config-pm-pnpm', pms.includes('pnpm'));
        setChecked('config-pm-yarn', pms.includes('yarn'));
        setChecked('config-pm-bun', pms.includes('bun'));

        // Monitor - scan settings only (service control is on Monitor page)
        const monitor = config.monitor || {};
        setValue('config-monitor-interval', monitor.scan_interval || 3600);
        setValue('config-monitor-cpu', monitor.cpu_threshold || 80);
        setValue('config-monitor-memory', monitor.memory_threshold || 80);
        setChecked('config-monitor-auto-terminate', monitor.auto_terminate);
        setChecked('config-monitor-terminate-malicious', monitor.terminate_malicious_only !== false);
        setChecked('config-monitor-use-ai', monitor.use_ai);
        setChecked('config-monitor-dry-run', monitor.dry_run);
        setValue('config-monitor-log-file', monitor.log_file);

        // OpenAI
        const openai = monitor.openai || {};
        setValue('config-openai-api-key', openai.api_key);
        setValue('config-openai-model', openai.model || 'gpt-4o-mini');

        // SMTP
        const smtp = monitor.smtp || {};
        setValue('config-smtp-host', smtp.host);
        setValue('config-smtp-port', smtp.port || 465);
        setValue('config-smtp-username', smtp.username);
        setValue('config-smtp-password', smtp.password);
        setValue('config-smtp-from', smtp.from_address);
        setChecked('config-smtp-ssl', smtp.use_ssl !== false);
        setChecked('config-smtp-tls', smtp.use_tls);

        // Email recipients
        const recipients = monitor.email_recipients || [];
        setValue('config-email-recipients', recipients.join(', '));

        // Web - only security settings are configurable here
        const web = config.web || {};
        setValue('config-web-token-exp', web.token_expiration_hours || 24);
        setValue('config-web-rate-limit', web.rate_limit_requests || 100);
        setValue('config-web-rate-window', web.rate_limit_window || 60);
        setValue('config-web-lockout', web.lockout_duration || 300);
        setValue('config-web-max-failed', web.max_failed_attempts || 5);
        setChecked('config-web-rate-enabled', web.rate_limit_enabled !== false);
        
        // IP Whitelist
        const whitelist = web.ip_whitelist || [];
        setValue('config-web-ip-whitelist', whitelist.join(', '));

        // Path
        const pathEl = document.getElementById('config-path');
        if (pathEl) pathEl.textContent = data.path;
        
        // Raw YAML
        const yamlTextarea = document.getElementById('config-raw-yaml');
        if (yamlTextarea) {
            yamlTextarea.value = configToYaml(config);
            validateYaml();
        }
        
        // Initialize tabs
        initTabs();

    } catch (error) {
        showToast(`Failed to load configuration: ${error.message}`, 'error');
    }
}

/**
 * Save configuration
 */
export async function save() {
    let config;
    
    // If on raw YAML tab, parse from textarea
    if (currentTab === 'raw') {
        const yamlTextarea = document.getElementById('config-raw-yaml');
        if (!yamlTextarea) {
            showToast('YAML editor not found', 'error');
            return;
        }
        
        if (!validateYaml()) {
            showToast('Please fix YAML syntax errors before saving', 'error');
            return;
        }
        
        try {
            config = yamlToConfig(yamlTextarea.value);
        } catch (error) {
            showToast(`Invalid YAML: ${error.message}`, 'error');
            return;
        }
    } else {
        // Build config from form fields
        // Gather package managers
        const packageManagers = [];
        if (isChecked('config-pm-npm')) packageManagers.push('npm');
        if (isChecked('config-pm-pnpm')) packageManagers.push('pnpm');
        if (isChecked('config-pm-yarn')) packageManagers.push('yarn');
        if (isChecked('config-pm-bun')) packageManagers.push('bun');

        // Parse email recipients
        const emailRecipientsStr = getValue('config-email-recipients');
        const emailRecipients = emailRecipientsStr 
            ? emailRecipientsStr.split(',').map(e => e.trim()).filter(e => e)
            : [];

        // Parse IP whitelist
        const ipWhitelistStr = getValue('config-web-ip-whitelist');
        const ipWhitelist = ipWhitelistStr
            ? ipWhitelistStr.split(',').map(ip => ip.trim()).filter(ip => ip)
            : [];

        config = {
            apps_directory: getValue('config-apps-dir'),
            webserver: getValue('config-webserver'),
            service_user: getValue('config-service-user'),
            service_group: getValue('config-service-group'),
            backup: {
                directory: getValue('config-backup-dir'),
                max_per_app: parseInt(getValue('config-max-backups')) || 10,
            },
            ssl: {
                enabled: isChecked('config-ssl-enabled'),
                provider: getValue('config-ssl-provider'),
                email: getValue('config-ssl-email'),
            },
            nodejs: {
                package_managers: packageManagers,
            },
            monitor: {
                scan_interval: parseInt(getValue('config-monitor-interval')) || 3600,
                cpu_threshold: parseFloat(getValue('config-monitor-cpu')) || 80,
                memory_threshold: parseFloat(getValue('config-monitor-memory')) || 80,
                auto_terminate: isChecked('config-monitor-auto-terminate'),
                terminate_malicious_only: isChecked('config-monitor-terminate-malicious'),
                use_ai: isChecked('config-monitor-use-ai'),
                dry_run: isChecked('config-monitor-dry-run'),
                log_file: getValue('config-monitor-log-file') || '/var/log/wasm/monitor.log',
                openai: {
                    api_key: getValue('config-openai-api-key'),
                    model: getValue('config-openai-model') || 'gpt-4o-mini',
                },
                smtp: {
                    host: getValue('config-smtp-host'),
                    port: parseInt(getValue('config-smtp-port')) || 465,
                    username: getValue('config-smtp-username'),
                    password: getValue('config-smtp-password'),
                    use_ssl: isChecked('config-smtp-ssl'),
                    use_tls: isChecked('config-smtp-tls'),
                    from_address: getValue('config-smtp-from'),
                },
                email_recipients: emailRecipients,
            },
            web: {
                token_expiration_hours: parseInt(getValue('config-web-token-exp')) || 24,
                rate_limit_enabled: isChecked('config-web-rate-enabled'),
                rate_limit_requests: parseInt(getValue('config-web-rate-limit')) || 100,
                rate_limit_window: parseInt(getValue('config-web-rate-window')) || 60,
                lockout_duration: parseInt(getValue('config-web-lockout')) || 300,
                max_failed_attempts: parseInt(getValue('config-web-max-failed')) || 5,
                ip_whitelist: ipWhitelist,
            },
        };
    }

    try {
        await api.updateConfig(config);
        showToast('Configuration saved successfully', 'success');
        // Reload configuration to ensure UI is in sync
        await load();
    } catch (error) {
        showToast(`Failed to save configuration: ${error.message}`, 'error');
    }
}

/**
 * Reset to defaults
 */
export async function resetToDefaults() {
    if (!await confirm('Are you sure you want to reset configuration to defaults?')) return;

    try {
        const defaults = await api.getConfigDefaults();

        setValue('config-apps-dir', defaults.apps_directory);
        setValue('config-webserver', defaults.webserver);
        setValue('config-service-user', defaults.service_user);
        setValue('config-service-group', defaults.service_group);
        setValue('config-backup-dir', defaults.backup?.directory);
        setValue('config-max-backups', defaults.backup?.max_per_app);
        
        setChecked('config-ssl-enabled', defaults.ssl?.enabled);
        setValue('config-ssl-provider', defaults.ssl?.provider);
        setValue('config-ssl-email', defaults.ssl?.email);
        
        // Package managers
        const pms = defaults.nodejs?.package_managers || ['npm'];
        setChecked('config-pm-npm', pms.includes('npm'));
        setChecked('config-pm-pnpm', pms.includes('pnpm'));
        setChecked('config-pm-yarn', pms.includes('yarn'));
        setChecked('config-pm-bun', pms.includes('bun'));
        
        // Monitor defaults (scan settings only)
        const monitor = defaults.monitor || {};
        setValue('config-monitor-interval', monitor.scan_interval);
        setValue('config-monitor-cpu', monitor.cpu_threshold);
        setValue('config-monitor-memory', monitor.memory_threshold);
        setChecked('config-monitor-auto-terminate', monitor.auto_terminate);
        setChecked('config-monitor-terminate-malicious', monitor.terminate_malicious_only);
        setChecked('config-monitor-use-ai', monitor.use_ai);
        setChecked('config-monitor-dry-run', monitor.dry_run);
        setValue('config-monitor-log-file', monitor.log_file);
        
        // OpenAI defaults
        const openai = monitor.openai || {};
        setValue('config-openai-api-key', openai.api_key);
        setValue('config-openai-model', openai.model);
        
        // SMTP defaults
        const smtp = monitor.smtp || {};
        setValue('config-smtp-host', smtp.host);
        setValue('config-smtp-port', smtp.port);
        setValue('config-smtp-username', smtp.username);
        setValue('config-smtp-password', smtp.password);
        setValue('config-smtp-from', smtp.from_address);
        setChecked('config-smtp-ssl', smtp.use_ssl);
        setChecked('config-smtp-tls', smtp.use_tls);
        setValue('config-email-recipients', (monitor.email_recipients || []).join(', '));
        
        // Web security defaults
        const web = defaults.web || {};
        setValue('config-web-token-exp', web.token_expiration_hours);
        setValue('config-web-rate-limit', web.rate_limit_requests);
        setValue('config-web-rate-window', web.rate_limit_window);
        setValue('config-web-lockout', web.lockout_duration);
        setValue('config-web-max-failed', web.max_failed_attempts);
        setChecked('config-web-rate-enabled', web.rate_limit_enabled);
        setValue('config-web-ip-whitelist', (web.ip_whitelist || []).join(', '));

        showToast('Reset to defaults. Click Save to apply.', 'info');
    } catch (error) {
        showToast(`Failed to get defaults: ${error.message}`, 'error');
    }
}

// Helper functions
function getValue(id) {
    const el = document.getElementById(id);
    return el ? el.value : '';
}

function setValue(id, value) {
    const el = document.getElementById(id);
    if (el) el.value = value ?? '';
}

function isChecked(id) {
    const el = document.getElementById(id);
    return el ? el.checked : false;
}

function setChecked(id, value) {
    const el = document.getElementById(id);
    if (el) el.checked = !!value;
}

export default { load, save, resetToDefaults, switchTab, validateYaml, formatYaml, initTabs };

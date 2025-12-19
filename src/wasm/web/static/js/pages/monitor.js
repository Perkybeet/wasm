/**
 * WASM Web Dashboard - Monitor Page
 */

import { api } from '../core/api.js';
import { showToast, setLoading, setError, confirm } from '../core/ui.js';
import { renderProcessesTable } from '../components/metrics.js';

// Real-time update interval (ms)
const UPDATE_INTERVAL = 5000;
let updateIntervalId = null;

// Store processes data for filtering
let currentProcesses = [];

// Column visibility state (persisted in localStorage)
let columnVisibility = loadColumnVisibility();

/**
 * Load column visibility settings from localStorage
 */
function loadColumnVisibility() {
    const saved = localStorage.getItem('wasm_process_columns');
    if (saved) {
        try {
            return JSON.parse(saved);
        } catch (e) {
            // Ignore parse errors
        }
    }
    return {
        pid: true,
        name: true,
        cpu: true,
        memory: true,
        user: true,
        status: false,
        command: false
    };
}

/**
 * Save column visibility to localStorage
 */
function saveColumnVisibility() {
    localStorage.setItem('wasm_process_columns', JSON.stringify(columnVisibility));
}

/**
 * Load monitor page data
 */
export async function load() {
    // Initialize column checkboxes from saved state
    initColumnCheckboxes();
    
    await Promise.all([
        loadStatus(),
        loadProcesses()
    ]);
    
    // Start real-time updates
    startRealTimeUpdates();
    
    // Close column menu when clicking outside
    document.addEventListener('click', handleClickOutside);
}

/**
 * Initialize column checkboxes based on saved state
 */
function initColumnCheckboxes() {
    Object.entries(columnVisibility).forEach(([col, visible]) => {
        const checkbox = document.getElementById(`col-${col}`);
        if (checkbox) {
            checkbox.checked = visible;
        }
    });
}

/**
 * Handle click outside column menu to close it
 */
function handleClickOutside(e) {
    const menu = document.getElementById('column-menu');
    const btn = document.getElementById('column-toggle-btn');
    if (menu && btn && !menu.contains(e.target) && !btn.contains(e.target)) {
        menu.classList.add('hidden');
    }
}

/**
 * Start real-time updates
 */
function startRealTimeUpdates() {
    stopRealTimeUpdates();
    updateIntervalId = setInterval(() => {
        loadStatus();
        loadProcesses();
    }, UPDATE_INTERVAL);
}

/**
 * Stop real-time updates
 */
export function stopRealTimeUpdates() {
    if (updateIntervalId) {
        clearInterval(updateIntervalId);
        updateIntervalId = null;
    }
}

/**
 * Cleanup when leaving page
 */
export function cleanup() {
    stopRealTimeUpdates();
    document.removeEventListener('click', handleClickOutside);
}

/**
 * Toggle column menu visibility
 */
export function toggleColumnMenu() {
    const menu = document.getElementById('column-menu');
    if (menu) {
        menu.classList.toggle('hidden');
    }
}

/**
 * Update column visibility when checkboxes change
 */
export function updateColumns() {
    const columns = ['pid', 'name', 'cpu', 'memory', 'user', 'status', 'command'];
    columns.forEach(col => {
        const checkbox = document.getElementById(`col-${col}`);
        if (checkbox) {
            columnVisibility[col] = checkbox.checked;
        }
    });
    
    saveColumnVisibility();
    renderProcessesList();
}

/**
 * Filter processes based on search term
 */
export function filterProcesses() {
    renderProcessesList();
}

/**
 * Render processes list with current filters
 */
function renderProcessesList() {
    const container = document.getElementById('processes-list');
    if (!container || currentProcesses.length === 0) return;
    
    const searchInput = document.getElementById('process-search');
    const showInactiveCheckbox = document.getElementById('show-inactive');
    
    const options = {
        showInactive: showInactiveCheckbox?.checked || false,
        searchTerm: searchInput?.value || '',
        columns: columnVisibility
    };
    
    container.innerHTML = renderProcessesTable(currentProcesses, options);
}

/**
 * Load monitor status
 */
async function loadStatus() {
    const container = document.getElementById('monitor-status');
    if (!container) return;

    try {
        const status = await api.getMonitorStatus();

        container.innerHTML = `
            <div class="grid grid-cols-1 md:grid-cols-2 gap-6">
                <!-- Status Info -->
                <div class="grid grid-cols-2 sm:grid-cols-4 gap-4">
                    <div>
                        <span class="text-slate-400 text-sm">Status</span>
                        <div class="flex items-center gap-2 mt-1">
                            <span class="w-2 h-2 rounded-full ${status.active ? 'bg-green-500 animate-pulse' : 'bg-red-500'}"></span>
                            <span class="font-medium ${status.active ? 'text-green-400' : 'text-red-400'}">${status.active ? 'Running' : 'Stopped'}</span>
                        </div>
                    </div>
                    <div>
                        <span class="text-slate-400 text-sm">Installed</span>
                        <div class="mt-1 font-medium">${status.installed ? '<span class="text-green-400">Yes</span>' : '<span class="text-slate-500">No</span>'}</div>
                    </div>
                    <div>
                        <span class="text-slate-400 text-sm">Auto-start</span>
                        <div class="mt-1 font-medium">${status.enabled ? '<span class="text-green-400">Enabled</span>' : '<span class="text-slate-500">Disabled</span>'}</div>
                    </div>
                    <div>
                        <span class="text-slate-400 text-sm">PID</span>
                        <div class="mt-1 font-mono">${status.pid || '<span class="text-slate-500">-</span>'}</div>
                    </div>
                </div>
                
                <!-- Actions -->
                <div class="flex items-center justify-end gap-2 flex-wrap">
                    ${!status.installed ? `
                        <button onclick="window.monitorPage.install()" class="btn-primary px-4 py-2 rounded-lg text-sm flex items-center gap-2">
                            <i class="fas fa-download"></i> Install Service
                        </button>
                    ` : `
                        <!-- Start/Stop buttons -->
                        ${status.active ? `
                            <button onclick="window.monitorPage.stop()" class="px-4 py-2 rounded-lg text-sm bg-orange-500/20 text-orange-400 hover:bg-orange-500/30 flex items-center gap-2">
                                <i class="fas fa-stop"></i> Stop
                            </button>
                        ` : `
                            <button onclick="window.monitorPage.start()" class="px-4 py-2 rounded-lg text-sm bg-green-500/20 text-green-400 hover:bg-green-500/30 flex items-center gap-2">
                                <i class="fas fa-play"></i> Start
                            </button>
                        `}
                        
                        <!-- Enable/Disable auto-start -->
                        ${status.enabled ? `
                            <button onclick="window.monitorPage.disable()" class="px-4 py-2 rounded-lg text-sm bg-slate-700 hover:bg-slate-600 flex items-center gap-2" title="Disable auto-start on boot">
                                <i class="fas fa-toggle-on"></i> Disable Auto-start
                            </button>
                        ` : `
                            <button onclick="window.monitorPage.enable()" class="px-4 py-2 rounded-lg text-sm bg-indigo-500/20 text-indigo-400 hover:bg-indigo-500/30 flex items-center gap-2" title="Enable auto-start on boot">
                                <i class="fas fa-toggle-off"></i> Enable Auto-start
                            </button>
                        `}
                        
                        <!-- Test Email -->
                        <button onclick="window.monitorPage.testEmail()" class="px-3 py-2 rounded-lg text-sm bg-slate-700 hover:bg-slate-600 flex items-center gap-2" title="Send test email notification">
                            <i class="fas fa-envelope"></i>
                            <span class="hidden sm:inline">Test Email</span>
                        </button>
                        
                        <!-- Uninstall -->
                        <button onclick="window.monitorPage.uninstall()" class="px-3 py-2 rounded-lg text-sm bg-slate-700 hover:bg-red-500/20 hover:text-red-400 flex items-center gap-2" title="Uninstall monitor service">
                            <i class="fas fa-trash"></i>
                            <span class="hidden sm:inline">Uninstall</span>
                        </button>
                    `}
                </div>
            </div>
            
            ${status.installed ? `
            <div class="mt-4 pt-4 border-t border-slate-700">
                <p class="text-xs text-slate-500">
                    <i class="fas fa-info-circle mr-1"></i>
                    <strong>Start/Stop:</strong> Control the monitor service now. 
                    <strong>Auto-start:</strong> Whether the service starts automatically on system boot.
                    Configure scan settings in <a href="#config" class="text-indigo-400 hover:underline">Settings → Monitor</a>.
                </p>
            </div>
            ` : `
            <div class="mt-4 pt-4 border-t border-slate-700">
                <p class="text-xs text-slate-500">
                    <i class="fas fa-info-circle mr-1"></i>
                    The Process Monitor uses AI to detect and neutralize malicious processes like crypto miners and reverse shells.
                    Click <strong>Install Service</strong> to set up the systemd service.
                </p>
            </div>
            `}
        `;
    } catch (error) {
        container.innerHTML = `
            <div class="text-slate-400 text-center py-4">
                <i class="fas fa-exclamation-triangle text-yellow-500 mr-2"></i>
                Monitor module not available. Install with: <code class="bg-slate-800 px-2 py-1 rounded">pip install psutil</code>
            </div>
        `;
    }
}

/**
 * Load processes list
 */
export async function loadProcesses() {
    const container = document.getElementById('processes-list');
    if (!container) return;

    try {
        // Get current sort and limit values
        const sortSelect = document.getElementById('process-sort');
        const limitSelect = document.getElementById('process-limit');
        
        const sortBy = sortSelect?.value || 'cpu';
        const limit = parseInt(limitSelect?.value || '100', 10);
        
        const data = await api.getProcesses(limit, sortBy);
        currentProcesses = data.processes || [];
        
        renderProcessesList();
    } catch (error) {
        setError('#processes-list', `Failed to load processes: ${error.message}`);
    }
}

/**
 * Run a scan with options
 */
export async function runScan(forceAi = false, analyzeAll = false) {
    try {
        let msg = 'Running scan...';
        if (analyzeAll) {
            msg = 'Running full AI scan (this may take a while)...';
        } else if (forceAi) {
            msg = 'Running scan with forced AI analysis...';
        }
        showToast(msg, 'info');
        
        const result = await api.runScan(true, forceAi, analyzeAll);
        showToast(`Scan complete: ${result.suspicious} suspicious processes found`, 'success');
        loadStatus();
        loadProcesses();
        
        // Show results if any threats found
        if (result.suspicious > 0) {
            showScanResults(result);
        }
    } catch (error) {
        showToast(`Scan failed: ${error.message}`, 'error');
    }
}

/**
 * Show scan results in a modal or alert
 */
function showScanResults(result) {
    // For now, just log to console - could add a modal later
    console.log('Scan results:', result);
}

/**
 * Kill a process
 */
export async function killProcess(pid) {
    if (!await confirm(`Are you sure you want to kill process ${pid}?`)) {
        return;
    }

    try {
        await api.killProcess(pid, 15);
        showToast(`Process ${pid} terminated`, 'success');
        loadProcesses();
    } catch (error) {
        showToast(`Failed: ${error.message}`, 'error');
    }
}

/**
 * Start monitor service
 */
export async function start() {
    try {
        showToast('Starting monitor...', 'info');
        await api.startMonitor();
        showToast('Monitor started', 'success');
        // Wait a moment for the service to fully start before refreshing status
        setTimeout(() => loadStatus(), 1000);
    } catch (error) {
        showToast(`Failed to start monitor: ${error.message}`, 'error');
    }
}

/**
 * Stop monitor service
 */
export async function stop() {
    try {
        showToast('Stopping monitor...', 'info');
        await api.stopMonitor();
        showToast('Monitor stopped', 'success');
        // Wait a moment for the service to fully stop before refreshing status
        setTimeout(() => loadStatus(), 1000);
    } catch (error) {
        showToast(`Failed to stop monitor: ${error.message}`, 'error');
    }
}

/**
 * Enable monitor service (auto-start on boot)
 */
export async function enable() {
    try {
        showToast('Enabling auto-start...', 'info');
        await api.enableMonitor();
        showToast('Monitor will start automatically on boot', 'success');
        loadStatus();
    } catch (error) {
        showToast(`Failed to enable monitor: ${error.message}`, 'error');
    }
}

/**
 * Disable monitor service (no auto-start on boot)
 */
export async function disable() {
    try {
        showToast('Disabling auto-start...', 'info');
        await api.disableMonitor();
        showToast('Monitor auto-start disabled', 'success');
        loadStatus();
    } catch (error) {
        showToast(`Failed to disable monitor: ${error.message}`, 'error');
    }
}

/**
 * Install monitor service
 */
export async function install() {
    try {
        showToast('Installing monitor...', 'info');
        await api.installMonitor();
        showToast('Monitor installed', 'success');
        loadStatus();
    } catch (error) {
        showToast(`Failed to install monitor: ${error.message}`, 'error');
    }
}

/**
 * Uninstall monitor service
 */
export async function uninstall() {
    if (!await confirm('Are you sure you want to uninstall the monitor service?')) {
        return;
    }

    try {
        showToast('Uninstalling monitor...', 'info');
        await api.uninstallMonitor();
        showToast('Monitor uninstalled', 'success');
        loadStatus();
    } catch (error) {
        showToast(`Failed to uninstall monitor: ${error.message}`, 'error');
    }
}

/**
 * Test email notification
 */
export async function testEmail() {
    try {
        showToast('Sending test email...', 'info');
        await api.testEmail();
        showToast('Test email sent', 'success');
    } catch (error) {
        showToast(`Failed to send test email: ${error.message}`, 'error');
    }
}

export default { 
    load, 
    cleanup, 
    runScan, 
    killProcess, 
    start, 
    stop, 
    enable, 
    disable, 
    install, 
    uninstall, 
    testEmail,
    loadProcesses,
    filterProcesses,
    toggleColumnMenu,
    updateColumns
};

/**
 * WASM Web Dashboard - Metrics Components
 */

import { escapeHtml } from '../core/ui.js';

/**
 * Update system metrics display
 */
export function updateMetrics(data) {
    // CPU
    updateMetric('cpu', data.cpu.percent);
    
    // Memory
    updateMetric('memory', data.memory.percent);
    
    // Disk
    updateMetric('disk', data.disk.percent);
    
    // Load
    const loadEl = document.getElementById('load-value');
    const loadDetail = document.getElementById('load-detail');
    if (loadEl) loadEl.textContent = data.load['1min'].toFixed(2);
    if (loadDetail) {
        loadDetail.textContent = `5m: ${data.load['5min'].toFixed(2)} | 15m: ${data.load['15min'].toFixed(2)}`;
    }
}

/**
 * Update a single metric
 */
function updateMetric(name, percent) {
    const valueEl = document.getElementById(`${name}-value`);
    const barEl = document.getElementById(`${name}-bar`);
    
    if (valueEl) valueEl.textContent = `${percent.toFixed(1)}%`;
    if (barEl) barEl.style.width = `${percent}%`;
}

/**
 * Render metric card HTML
 */
export function renderMetricCard(id, label, icon, iconColor, barColor) {
    return `
        <div class="metric-card card p-6">
            <div class="flex items-center justify-between mb-4">
                <span class="text-slate-400">${label}</span>
                <i class="fas ${icon} ${iconColor}"></i>
            </div>
            <div class="text-3xl font-bold" id="${id}-value">--</div>
            <div class="mt-2 h-2 bg-slate-700 rounded-full overflow-hidden">
                <div id="${id}-bar" class="progress-bar h-full ${barColor} rounded-full" style="width: 0%"></div>
            </div>
        </div>
    `;
}

/**
 * Render system info
 */
export function renderSystemInfo(info) {
    return `
        <div class="flex justify-between py-2 border-b border-slate-700">
            <span class="text-slate-400">Hostname</span>
            <span>${info.hostname}</span>
        </div>
        <div class="flex justify-between py-2 border-b border-slate-700">
            <span class="text-slate-400">OS</span>
            <span>${info.os}</span>
        </div>
        <div class="flex justify-between py-2 border-b border-slate-700">
            <span class="text-slate-400">Kernel</span>
            <span>${info.kernel}</span>
        </div>
        <div class="flex justify-between py-2 border-b border-slate-700">
            <span class="text-slate-400">Uptime</span>
            <span>${info.uptime}</span>
        </div>
        <div class="flex justify-between py-2">
            <span class="text-slate-400">CPU Cores</span>
            <span>${info.cpu.cores}</span>
        </div>
    `;
}

/**
 * Render processes table with dynamic columns support
 * @param {Array} processes - List of processes
 * @param {Object} options - Display options
 * @param {boolean} options.showInactive - Show processes with 0% CPU and memory
 * @param {string} options.searchTerm - Filter by name/command
 * @param {Object} options.columns - Which columns to show
 */
export function renderProcessesTable(processes, options = {}) {
    const {
        showInactive = false,
        searchTerm = '',
        columns = {
            pid: true,
            name: true,
            cpu: true,
            memory: true,
            user: true,
            status: false,
            command: false
        }
    } = options;
    
    // Filter processes
    let filteredProcesses = processes;
    
    // Filter inactive (unless showInactive is true)
    if (!showInactive) {
        filteredProcesses = filteredProcesses.filter(
            proc => proc.cpu_percent > 0.1 || proc.memory_percent > 0.1
        );
    }
    
    // Filter by search term
    if (searchTerm) {
        const term = searchTerm.toLowerCase();
        filteredProcesses = filteredProcesses.filter(proc => 
            (proc.name && proc.name.toLowerCase().includes(term)) ||
            (proc.command && proc.command.toLowerCase().includes(term)) ||
            (proc.user && proc.user.toLowerCase().includes(term)) ||
            (proc.pid && proc.pid.toString().includes(term))
        );
    }
    
    if (filteredProcesses.length === 0) {
        const message = searchTerm 
            ? `No processes matching "${escapeHtml(searchTerm)}"`
            : 'No active processes consuming resources';
        return `
            <div class="text-center text-slate-400 py-8">
                <i class="fas ${searchTerm ? 'fa-search' : 'fa-check-circle'} ${searchTerm ? 'text-slate-500' : 'text-green-500'} text-2xl mb-2"></i>
                <p>${message}</p>
            </div>
        `;
    }
    
    // Build table headers based on visible columns
    const headers = [];
    if (columns.pid) headers.push({ key: 'pid', label: 'PID', class: 'w-20' });
    if (columns.name) headers.push({ key: 'name', label: 'Name', class: '' });
    if (columns.cpu) headers.push({ key: 'cpu', label: 'CPU %', class: 'w-24 text-right' });
    if (columns.memory) headers.push({ key: 'memory', label: 'Memory %', class: 'w-24 text-right' });
    if (columns.user) headers.push({ key: 'user', label: 'User', class: 'w-28' });
    if (columns.status) headers.push({ key: 'status', label: 'Status', class: 'w-24' });
    if (columns.command) headers.push({ key: 'command', label: 'Command', class: 'max-w-xs' });
    headers.push({ key: 'actions', label: '', class: 'w-16 text-right' });
    
    return `
        <table class="w-full">
            <thead>
                <tr class="text-left text-slate-400 text-sm border-b border-slate-700">
                    ${headers.map(h => `<th class="pb-3 ${h.class}">${h.label}</th>`).join('')}
                </tr>
            </thead>
            <tbody>
                ${filteredProcesses.map(proc => renderProcessRow(proc, columns)).join('')}
            </tbody>
        </table>
        <div class="flex items-center justify-between mt-4 pt-3 border-t border-slate-700">
            <div class="text-xs text-slate-500">
                Showing ${filteredProcesses.length} of ${processes.length} processes
                ${!showInactive ? ' (hiding inactive)' : ''}
            </div>
            <div class="flex items-center gap-4 text-xs">
                <span class="flex items-center gap-1">
                    <span class="w-2 h-2 rounded-full bg-green-500"></span>
                    <span class="text-slate-400">Normal</span>
                </span>
                <span class="flex items-center gap-1">
                    <span class="w-2 h-2 rounded-full bg-yellow-500"></span>
                    <span class="text-slate-400">&gt;50% usage</span>
                </span>
                <span class="flex items-center gap-1">
                    <span class="w-2 h-2 rounded-full bg-red-500"></span>
                    <span class="text-slate-400">&gt;80% usage</span>
                </span>
            </div>
        </div>
    `;
}

/**
 * Render a single process row
 */
function renderProcessRow(proc, columns) {
    const cpuPercent = proc.cpu_percent || 0;
    const memPercent = proc.memory_percent || 0;
    
    // Determine color classes based on usage
    const getCpuClass = (val) => {
        if (val > 80) return 'text-red-400 font-semibold';
        if (val > 50) return 'text-yellow-400';
        return '';
    };
    
    const getMemClass = (val) => {
        if (val > 80) return 'text-red-400 font-semibold';
        if (val > 50) return 'text-yellow-400';
        return '';
    };
    
    const getStatusClass = (status) => {
        switch (status) {
            case 'running': return 'text-green-400';
            case 'sleeping': return 'text-blue-400';
            case 'stopped': return 'text-yellow-400';
            case 'zombie': return 'text-red-400';
            default: return 'text-slate-400';
        }
    };
    
    const cells = [];
    
    if (columns.pid) {
        cells.push(`<td class="py-2 text-slate-400 font-mono text-sm">${proc.pid}</td>`);
    }
    if (columns.name) {
        cells.push(`<td class="py-2 font-medium">${escapeHtml(proc.name)}</td>`);
    }
    if (columns.cpu) {
        cells.push(`<td class="py-2 text-right tabular-nums ${getCpuClass(cpuPercent)}">${cpuPercent.toFixed(1)}%</td>`);
    }
    if (columns.memory) {
        cells.push(`<td class="py-2 text-right tabular-nums ${getMemClass(memPercent)}">${memPercent.toFixed(1)}%</td>`);
    }
    if (columns.user) {
        cells.push(`<td class="py-2 text-slate-400 text-sm">${escapeHtml(proc.user || 'unknown')}</td>`);
    }
    if (columns.status) {
        cells.push(`<td class="py-2 text-sm ${getStatusClass(proc.status)}">${escapeHtml(proc.status || 'unknown')}</td>`);
    }
    if (columns.command) {
        const cmd = proc.command || '';
        const truncatedCmd = cmd.length > 60 ? cmd.substring(0, 60) + '...' : cmd;
        cells.push(`<td class="py-2 text-slate-400 text-xs font-mono truncate max-w-xs" title="${escapeHtml(cmd)}">${escapeHtml(truncatedCmd)}</td>`);
    }
    
    // Actions column
    cells.push(`
        <td class="py-2 text-right">
            <button onclick="window.monitorPage.killProcess(${proc.pid})" 
                    class="text-red-400 hover:text-red-300 text-sm px-2 py-1 rounded hover:bg-red-500/10"
                    title="Kill process ${proc.pid}">
                <i class="fas fa-times"></i>
            </button>
        </td>
    `);
    
    return `<tr class="border-t border-slate-700/50 hover:bg-slate-800/50">${cells.join('')}</tr>`;
}

export default {
    updateMetrics,
    renderMetricCard,
    renderSystemInfo,
    renderProcessesTable
};

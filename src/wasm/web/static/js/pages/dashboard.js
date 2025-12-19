/**
 * WASM Web Dashboard - Dashboard Page
 */

import { api } from '../core/api.js';
import { setContent, setLoading, showToast, escapeHtml, formatTime } from '../core/ui.js';
import { renderSystemInfo } from '../components/metrics.js';
import { STATUS_COLORS, STATUS_ICONS } from '../components/jobs.js';

// Real-time update interval (ms)
const UPDATE_INTERVAL = 5000;
let updateIntervalId = null;

/**
 * Load dashboard data
 */
export async function load() {
    // Initial load
    await refreshData();
    
    // Start real-time updates
    startRealTimeUpdates();
}

/**
 * Refresh all dashboard data
 */
async function refreshData() {
    await Promise.all([
        loadAppsOverview(),
        loadSystemInfo(),
        loadJobsOverview(),
        loadProcessesOverview()
    ]);
}

/**
 * Start real-time updates
 */
function startRealTimeUpdates() {
    // Clear any existing interval
    stopRealTimeUpdates();
    
    // Set up periodic refresh
    updateIntervalId = setInterval(() => {
        refreshData();
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
}

/**
 * Load apps overview for dashboard
 */
async function loadAppsOverview() {
    const container = document.getElementById('apps-overview');
    if (!container) return;

    try {
        const data = await api.getApps();

        if (data.apps.length === 0) {
            container.innerHTML = `
                <div class="text-slate-400 text-center py-4">
                    No applications deployed
                </div>
            `;
            return;
        }

        container.innerHTML = data.apps.slice(0, 5).map(app => `
            <div class="flex items-center justify-between p-3 bg-slate-800/50 rounded-lg">
                <div class="flex items-center gap-3">
                    <span class="w-2 h-2 rounded-full ${app.active ? 'bg-green-500' : 'bg-red-500'}"></span>
                    <span class="font-medium">${escapeHtml(app.domain)}</span>
                </div>
                <span class="text-sm text-slate-400">${app.app_type || 'unknown'}</span>
            </div>
        `).join('');
    } catch (error) {
        console.error('Failed to load apps:', error);
        container.innerHTML = '<div class="text-red-400 text-center py-4">Failed to load apps</div>';
    }
}

/**
 * Load system info
 */
async function loadSystemInfo() {
    const container = document.getElementById('system-info');
    if (!container) return;

    try {
        const sysInfo = await api.getSystemInfo();
        container.innerHTML = renderSystemInfo(sysInfo);
    } catch (error) {
        console.error('Failed to load system info:', error);
        container.innerHTML = '<div class="text-red-400 text-center py-4">Failed to load system info</div>';
    }
}

/**
 * Load recent jobs overview
 */
async function loadJobsOverview() {
    const container = document.getElementById('jobs-overview');
    if (!container) return;

    try {
        const data = await api.getJobs(5);

        if (data.jobs.length === 0) {
            container.innerHTML = `
                <div class="text-slate-400 text-center py-4">
                    <i class="fas fa-check-circle text-green-500 mb-2"></i>
                    <p>No recent jobs</p>
                </div>
            `;
            return;
        }

        container.innerHTML = data.jobs.map(job => {
            const statusColor = STATUS_COLORS[job.status] || STATUS_COLORS.pending;
            const statusIcon = STATUS_ICONS[job.status] || STATUS_ICONS.pending;
            const isActive = job.status === 'running' || job.status === 'pending';

            return `
                <div class="flex items-center justify-between p-3 bg-slate-800/50 rounded-lg ${isActive ? 'border border-indigo-500/30' : ''}"
                     onclick="window.jobsPage.showDetails('${job.id}')" style="cursor: pointer;">
                    <div class="flex items-center gap-3 flex-1 min-w-0">
                        <span class="badge ${statusColor} text-xs">
                            <i class="fas ${statusIcon} ${isActive ? '' : 'mr-1'}"></i>
                            ${isActive ? '' : job.status}
                        </span>
                        <span class="truncate">${escapeHtml(job.name)}</span>
                    </div>
                    <div class="flex items-center gap-2 text-sm text-slate-400">
                        ${isActive ? `<span class="text-indigo-400">${job.progress}%</span>` : ''}
                        <span>${formatTime(job.created_at)}</span>
                    </div>
                </div>
            `;
        }).join('');
    } catch (error) {
        console.error('Failed to load jobs:', error);
        container.innerHTML = '<div class="text-red-400 text-center py-4">Failed to load jobs</div>';
    }
}

/**
 * Load top processes overview
 */
async function loadProcessesOverview() {
    const container = document.getElementById('processes-overview');
    if (!container) return;

    try {
        const data = await api.getProcesses(5, 'cpu');
        
        // Filter out inactive processes
        const activeProcesses = data.processes.filter(p => p.cpu_percent > 0.1 || p.memory_percent > 0.1);

        if (activeProcesses.length === 0) {
            container.innerHTML = `
                <div class="text-slate-400 text-center py-4">
                    <i class="fas fa-check-circle text-green-500 mb-2"></i>
                    <p>No active processes</p>
                </div>
            `;
            return;
        }

        container.innerHTML = activeProcesses.slice(0, 5).map(proc => `
            <div class="flex items-center justify-between p-2 bg-slate-800/50 rounded text-sm">
                <div class="flex items-center gap-2 flex-1 min-w-0">
                    <span class="text-slate-500 w-12">${proc.pid}</span>
                    <span class="truncate">${escapeHtml(proc.name)}</span>
                </div>
                <div class="flex items-center gap-4 text-xs">
                    <span class="${proc.cpu_percent > 50 ? 'text-red-400' : 'text-slate-400'}">
                        <i class="fas fa-microchip mr-1"></i>${proc.cpu_percent.toFixed(1)}%
                    </span>
                    <span class="${proc.memory_percent > 50 ? 'text-red-400' : 'text-slate-400'}">
                        <i class="fas fa-memory mr-1"></i>${proc.memory_percent.toFixed(1)}%
                    </span>
                </div>
            </div>
        `).join('');
    } catch (error) {
        console.error('Failed to load processes:', error);
        container.innerHTML = '<div class="text-red-400 text-center py-4">Failed to load processes</div>';
    }
}

export default { load, cleanup, stopRealTimeUpdates };

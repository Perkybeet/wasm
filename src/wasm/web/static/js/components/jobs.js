/**
 * WASM Web Dashboard - Job Components
 */

import { escapeHtml, formatTime } from '../core/ui.js';

export const STATUS_COLORS = {
    pending: 'bg-yellow-500/20 text-yellow-400',
    running: 'bg-blue-500/20 text-blue-400',
    completed: 'bg-green-500/20 text-green-400',
    failed: 'bg-red-500/20 text-red-400',
    cancelled: 'bg-slate-500/20 text-slate-400',
};

export const STATUS_ICONS = {
    pending: 'fa-clock',
    running: 'fa-spinner fa-spin',
    completed: 'fa-check',
    failed: 'fa-times',
    cancelled: 'fa-ban',
};

const LOG_COLORS = {
    info: 'text-slate-300',
    success: 'text-green-400',
    warning: 'text-yellow-400',
    error: 'text-red-400',
};

/**
 * Render a job card
 */
export function renderJobCard(job) {
    const isActive = job.status === 'running' || job.status === 'pending';
    const statusColor = STATUS_COLORS[job.status] || STATUS_COLORS.pending;
    const statusIcon = STATUS_ICONS[job.status] || STATUS_ICONS.pending;

    return `
        <div class="card p-4 ${isActive ? 'border-indigo-500/30' : ''}" data-job-id="${job.id}">
            <div class="flex items-center justify-between mb-3">
                <div class="flex items-center gap-3">
                    <span class="badge ${statusColor}">
                        <i class="fas ${statusIcon} mr-1"></i>
                        ${job.status}
                    </span>
                    <span class="font-medium">${escapeHtml(job.name)}</span>
                </div>
                <span class="text-sm text-slate-400">${formatTime(job.created_at)}</span>
            </div>
            
            <p class="text-sm text-slate-400 mb-3">${escapeHtml(job.description)}</p>
            
            ${isActive ? `
                <div class="mb-3">
                    <div class="flex justify-between text-sm mb-1">
                        <span class="text-slate-400">${escapeHtml(job.current_step || 'Starting...')}</span>
                        <span>${job.progress}%</span>
                    </div>
                    <div class="h-2 bg-slate-700 rounded-full overflow-hidden">
                        <div class="h-full bg-indigo-500 rounded-full progress-bar" style="width: ${job.progress}%"></div>
                    </div>
                </div>
            ` : ''}
            
            <div class="flex items-center justify-between">
                <div class="text-xs text-slate-500">
                    ID: ${escapeHtml(job.id)} | Type: ${escapeHtml(job.type)}
                </div>
                <div class="flex gap-2">
                    <button onclick="window.jobsPage.showDetails('${job.id}')" 
                            class="px-3 py-1 rounded bg-slate-700 hover:bg-slate-600 text-sm">
                        <i class="fas fa-eye mr-1"></i>Details
                    </button>
                    ${isActive ? `
                        <button onclick="window.jobsPage.cancel('${job.id}')" 
                                class="px-3 py-1 rounded bg-red-500/20 text-red-400 hover:bg-red-500/30 text-sm">
                            <i class="fas fa-stop mr-1"></i>Cancel
                        </button>
                    ` : ''}
                </div>
            </div>
        </div>
    `;
}

/**
 * Render job modal content
 */
export function renderJobModalContent(job) {
    const statusColor = STATUS_COLORS[job.status] || STATUS_COLORS.pending;
    const isActive = job.status === 'running' || job.status === 'pending';

    return {
        title: job.name,
        status: `<span class="badge ${statusColor}">${job.status.charAt(0).toUpperCase() + job.status.slice(1)}</span>`,
        progress: `${job.progress}%`,
        progressWidth: `${job.progress}%`,
        step: job.current_step || '--',
        logs: renderJobLogs(job.logs),
        created: `Created: ${formatTime(job.created_at)}`,
        showCancel: isActive
    };
}

/**
 * Render job logs
 */
export function renderJobLogs(logs) {
    if (!logs || logs.length === 0) {
        return '<div class="text-slate-500">Waiting for output...</div>';
    }

    return logs.map(log => {
        const color = LOG_COLORS[log.level] || LOG_COLORS.info;
        return `<div class="log-line ${color}">${escapeHtml(log.message)}</div>`;
    }).join('');
}

/**
 * Update active jobs badge
 */
export function updateActiveJobsBadge(count) {
    const badge = document.getElementById('active-jobs-badge');
    const summary = document.getElementById('active-jobs-summary');
    const text = document.getElementById('active-jobs-text');

    if (count > 0) {
        badge?.classList.remove('hidden');
        if (badge) badge.textContent = count;
        summary?.classList.remove('hidden');
        if (text) text.textContent = `${count} job${count > 1 ? 's' : ''} running`;
    } else {
        badge?.classList.add('hidden');
        summary?.classList.add('hidden');
    }
}

export default {
    renderJobCard,
    renderJobModalContent,
    renderJobLogs,
    updateActiveJobsBadge,
    STATUS_COLORS,
    STATUS_ICONS
};

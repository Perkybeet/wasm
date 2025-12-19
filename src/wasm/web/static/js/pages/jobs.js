/**
 * WASM Web Dashboard - Jobs Page
 */

import { api } from '../core/api.js';
import { ws } from '../core/websocket.js';
import { showToast, showModal, hideModal, setLoading, setEmpty, setError, confirm } from '../core/ui.js';
import { renderJobCard, renderJobModalContent, renderJobLogs, updateActiveJobsBadge } from '../components/jobs.js';

let jobsWs = null;
let currentJobWs = null;
let currentJobId = null;
let loadDebounceTimer = null;

/**
 * Load jobs list
 */
export async function load() {
    const container = document.getElementById('jobs-list');
    if (!container) return;

    const filter = document.getElementById('jobs-filter')?.value || '';

    setLoading('#jobs-list');

    try {
        const data = await api.getJobs(50, filter || null);

        // Update active jobs badge
        updateActiveJobsBadge(data.active);

        if (data.jobs.length === 0) {
            container.innerHTML = `
                <div class="card p-8 text-center">
                    <i class="fas fa-tasks text-4xl text-slate-600 mb-4"></i>
                    <p class="text-slate-400">No jobs found</p>
                </div>
            `;
            return;
        }

        container.innerHTML = data.jobs.map(job => renderJobCard(job)).join('');

    } catch (error) {
        setError('#jobs-list', `Failed to load jobs: ${error.message}`);
    }
}

/**
 * Update a single job card in place (for real-time updates)
 */
function updateJobCard(job) {
    const container = document.getElementById('jobs-list');
    if (!container) return;

    const existingCard = container.querySelector(`[data-job-id="${job.id}"]`);
    
    if (existingCard) {
        // Update existing card
        const newCard = document.createElement('div');
        newCard.innerHTML = renderJobCard(job);
        existingCard.replaceWith(newCard.firstElementChild);
    } else {
        // New job - add at the top
        const newCard = document.createElement('div');
        newCard.innerHTML = renderJobCard(job);
        container.prepend(newCard.firstElementChild);
    }
}

/**
 * Filter jobs
 */
export function filter() {
    load();
}

/**
 * Show job details modal
 */
export async function showDetails(jobId) {
    currentJobId = jobId;
    showModal('job-modal');

    try {
        const job = await api.getJob(jobId);
        updateModal(job);

        // Connect to job WebSocket for real-time updates
        if (job.status === 'running' || job.status === 'pending') {
            console.log(`[Jobs] Connecting WebSocket for job ${jobId}`);
            currentJobWs = ws.connectJob(jobId, 
                (data) => {
                    console.log(`[Jobs] WebSocket message:`, data);
                    if (data.type === 'update' || data.type === 'connected') {
                        updateModal(data.job);
                    } else if (data.type === 'finished') {
                        updateModal(data.job);
                        load(); // Refresh jobs list
                    }
                },
                (error) => {
                    console.error(`[Jobs] WebSocket error:`, error);
                }
            );
        }
    } catch (error) {
        showToast(`Failed to load job: ${error.message}`, 'error');
        hideDetails();
    }
}

/**
 * Update job modal
 */
function updateModal(job) {
    const content = renderJobModalContent(job);

    document.getElementById('job-modal-title').textContent = content.title;
    
    const statusEl = document.getElementById('job-modal-status');
    statusEl.innerHTML = content.status;
    
    document.getElementById('job-modal-progress').textContent = content.progress;
    document.getElementById('job-modal-progress-bar').style.width = content.progressWidth;
    document.getElementById('job-modal-step').textContent = content.step;
    
    const logsContainer = document.getElementById('job-modal-logs');
    logsContainer.innerHTML = content.logs;
    logsContainer.scrollTop = logsContainer.scrollHeight;
    
    document.getElementById('job-modal-created').textContent = content.created;
    
    const cancelBtn = document.getElementById('job-modal-cancel');
    if (content.showCancel) {
        cancelBtn.classList.remove('hidden');
    } else {
        cancelBtn.classList.add('hidden');
    }
}

/**
 * Hide job details modal
 */
export function hideDetails() {
    hideModal('job-modal');
    if (currentJobWs) {
        currentJobWs.close();
        currentJobWs = null;
    }
    currentJobId = null;
}

/**
 * Cancel a job
 */
export async function cancel(jobId) {
    if (!confirm('Are you sure you want to cancel this job?')) return;

    try {
        await api.cancelJob(jobId);
        showToast('Job cancelled', 'success');
        load();
    } catch (error) {
        showToast(`Failed to cancel job: ${error.message}`, 'error');
    }
}

/**
 * Cancel current job in modal
 */
export async function cancelCurrent() {
    if (currentJobId) {
        await cancel(currentJobId);
        hideDetails();
    }
}

/**
 * Start jobs WebSocket for real-time updates
 */
export function startWebSocket() {
    if (jobsWs) {
        jobsWs.close();
    }

    console.log('[Jobs] Starting all-jobs WebSocket');
    jobsWs = ws.connectAllJobs(
        (data) => {
            console.log('[Jobs] All-jobs WebSocket message:', data);
            if (data.type === 'job_update') {
                // Update job card in place if on jobs page
                const jobsPage = document.getElementById('page-jobs');
                if (jobsPage && !jobsPage.classList.contains('hidden')) {
                    updateJobCard(data.job);
                }
                // Update badge
                updateActiveJobsBadge(data.job.status === 'running' || data.job.status === 'pending' ? -1 : null);
            } else if (data.type === 'connected') {
                updateActiveJobsBadge(data.active);
            }
        },
        (error) => {
            console.error('[Jobs] All-jobs WebSocket error:', error);
        }
    );
}

/**
 * Check active jobs on load
 */
export async function checkActive() {
    try {
        const data = await api.getActiveJobs();
        updateActiveJobsBadge(data.active);
    } catch (error) {
        console.error('Failed to check active jobs:', error);
    }
}

export default {
    load,
    filter,
    showDetails,
    hideDetails,
    cancel,
    cancelCurrent,
    startWebSocket,
    checkActive
};

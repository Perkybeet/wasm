/**
 * WASM Web Dashboard - Backups Page
 */

import { api } from '../core/api.js';
import { showToast, showModal, hideModal, setLoading, setEmpty, setError, confirm, formatBytes, formatTime } from '../core/ui.js';
import { router } from '../core/router.js';

let currentBackupId = null;
let allBackups = [];

/**
 * Load backups page
 */
export async function load() {
    await Promise.all([
        loadStorageInfo(),
        loadBackups(),
        loadAppSelect()
    ]);
}

/**
 * Load storage information
 */
async function loadStorageInfo() {
    const container = document.getElementById('backup-storage-info');
    if (!container) return;

    try {
        const data = await api.getBackupStorage();
        container.innerHTML = `
            <div class="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
                <div>
                    <span class="text-slate-400">Total Backups</span>
                    <div class="font-semibold text-lg">${data.total_backups || 0}</div>
                </div>
                <div>
                    <span class="text-slate-400">Total Size</span>
                    <div class="font-semibold text-lg">${formatBytes(data.total_size || 0)}</div>
                </div>
                <div>
                    <span class="text-slate-400">Storage Path</span>
                    <div class="font-mono text-xs truncate" title="${data.path || 'N/A'}">${data.path || 'N/A'}</div>
                </div>
                <div>
                    <span class="text-slate-400">Available Space</span>
                    <div class="font-semibold text-lg">${formatBytes(data.available_space || 0)}</div>
                </div>
            </div>
        `;
    } catch (error) {
        container.innerHTML = `
            <div class="text-slate-400 text-center py-2">
                <i class="fas fa-info-circle mr-2"></i>
                Unable to load storage info
            </div>
        `;
    }
}

/**
 * Load backups list
 */
async function loadBackups() {
    const container = document.getElementById('backups-list');
    if (!container) return;

    setLoading('#backups-list');

    try {
        const data = await api.getBackups();
        allBackups = data.backups || [];

        renderBackups(allBackups);
        updateAppFilterOptions(allBackups);
    } catch (error) {
        setError('#backups-list', `Failed to load backups: ${error.message}`);
    }
}

/**
 * Render backups list
 */
function renderBackups(backups) {
    const container = document.getElementById('backups-list');
    if (!container) return;

    if (backups.length === 0) {
        setEmpty('#backups-list', 'No backups found', 'fa-archive');
        return;
    }

    container.innerHTML = backups.map(backup => `
        <div class="card p-4 hover:border-indigo-500/50 cursor-pointer transition-colors" onclick="window.backupsPage.showDetails('${backup.id}')">
            <div class="flex items-center justify-between">
                <div class="flex items-center gap-4">
                    <div class="w-10 h-10 rounded-lg bg-indigo-500/20 flex items-center justify-center">
                        <i class="fas fa-archive text-indigo-400"></i>
                    </div>
                    <div>
                        <div class="font-semibold">${backup.domain}</div>
                        <div class="text-sm text-slate-400">
                            ${backup.description || 'No description'}
                        </div>
                    </div>
                </div>
                <div class="flex items-center gap-6">
                    <div class="text-right">
                        <div class="text-sm">${formatBytes(backup.size || 0)}</div>
                        <div class="text-xs text-slate-400">${formatTime(backup.created_at)}</div>
                    </div>
                    <div class="flex gap-2">
                        ${backup.verified ? 
                            '<span class="text-green-400" title="Verified"><i class="fas fa-check-circle"></i></span>' : 
                            '<span class="text-yellow-400" title="Not verified"><i class="fas fa-question-circle"></i></span>'
                        }
                    </div>
                </div>
            </div>
            ${backup.tags && backup.tags.length > 0 ? `
                <div class="mt-2 flex gap-2">
                    ${backup.tags.map(tag => `
                        <span class="text-xs px-2 py-1 rounded bg-slate-700 text-slate-300">${tag}</span>
                    `).join('')}
                </div>
            ` : ''}
        </div>
    `).join('');
}

/**
 * Update app filter options from backups
 */
function updateAppFilterOptions(backups) {
    const filter = document.getElementById('backup-app-filter');
    if (!filter) return;

    const domains = [...new Set(backups.map(b => b.domain))];
    filter.innerHTML = '<option value="">All Applications</option>' +
        domains.map(d => `<option value="${d}">${d}</option>`).join('');
}

/**
 * Load app select options for create modal
 */
async function loadAppSelect() {
    try {
        const data = await api.getApps();
        const select = document.getElementById('backup-app-select');
        if (select) {
            select.innerHTML = '<option value="">Select Application...</option>' +
                data.apps.map(app => `<option value="${app.domain}">${app.domain}</option>`).join('');
        }
    } catch (error) {
        console.error('Failed to load apps for backup select:', error);
    }
}

/**
 * Filter backups
 */
export function filter() {
    const appFilter = document.getElementById('backup-app-filter')?.value || '';
    const search = document.getElementById('backup-search')?.value?.toLowerCase() || '';

    const filtered = allBackups.filter(backup => {
        if (appFilter && backup.domain !== appFilter) return false;
        if (search) {
            const searchTarget = `${backup.domain} ${backup.description || ''} ${(backup.tags || []).join(' ')}`.toLowerCase();
            if (!searchTarget.includes(search)) return false;
        }
        return true;
    });

    renderBackups(filtered);
}

/**
 * Show create backup modal
 */
export function showCreate() {
    showModal('create-backup-modal');
}

/**
 * Hide create backup modal
 */
export function hideCreate() {
    hideModal('create-backup-modal');
    document.getElementById('create-backup-form')?.reset();
}

/**
 * Create a backup
 */
export async function create(event) {
    event.preventDefault();

    const form = event.target;
    const formData = new FormData(form);

    const domain = formData.get('domain');
    if (!domain) {
        showToast('Please select an application', 'error');
        return;
    }

    const data = {
        domain,
        description: formData.get('description') || '',
        tags: formData.get('tags') ? formData.get('tags').split(',').map(t => t.trim()).filter(t => t) : [],
        include_env: formData.get('include_env') === 'on',
        include_build: formData.get('include_build') === 'on',
    };

    try {
        const result = await api.createBackup(data);
        showToast(`Backup job started for ${domain}`, 'info');
        hideCreate();
        router.navigate('jobs');
        if (result.job?.id) {
            setTimeout(() => window.jobsPage?.showDetails(result.job.id), 500);
        }
    } catch (error) {
        showToast(`Failed to create backup: ${error.message}`, 'error');
    }
}

/**
 * Show backup details
 */
export async function showDetails(backupId) {
    currentBackupId = backupId;
    const backup = allBackups.find(b => b.id === backupId);
    
    if (!backup) {
        // Try to fetch from API
        try {
            const data = await api.getBackup(backupId);
            renderBackupDetails(data);
        } catch (error) {
            showToast(`Failed to load backup details: ${error.message}`, 'error');
            return;
        }
    } else {
        renderBackupDetails(backup);
    }
    
    showModal('backup-details-modal');
}

/**
 * Render backup details in modal
 */
function renderBackupDetails(backup) {
    const container = document.getElementById('backup-details-content');
    if (!container) return;

    container.innerHTML = `
        <div class="grid grid-cols-2 gap-4">
            <div>
                <span class="text-slate-400 text-sm">Backup ID</span>
                <div class="font-mono text-sm">${backup.id}</div>
            </div>
            <div>
                <span class="text-slate-400 text-sm">Application</span>
                <div>${backup.domain}</div>
            </div>
            <div>
                <span class="text-slate-400 text-sm">Created</span>
                <div>${formatTime(backup.created_at)}</div>
            </div>
            <div>
                <span class="text-slate-400 text-sm">Size</span>
                <div>${formatBytes(backup.size || 0)}</div>
            </div>
            <div class="col-span-2">
                <span class="text-slate-400 text-sm">Description</span>
                <div>${backup.description || 'No description'}</div>
            </div>
            ${backup.checksum ? `
                <div class="col-span-2">
                    <span class="text-slate-400 text-sm">Checksum</span>
                    <div class="font-mono text-xs break-all">${backup.checksum}</div>
                </div>
            ` : ''}
            ${backup.tags && backup.tags.length > 0 ? `
                <div class="col-span-2">
                    <span class="text-slate-400 text-sm">Tags</span>
                    <div class="flex gap-2 mt-1">
                        ${backup.tags.map(tag => `
                            <span class="text-xs px-2 py-1 rounded bg-slate-700 text-slate-300">${tag}</span>
                        `).join('')}
                    </div>
                </div>
            ` : ''}
        </div>
        <div class="mt-4">
            <button onclick="window.backupsPage.verify('${backup.id}')" class="text-sm text-indigo-400 hover:text-indigo-300">
                <i class="fas fa-check-double mr-1"></i>
                Verify Integrity
            </button>
        </div>
    `;
}

/**
 * Hide backup details modal
 */
export function hideDetails() {
    hideModal('backup-details-modal');
    currentBackupId = null;
}

/**
 * Verify a backup
 */
export async function verify(backupId) {
    try {
        showToast('Verifying backup...', 'info');
        const result = await api.verifyBackup(backupId);
        if (result.valid) {
            showToast('Backup verified successfully', 'success');
        } else {
            showToast(`Backup verification failed: ${result.error || 'Unknown error'}`, 'error');
        }
    } catch (error) {
        showToast(`Verification failed: ${error.message}`, 'error');
    }
}

/**
 * Restore current backup
 */
export async function restore() {
    if (!currentBackupId) return;
    
    if (!await confirm('Are you sure you want to restore this backup? This will overwrite the current application files.')) {
        return;
    }

    try {
        const result = await api.restoreBackup(currentBackupId);
        showToast(`Restore job started`, 'info');
        hideDetails();
        router.navigate('jobs');
        if (result.job?.id) {
            setTimeout(() => window.jobsPage?.showDetails(result.job.id), 500);
        }
    } catch (error) {
        showToast(`Restore failed: ${error.message}`, 'error');
    }
}

/**
 * Delete current backup
 */
export async function remove() {
    if (!currentBackupId) return;
    
    if (!await confirm('Are you sure you want to delete this backup? This cannot be undone.')) {
        return;
    }

    try {
        await api.deleteBackup(currentBackupId);
        showToast('Backup deleted successfully', 'success');
        hideDetails();
        load();
    } catch (error) {
        showToast(`Failed to delete backup: ${error.message}`, 'error');
    }
}

export default {
    load,
    filter,
    showCreate,
    hideCreate,
    create,
    showDetails,
    hideDetails,
    verify,
    restore,
    remove
};

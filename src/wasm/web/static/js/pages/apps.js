/**
 * WASM Web Dashboard - Applications Page
 */

import { api } from '../core/api.js';
import { showToast, showModal, hideModal, setLoading, setEmpty, setError, getFormData, resetForm, confirm } from '../core/ui.js';
import { router } from '../core/router.js';
import { renderAppCard } from '../components/cards.js';

/**
 * Load applications list
 */
export async function load() {
    const container = document.getElementById('apps-list');
    if (!container) return;

    setLoading('#apps-list');

    try {
        const data = await api.getApps();

        if (data.apps.length === 0) {
            container.innerHTML = `
                <div class="card p-8 text-center">
                    <i class="fas fa-cube text-4xl text-slate-600 mb-4"></i>
                    <p class="text-slate-400">No applications deployed yet</p>
                    <button onclick="window.appsPage.showCreate()" class="btn-primary mt-4 px-6 py-2 rounded-lg">
                        Deploy Your First App
                    </button>
                </div>
            `;
            return;
        }

        container.innerHTML = data.apps.map(app => renderAppCard(app, {
            start: `window.appsPage.action('${app.domain}', 'start')`,
            stop: `window.appsPage.action('${app.domain}', 'stop')`,
            restart: `window.appsPage.action('${app.domain}', 'restart')`,
            update: `window.appsPage.update('${app.domain}')`,
            backup: `window.appsPage.backup('${app.domain}')`,
            logs: `window.appsPage.viewLogs('${app.domain}')`,
            delete: `window.appsPage.remove('${app.domain}')`
        })).join('');

        // Update logs page selector
        updateLogAppSelect(data.apps);

    } catch (error) {
        setError('#apps-list', `Failed to load applications: ${error.message}`);
    }
}

/**
 * Perform app action (start/stop/restart)
 */
export async function action(domain, actionType) {
    try {
        switch (actionType) {
            case 'start':
                await api.startApp(domain);
                showToast(`Started ${domain}`, 'success');
                break;
            case 'stop':
                await api.stopApp(domain);
                showToast(`Stopped ${domain}`, 'success');
                break;
            case 'restart':
                await api.restartApp(domain);
                showToast(`Restarted ${domain}`, 'success');
                break;
        }
        load();
    } catch (error) {
        showToast(`Failed to ${actionType} ${domain}: ${error.message}`, 'error');
    }
}

/**
 * Update app (pull & rebuild)
 */
export async function update(domain) {
    try {
        const result = await api.updateApp(domain);
        showToast(`Update job started for ${domain}`, 'info');
        router.navigate('jobs');
        setTimeout(() => window.jobsPage?.showDetails(result.job.id), 500);
    } catch (error) {
        showToast(`Failed to update: ${error.message}`, 'error');
    }
}

/**
 * Backup app
 */
export async function backup(domain) {
    try {
        const result = await api.backupApp(domain);
        showToast(`Backup job started for ${domain}`, 'info');
        router.navigate('jobs');
        setTimeout(() => window.jobsPage?.showDetails(result.job.id), 500);
    } catch (error) {
        showToast(`Failed to create backup: ${error.message}`, 'error');
    }
}

/**
 * View app logs
 */
export function viewLogs(domain) {
    router.navigate('logs');
    setTimeout(() => {
        const select = document.getElementById('log-app-select');
        if (select) {
            select.value = domain;
            window.logsPage?.switchStream();
        }
    }, 100);
}

/**
 * Delete app
 */
export async function remove(domain) {
    if (!confirm(`Are you sure you want to delete ${domain}? This will remove the application, files, and SSL certificates.`)) {
        return;
    }

    try {
        const result = await api.deleteAppJob(domain, true, true);
        showToast(`Deletion job started for ${domain}`, 'info');
        router.navigate('jobs');
        setTimeout(() => window.jobsPage?.showDetails(result.job.id), 500);
    } catch (error) {
        showToast(`Failed to delete: ${error.message}`, 'error');
    }
}

/**
 * Show create app modal
 */
export function showCreate() {
    showModal('create-app-modal');
}

/**
 * Hide create app modal
 */
export function hideCreate() {
    hideModal('create-app-modal');
    resetForm('#create-app-form');
}

/**
 * Create/deploy new app
 */
export async function create(event) {
    event.preventDefault();

    const form = event.target;
    const formData = new FormData(form);
    
    // Parse environment variables from textarea
    const envVarsText = formData.get('env_vars') || '';
    const env_vars = {};
    for (const line of envVarsText.split('\n')) {
        const trimmed = line.trim();
        if (trimmed && trimmed.includes('=')) {
            const [key, ...valueParts] = trimmed.split('=');
            env_vars[key.trim()] = valueParts.join('=').trim();
        }
    }

    const data = {
        domain: formData.get('domain'),
        source: formData.get('source'),
        app_type: formData.get('app_type'),
        port: formData.get('port') ? parseInt(formData.get('port')) : null,
        branch: formData.get('branch') || null,
        webserver: formData.get('webserver') || 'nginx',
        ssl: formData.get('ssl') === 'on' || formData.get('ssl') === true,
        package_manager: formData.get('package_manager') || 'auto',
        env_vars: Object.keys(env_vars).length > 0 ? env_vars : {},
    };

    try {
        const result = await api.deployApp(data);
        showToast(`Deployment job started for ${data.domain}`, 'info');
        hideCreate();
        router.navigate('jobs');
        setTimeout(() => window.jobsPage?.showDetails(result.job.id), 500);
    } catch (error) {
        showToast(`Failed to start deployment: ${error.message}`, 'error');
    }
}

/**
 * Update logs page app selector
 */
function updateLogAppSelect(apps) {
    const select = document.getElementById('log-app-select');
    if (select) {
        select.innerHTML = '<option value="">Select Application...</option>' +
            apps.map(app => `<option value="${app.domain}">${app.domain}</option>`).join('');
    }
}

// Export for global access
export default {
    load,
    action,
    update,
    backup,
    viewLogs,
    remove,
    showCreate,
    hideCreate,
    create
};

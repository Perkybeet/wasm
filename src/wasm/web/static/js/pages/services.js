/**
 * WASM Web Dashboard - Services Page
 */

import { api } from '../core/api.js';
import { showToast, showModal, hideModal, setLoading, setEmpty, setError, confirm, escapeHtml } from '../core/ui.js';
import { renderServiceCard } from '../components/cards.js';

/**
 * Load services list
 */
export async function load() {
    const container = document.getElementById('services-list');
    if (!container) return;

    setLoading('#services-list');
    
    // Check if WASM-only toggle is checked
    const wasmOnlyToggle = document.getElementById('services-wasm-only');
    const wasmOnly = wasmOnlyToggle ? wasmOnlyToggle.checked : true;

    try {
        const data = await api.getServices(wasmOnly);

        if (data.services.length === 0) {
            const msg = wasmOnly ? 'No WASM services found' : 'No services found';
            setEmpty('#services-list', msg, 'fa-cogs');
            return;
        }

        container.innerHTML = data.services.map(svc => {
            const isWasmService = svc.name.startsWith('wasm-');
            return renderServiceCard(svc, {
                start: `window.servicesPage.action('${svc.name}', 'start')`,
                stop: `window.servicesPage.action('${svc.name}', 'stop')`,
                restart: `window.servicesPage.action('${svc.name}', 'restart')`,
                config: isWasmService ? `window.servicesPage.viewConfig('${svc.name}')` : null,
                logs: `window.servicesPage.viewLogs('${svc.name}')`,
                remove: isWasmService ? `window.servicesPage.remove('${svc.name}')` : null
            }, isWasmService);
        }).join('');

    } catch (error) {
        setError('#services-list', `Failed to load services: ${error.message}`);
    }
}

/**
 * Perform service action
 */
export async function action(name, actionType) {
    try {
        switch (actionType) {
            case 'start':
                await api.startService(name);
                break;
            case 'stop':
                await api.stopService(name);
                break;
            case 'restart':
                await api.restartService(name);
                break;
        }
        showToast(`Service ${actionType}ed: ${name}`, 'success');
        load();
    } catch (error) {
        showToast(`Failed: ${error.message}`, 'error');
    }
}

/**
 * Show create service modal
 */
export function showCreate() {
    showModal('create-service-modal');
}

/**
 * Hide create service modal
 */
export function hideCreate() {
    hideModal('create-service-modal');
    document.getElementById('create-service-form')?.reset();
}

/**
 * Create a new service
 */
export async function create(event) {
    event.preventDefault();

    const form = event.target;
    const formData = new FormData(form);
    
    // Check which mode is active
    const advancedMode = document.getElementById('service-advanced-mode');
    const isAdvanced = advancedMode && !advancedMode.classList.contains('hidden');

    let data;
    
    if (isAdvanced) {
        // Advanced mode - raw systemd unit
        const name = formData.get('name_advanced');
        const rawContent = formData.get('raw_content');
        
        if (!name) {
            showToast('Service name is required', 'error');
            return;
        }
        if (!rawContent) {
            showToast('Service content is required', 'error');
            return;
        }
        
        data = {
            name: name,
            raw_content: rawContent,
        };
    } else {
        // Simple mode
        data = {
            name: formData.get('name'),
            command: formData.get('command'),
            directory: formData.get('directory'),
            user: formData.get('user') || 'www-data',
            description: formData.get('description') || '',
        };
    }

    try {
        await api.createService(data);
        showToast(`Service created: ${data.name}`, 'success');
        hideCreate();
        load();
    } catch (error) {
        showToast(`Failed to create service: ${error.message}`, 'error');
    }
}

/**
 * View service logs
 */
export async function viewLogs(name) {
    const titleEl = document.getElementById('service-logs-title');
    const contentEl = document.getElementById('service-logs-content');
    
    if (titleEl) titleEl.textContent = `Logs: ${name}`;
    if (contentEl) contentEl.innerHTML = '<div class="text-slate-500">Loading logs...</div>';
    
    showModal('service-logs-modal');

    try {
        const data = await api.getServiceLogs(name, 200);
        if (contentEl) {
            contentEl.innerHTML = data.logs 
                ? `<pre class="whitespace-pre-wrap text-slate-300">${escapeHtml(data.logs)}</pre>`
                : '<div class="text-slate-500">No logs available</div>';
        }
    } catch (error) {
        if (contentEl) {
            contentEl.innerHTML = `<div class="text-red-400">Failed to load logs: ${escapeHtml(error.message)}</div>`;
        }
    }
}

/**
 * Hide logs modal
 */
export function hideLogs() {
    hideModal('service-logs-modal');
}

/**
 * Delete a service
 */
export async function remove(name) {
    if (!await confirm(`Are you sure you want to delete service ${name}?`)) {
        return;
    }

    try {
        await api.deleteService(name);
        showToast(`Service deleted: ${name}`, 'success');
        load();
    } catch (error) {
        showToast(`Failed to delete service: ${error.message}`, 'error');
    }
}

/**
 * Toggle between simple and advanced mode for service creation
 */
export function toggleCreateMode(mode) {
    const simpleMode = document.getElementById('service-simple-mode');
    const advancedMode = document.getElementById('service-advanced-mode');
    const simpleBtn = document.getElementById('service-mode-simple');
    const advancedBtn = document.getElementById('service-mode-advanced');
    
    if (mode === 'simple') {
        simpleMode?.classList.remove('hidden');
        advancedMode?.classList.add('hidden');
        simpleBtn?.classList.add('bg-indigo-500/20', 'text-indigo-400');
        simpleBtn?.classList.remove('text-slate-400');
        advancedBtn?.classList.remove('bg-indigo-500/20', 'text-indigo-400');
        advancedBtn?.classList.add('text-slate-400');
    } else {
        simpleMode?.classList.add('hidden');
        advancedMode?.classList.remove('hidden');
        advancedBtn?.classList.add('bg-indigo-500/20', 'text-indigo-400');
        advancedBtn?.classList.remove('text-slate-400');
        simpleBtn?.classList.remove('bg-indigo-500/20', 'text-indigo-400');
        simpleBtn?.classList.add('text-slate-400');
    }
}

// State for config editing
let currentServiceName = null;
let originalServiceConfig = '';

/**
 * View service configuration
 */
export async function viewConfig(name) {
    currentServiceName = name;
    const titleEl = document.getElementById('service-config-title');
    const contentEl = document.getElementById('service-config-content');
    const editTextarea = document.getElementById('service-config-edit');
    const viewModeBtn = document.getElementById('service-config-view-mode');
    const editModeBtn = document.getElementById('service-config-edit-mode');
    const viewContent = document.getElementById('service-config-view-content');
    const editContent = document.getElementById('service-config-edit-content');
    
    if (titleEl) titleEl.textContent = `Config: ${name}`;
    if (contentEl) contentEl.innerHTML = '<div class="text-slate-500">Loading configuration...</div>';
    
    // Reset to view mode
    viewModeBtn?.classList.remove('hidden');
    editModeBtn?.classList.add('hidden');
    viewContent?.classList.remove('hidden');
    editContent?.classList.add('hidden');
    
    showModal('service-config-modal');

    try {
        const data = await api.getServiceConfig(name);
        originalServiceConfig = data.config || '';
        
        if (contentEl) {
            contentEl.innerHTML = data.config 
                ? `<pre class="whitespace-pre-wrap text-slate-300">${escapeHtml(data.config)}</pre>`
                : '<div class="text-slate-500">No configuration available</div>';
        }
        if (editTextarea) {
            editTextarea.value = data.config || '';
        }
    } catch (error) {
        if (contentEl) {
            contentEl.innerHTML = `<div class="text-red-400">Failed to load configuration: ${escapeHtml(error.message)}</div>`;
        }
    }
}

/**
 * Hide config modal
 */
export function hideConfig() {
    hideModal('service-config-modal');
    currentServiceName = null;
    originalServiceConfig = '';
}

/**
 * Toggle config edit mode
 */
export function toggleConfigEdit() {
    const viewModeBtn = document.getElementById('service-config-view-mode');
    const editModeBtn = document.getElementById('service-config-edit-mode');
    const viewContent = document.getElementById('service-config-view-content');
    const editContent = document.getElementById('service-config-edit-content');
    const editTextarea = document.getElementById('service-config-edit');
    
    if (viewContent?.classList.contains('hidden')) {
        // Switch to view mode
        viewModeBtn?.classList.remove('hidden');
        editModeBtn?.classList.add('hidden');
        viewContent?.classList.remove('hidden');
        editContent?.classList.add('hidden');
    } else {
        // Switch to edit mode
        viewModeBtn?.classList.add('hidden');
        editModeBtn?.classList.remove('hidden');
        viewContent?.classList.add('hidden');
        editContent?.classList.remove('hidden');
        editTextarea?.focus();
    }
}

/**
 * Cancel config edit
 */
export function cancelConfigEdit() {
    const editTextarea = document.getElementById('service-config-edit');
    if (editTextarea) {
        editTextarea.value = originalServiceConfig;
    }
    toggleConfigEdit();
}

/**
 * Save service config
 */
export async function saveConfig() {
    const editTextarea = document.getElementById('service-config-edit');
    const newConfig = editTextarea?.value || '';
    
    if (!currentServiceName) {
        showToast('No service selected', 'error');
        return;
    }
    
    try {
        await api.updateServiceConfig(currentServiceName, newConfig);
        showToast('Configuration saved. Restart the service to apply changes.', 'success');
        originalServiceConfig = newConfig;
        
        // Update the view mode content
        const contentEl = document.getElementById('service-config-content');
        if (contentEl) {
            contentEl.innerHTML = `<pre class="whitespace-pre-wrap text-slate-300">${escapeHtml(newConfig)}</pre>`;
        }
        
        toggleConfigEdit();
    } catch (error) {
        showToast(`Failed to save configuration: ${error.message}`, 'error');
    }
}

export default { load, action, showCreate, hideCreate, create, viewLogs, hideLogs, viewConfig, hideConfig, toggleConfigEdit, cancelConfigEdit, saveConfig, remove, toggleCreateMode };

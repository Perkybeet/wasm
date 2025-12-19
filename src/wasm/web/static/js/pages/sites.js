/**
 * WASM Web Dashboard - Sites Page
 */

import { api } from '../core/api.js';
import { showToast, showModal, hideModal, setLoading, setEmpty, setError, confirm, escapeHtml } from '../core/ui.js';
import { renderSiteCard } from '../components/cards.js';

/**
 * Load sites list
 */
export async function load() {
    const container = document.getElementById('sites-list');
    if (!container) return;

    setLoading('#sites-list');

    try {
        const data = await api.getSites();

        if (data.sites.length === 0) {
            setEmpty('#sites-list', 'No sites configured', 'fa-globe');
            return;
        }

        container.innerHTML = data.sites.map(site => renderSiteCard(site, data.webserver, {
            enable: `window.sitesPage.action('${site.name}', 'enable')`,
            disable: `window.sitesPage.action('${site.name}', 'disable')`,
            viewConfig: `window.sitesPage.viewConfig('${site.name}')`,
            remove: `window.sitesPage.remove('${site.name}')`
        })).join('');

    } catch (error) {
        setError('#sites-list', `Failed to load sites: ${error.message}`);
    }
}

/**
 * Perform site action
 */
export async function action(name, actionType) {
    try {
        if (actionType === 'enable') {
            await api.enableSite(name);
        } else {
            await api.disableSite(name);
        }
        showToast(`Site ${actionType}d: ${name}`, 'success');
        load();
    } catch (error) {
        showToast(`Failed: ${error.message}`, 'error');
    }
}

/**
 * Show create site modal
 */
export function showCreate() {
    showModal('create-site-modal');
}

/**
 * Hide create site modal
 */
export function hideCreate() {
    hideModal('create-site-modal');
    document.getElementById('create-site-form')?.reset();
}

/**
 * Create a new site
 */
export async function create(event) {
    event.preventDefault();

    const form = event.target;
    const formData = new FormData(form);
    
    // Check which mode is active
    const advancedMode = document.getElementById('site-advanced-mode');
    const isAdvanced = advancedMode && !advancedMode.classList.contains('hidden');

    let data;
    
    if (isAdvanced) {
        // Advanced mode - raw config
        const domain = formData.get('domain_advanced');
        const webserver = formData.get('webserver_advanced');
        const rawConfig = formData.get('raw_config');
        
        if (!domain) {
            showToast('Site name is required', 'error');
            return;
        }
        if (!rawConfig) {
            showToast('Configuration content is required', 'error');
            return;
        }
        
        data = {
            domain: domain,
            webserver: webserver || 'nginx',
            raw_config: rawConfig,
        };
    } else {
        // Simple mode
        data = {
            domain: formData.get('domain'),
            webserver: formData.get('webserver') || 'nginx',
            template: formData.get('template') || 'proxy',
            port: parseInt(formData.get('port')) || 3000,
        };
    }

    try {
        await api.createSite(data);
        showToast(`Site created: ${data.domain}`, 'success');
        hideCreate();
        load();
    } catch (error) {
        showToast(`Failed to create site: ${error.message}`, 'error');
    }
}

/**
 * View site configuration
 */
let currentSiteName = null;

export async function viewConfig(name) {
    currentSiteName = name;
    const titleEl = document.getElementById('site-config-title');
    const codeEl = document.getElementById('site-config-code');
    const textareaEl = document.getElementById('site-config-textarea');
    const viewEl = document.getElementById('site-config-view');
    const editEl = document.getElementById('site-config-edit');
    const editBtn = document.getElementById('site-config-edit-btn');
    
    // Reset to view mode
    viewEl?.classList.remove('hidden');
    editEl?.classList.add('hidden');
    if (editBtn) {
        editBtn.innerHTML = '<i class="fas fa-edit mr-1"></i> Edit';
    }
    
    if (titleEl) titleEl.textContent = `Configuration: ${name}`;
    if (codeEl) codeEl.textContent = 'Loading...';
    
    showModal('site-config-modal');

    try {
        const data = await api.getSiteConfig(name);
        const config = data.config || 'No configuration found';
        if (codeEl) {
            codeEl.textContent = config;
        }
        if (textareaEl) {
            textareaEl.value = config;
        }
    } catch (error) {
        if (codeEl) {
            codeEl.textContent = `Failed to load config: ${error.message}`;
        }
    }
}

/**
 * Toggle config edit mode
 */
export function toggleConfigEdit() {
    const viewEl = document.getElementById('site-config-view');
    const editEl = document.getElementById('site-config-edit');
    const editBtn = document.getElementById('site-config-edit-btn');
    
    const isEditing = !editEl?.classList.contains('hidden');
    
    if (isEditing) {
        // Switch to view mode
        viewEl?.classList.remove('hidden');
        editEl?.classList.add('hidden');
        if (editBtn) {
            editBtn.innerHTML = '<i class="fas fa-edit mr-1"></i> Edit';
        }
    } else {
        // Switch to edit mode
        viewEl?.classList.add('hidden');
        editEl?.classList.remove('hidden');
        if (editBtn) {
            editBtn.innerHTML = '<i class="fas fa-eye mr-1"></i> View';
        }
    }
}

/**
 * Cancel config editing
 */
export function cancelConfigEdit() {
    const codeEl = document.getElementById('site-config-code');
    const textareaEl = document.getElementById('site-config-textarea');
    
    // Restore original content
    if (codeEl && textareaEl) {
        textareaEl.value = codeEl.textContent;
    }
    
    toggleConfigEdit();
}

/**
 * Save site configuration
 */
export async function saveConfig() {
    const textareaEl = document.getElementById('site-config-textarea');
    const codeEl = document.getElementById('site-config-code');
    
    if (!currentSiteName || !textareaEl) {
        showToast('Error: No site selected', 'error');
        return;
    }
    
    const newConfig = textareaEl.value;
    
    try {
        await api.updateSiteConfig(currentSiteName, newConfig);
        showToast(`Configuration saved for ${currentSiteName}`, 'success');
        
        // Update the view
        if (codeEl) {
            codeEl.textContent = newConfig;
        }
        
        // Switch back to view mode
        toggleConfigEdit();
        
        // Reload sites list
        load();
    } catch (error) {
        showToast(`Failed to save configuration: ${error.message}`, 'error');
    }
}

/**
 * Hide site config modal
 */
export function hideConfig() {
    hideModal('site-config-modal');
    currentSiteName = null;
}

/**
 * Delete a site
 */
export async function remove(name) {
    if (!await confirm(`Are you sure you want to delete site ${name}?`)) {
        return;
    }

    try {
        await api.deleteSite(name);
        showToast(`Site deleted: ${name}`, 'success');
        load();
    } catch (error) {
        showToast(`Failed to delete site: ${error.message}`, 'error');
    }
}

/**
 * Reload web server
 */
export async function reloadServer() {
    try {
        await api.reloadWebserver();
        showToast('Web server reloaded', 'success');
    } catch (error) {
        showToast(`Failed: ${error.message}`, 'error');
    }
}

/**
 * Toggle between simple and advanced mode for site creation
 */
export function toggleCreateMode(mode) {
    const simpleMode = document.getElementById('site-simple-mode');
    const advancedMode = document.getElementById('site-advanced-mode');
    const simpleBtn = document.getElementById('site-mode-simple');
    const advancedBtn = document.getElementById('site-mode-advanced');
    
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

export default { load, action, showCreate, hideCreate, create, viewConfig, hideConfig, toggleConfigEdit, cancelConfigEdit, saveConfig, remove, reloadServer, toggleCreateMode };

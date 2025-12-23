/**
 * WASM Web Dashboard - Databases Page
 * 
 * Database management with engine-centric navigation:
 * - Main view: List of database engines
 * - Engine detail: Databases, users, backups for selected engine
 */

import { api } from '../core/api.js';
import { showToast, showModal, hideModal, setLoading, setEmpty, setError, confirm } from '../core/ui.js';

// State
let engines = [];
let databases = [];
let users = [];
let backups = [];
let selectedEngine = null;  // Currently selected engine for detail view
let currentTab = 'databases'; // 'databases', 'users', 'backups'

// ==================== Main Entry Points ====================

/**
 * Load databases page - shows engines list
 */
export async function load() {
    selectedEngine = null;
    setLoading('#databases-list');
    await loadEngines();
    renderEnginesView();
}

/**
 * Refresh current view
 */
export async function refresh() {
    if (selectedEngine) {
        await loadEngineDetail(selectedEngine);
    } else {
        await load();
    }
}

// ==================== Engines List View ====================

async function loadEngines() {
    try {
        const data = await api.getDbEngines();
        engines = data.engines || [];
    } catch (error) {
        console.error('Failed to load engines:', error);
        engines = [];
    }
}

function renderEnginesView() {
    const container = document.getElementById('databases-list');
    const actionsContainer = document.getElementById('databases-actions');
    const breadcrumb = document.getElementById('databases-breadcrumb');
    
    if (!container) return;
    
    // Update breadcrumb
    if (breadcrumb) {
        breadcrumb.innerHTML = '<span class="text-slate-400">Database Engines</span>';
    }
    
    // Hide detail actions, show nothing for engines list
    if (actionsContainer) {
        actionsContainer.innerHTML = '';
    }
    
    if (engines.length === 0) {
        setEmpty('#databases-list', 'No database engines available', 'fa-database');
        return;
    }
    
    container.innerHTML = engines.map(engine => renderEngineCard(engine)).join('');
}

function renderEngineCard(engine) {
    const statusDot = engine.running ? 'bg-green-500' : (engine.installed ? 'bg-yellow-500' : 'bg-slate-500');
    const statusText = engine.running ? 'Running' : (engine.installed ? 'Stopped' : 'Not installed');
    
    const engineIcons = {
        mysql: { icon: 'fa-database', color: 'text-orange-400', bg: 'bg-orange-500/20' },
        postgresql: { icon: 'fa-database', color: 'text-blue-400', bg: 'bg-blue-500/20' },
        redis: { icon: 'fa-bolt', color: 'text-red-400', bg: 'bg-red-500/20' },
        mongodb: { icon: 'fa-leaf', color: 'text-green-400', bg: 'bg-green-500/20' },
    };
    const config = engineIcons[engine.name] || { icon: 'fa-database', color: 'text-slate-400', bg: 'bg-slate-500/20' };
    
    // Clickable card if engine is running
    const clickable = engine.installed && engine.running;
    const clickHandler = clickable ? `onclick="window.databasesPage.selectEngine('${engine.name}')"` : '';
    const cursorClass = clickable ? 'cursor-pointer hover:border-indigo-500/50' : '';
    
    return `
        <div class="card p-4 ${cursorClass}" ${clickHandler} data-engine="${escapeHtml(engine.name)}">
            <div class="flex items-center justify-between">
                <div class="flex items-center gap-4">
                    <div class="w-12 h-12 rounded-lg ${config.bg} flex items-center justify-center">
                        <i class="fas ${config.icon} ${config.color} text-xl"></i>
                    </div>
                    <div>
                        <h4 class="font-medium text-lg">${escapeHtml(engine.display_name)}</h4>
                        <div class="flex items-center gap-3 text-sm text-slate-400">
                            <span class="flex items-center gap-1">
                                <span class="w-2 h-2 rounded-full ${statusDot}"></span>
                                ${statusText}
                            </span>
                            ${engine.version ? `<span>v${escapeHtml(engine.version)}</span>` : ''}
                            ${engine.installed ? `<span>Port ${engine.port}</span>` : ''}
                        </div>
                    </div>
                </div>
                <div class="flex items-center gap-2" onclick="event.stopPropagation()">
                    ${engine.installed ? `
                        <button onclick="window.databasesPage.showEngineLogs('${engine.name}')" class="icon-btn" title="View Logs">
                            <i class="fas fa-terminal text-slate-400"></i>
                        </button>
                        ${engine.running ? `
                            <button onclick="window.databasesPage.restartEngine('${engine.name}')" class="icon-btn" title="Restart">
                                <i class="fas fa-sync-alt text-yellow-400"></i>
                            </button>
                            <button onclick="window.databasesPage.stopEngine('${engine.name}')" class="icon-btn" title="Stop">
                                <i class="fas fa-stop text-red-400"></i>
                            </button>
                        ` : `
                            <button onclick="window.databasesPage.startEngine('${engine.name}')" class="icon-btn" title="Start">
                                <i class="fas fa-play text-green-400"></i>
                            </button>
                        `}
                        <button onclick="window.databasesPage.showUninstallEngine('${engine.name}')" class="icon-btn danger" title="Uninstall">
                            <i class="fas fa-trash text-red-400"></i>
                        </button>
                    ` : `
                        <button onclick="window.databasesPage.installEngine('${engine.name}')" class="btn-primary px-4 py-2 rounded-lg text-sm flex items-center gap-2">
                            <i class="fas fa-download"></i>
                            Install
                        </button>
                    `}
                </div>
            </div>
            ${clickable ? `
                <div class="mt-3 pt-3 border-t border-slate-700 text-sm text-slate-400">
                    <i class="fas fa-mouse-pointer mr-2"></i>
                    Click to manage databases, users, and backups
                </div>
            ` : ''}
        </div>
    `;
}

// ==================== Engine Detail View ====================

export async function selectEngine(engineName) {
    selectedEngine = engineName;
    await loadEngineDetail(engineName);
}

async function loadEngineDetail(engineName) {
    setLoading('#databases-list');
    
    const engine = engines.find(e => e.name === engineName);
    if (!engine || !engine.running) {
        showToast('Engine not available', 'error');
        await load();
        return;
    }
    
    // Load data for this engine
    await Promise.all([
        loadDatabases(engineName),
        loadUsers(engineName),
        loadBackups(engineName),
    ]);
    
    renderEngineDetailView(engine);
}

function renderEngineDetailView(engine) {
    const container = document.getElementById('databases-list');
    const actionsContainer = document.getElementById('databases-actions');
    const breadcrumb = document.getElementById('databases-breadcrumb');
    
    if (!container) return;
    
    // Update breadcrumb with back button
    if (breadcrumb) {
        breadcrumb.innerHTML = `
            <button onclick="window.databasesPage.load()" class="text-indigo-400 hover:text-indigo-300 flex items-center gap-2">
                <i class="fas fa-arrow-left"></i>
                Engines
            </button>
            <i class="fas fa-chevron-right text-slate-600 mx-2"></i>
            <span class="text-white">${escapeHtml(engine.display_name)}</span>
        `;
    }
    
    // Render actions bar
    if (actionsContainer) {
        actionsContainer.innerHTML = `
            <div class="flex items-center gap-2 flex-wrap">
                <!-- Tabs -->
                <div class="flex gap-1 bg-slate-800 rounded-lg p-1 mr-4">
                    <button onclick="window.databasesPage.switchTab('databases')" data-tab="databases"
                        class="px-4 py-2 rounded-md text-sm font-medium transition-colors ${currentTab === 'databases' ? 'bg-indigo-500 text-white' : 'text-slate-400 hover:text-white'}">
                        <i class="fas fa-database mr-2"></i>Databases
                    </button>
                    <button onclick="window.databasesPage.switchTab('users')" data-tab="users"
                        class="px-4 py-2 rounded-md text-sm font-medium transition-colors ${currentTab === 'users' ? 'bg-indigo-500 text-white' : 'text-slate-400 hover:text-white'}">
                        <i class="fas fa-users mr-2"></i>Users
                    </button>
                    <button onclick="window.databasesPage.switchTab('backups')" data-tab="backups"
                        class="px-4 py-2 rounded-md text-sm font-medium transition-colors ${currentTab === 'backups' ? 'bg-indigo-500 text-white' : 'text-slate-400 hover:text-white'}">
                        <i class="fas fa-archive mr-2"></i>Backups
                    </button>
                </div>
                
                <!-- Action buttons based on tab -->
                <div id="tab-actions" class="flex gap-2">
                    ${renderTabActions()}
                </div>
            </div>
        `;
    }
    
    // Render content based on current tab
    renderCurrentTab();
}

function renderTabActions() {
    switch (currentTab) {
        case 'databases':
            return `
                <button onclick="window.databasesPage.showCreateDb()" class="btn-primary px-4 py-2 rounded-lg text-sm flex items-center gap-2">
                    <i class="fas fa-plus"></i>
                    Create Database
                </button>
                <button onclick="window.databasesPage.showImportSql()" class="btn-secondary px-4 py-2 rounded-lg text-sm flex items-center gap-2">
                    <i class="fas fa-file-import"></i>
                    Import SQL
                </button>
            `;
        case 'users':
            return `
                <button onclick="window.databasesPage.showCreateUser()" class="btn-primary px-4 py-2 rounded-lg text-sm flex items-center gap-2">
                    <i class="fas fa-user-plus"></i>
                    Create User
                </button>
            `;
        case 'backups':
            return `
                <button onclick="window.databasesPage.showCreateBackup()" class="btn-primary px-4 py-2 rounded-lg text-sm flex items-center gap-2">
                    <i class="fas fa-plus"></i>
                    Create Backup
                </button>
            `;
        default:
            return '';
    }
}

export function switchTab(tab) {
    currentTab = tab;
    
    // Update tab buttons
    document.querySelectorAll('[data-tab]').forEach(btn => {
        if (btn.dataset.tab === tab) {
            btn.classList.add('bg-indigo-500', 'text-white');
            btn.classList.remove('text-slate-400', 'hover:text-white');
        } else {
            btn.classList.remove('bg-indigo-500', 'text-white');
            btn.classList.add('text-slate-400', 'hover:text-white');
        }
    });
    
    // Update action buttons
    const tabActions = document.getElementById('tab-actions');
    if (tabActions) {
        tabActions.innerHTML = renderTabActions();
    }
    
    renderCurrentTab();
}

function renderCurrentTab() {
    switch (currentTab) {
        case 'databases':
            renderDatabasesTab();
            break;
        case 'users':
            renderUsersTab();
            break;
        case 'backups':
            renderBackupsTab();
            break;
    }
}

// ==================== Databases Tab ====================

async function loadDatabases(engine = null) {
    try {
        const data = await api.getDatabases(engine);
        databases = data.databases || [];
    } catch (error) {
        console.error('Failed to load databases:', error);
        databases = [];
    }
}

function renderDatabasesTab() {
    const container = document.getElementById('databases-list');
    if (!container) return;
    
    if (databases.length === 0) {
        setEmpty('#databases-list', 'No databases found', 'fa-database');
        return;
    }
    
    container.innerHTML = databases.map(db => `
        <div class="card p-4" data-database="${escapeHtml(db.name)}">
            <div class="flex items-center justify-between">
                <div class="flex items-center gap-4">
                    <div class="w-10 h-10 rounded-lg bg-indigo-500/20 flex items-center justify-center">
                        <i class="fas fa-database text-indigo-400"></i>
                    </div>
                    <div>
                        <h4 class="font-medium">${escapeHtml(db.name)}</h4>
                        <div class="flex items-center gap-3 text-sm text-slate-400">
                            ${db.size ? `<span>${escapeHtml(db.size)}</span>` : ''}
                            ${db.tables ? `<span>${db.tables} tables</span>` : ''}
                            ${db.encoding ? `<span>${escapeHtml(db.encoding)}</span>` : ''}
                        </div>
                    </div>
                </div>
                <div class="flex items-center gap-2">
                    <button onclick="window.databasesPage.showBackupDb('${escapeHtml(db.name)}')" class="icon-btn" title="Backup">
                        <i class="fas fa-download text-blue-400"></i>
                    </button>
                    <button onclick="window.databasesPage.showQueryModal('${escapeHtml(db.name)}')" class="icon-btn" title="Query">
                        <i class="fas fa-terminal text-green-400"></i>
                    </button>
                    <button onclick="window.databasesPage.showConnectionString('${escapeHtml(db.name)}')" class="icon-btn" title="Connection String">
                        <i class="fas fa-link text-purple-400"></i>
                    </button>
                    <button onclick="window.databasesPage.dropDb('${escapeHtml(db.name)}')" class="icon-btn danger" title="Drop">
                        <i class="fas fa-trash text-red-400"></i>
                    </button>
                </div>
            </div>
        </div>
    `).join('');
}

// ==================== Users Tab ====================

async function loadUsers(engine = null) {
    try {
        if (engine) {
            const data = await api.getDbUsers(engine);
            users = (data.users || []).map(u => ({ ...u, engine }));
        } else {
            users = [];
        }
    } catch (error) {
        console.error('Failed to load users:', error);
        users = [];
    }
}

function renderUsersTab() {
    const container = document.getElementById('databases-list');
    if (!container) return;
    
    if (users.length === 0) {
        setEmpty('#databases-list', 'No database users found', 'fa-users');
        return;
    }
    
    container.innerHTML = users.map(user => `
        <div class="card p-4" data-user="${escapeHtml(user.username)}">
            <div class="flex items-center justify-between">
                <div class="flex items-center gap-4">
                    <div class="w-10 h-10 rounded-lg bg-purple-500/20 flex items-center justify-center">
                        <i class="fas fa-user text-purple-400"></i>
                    </div>
                    <div>
                        <h4 class="font-medium">${escapeHtml(user.username)}</h4>
                        <div class="flex items-center gap-3 text-sm text-slate-400">
                            <span>@${escapeHtml(user.host || 'localhost')}</span>
                            ${user.databases?.length > 0 ? `<span>${user.databases.length} databases</span>` : ''}
                        </div>
                    </div>
                </div>
                <div class="flex items-center gap-2">
                    <button onclick="window.databasesPage.showGrantPrivileges('${escapeHtml(user.username)}')" class="icon-btn" title="Grant Privileges">
                        <i class="fas fa-key text-green-400"></i>
                    </button>
                    <button onclick="window.databasesPage.showRevokePrivileges('${escapeHtml(user.username)}')" class="icon-btn" title="Revoke Privileges">
                        <i class="fas fa-ban text-yellow-400"></i>
                    </button>
                    <button onclick="window.databasesPage.deleteUser('${escapeHtml(user.username)}', '${escapeHtml(user.host || 'localhost')}')" class="icon-btn danger" title="Delete">
                        <i class="fas fa-trash text-red-400"></i>
                    </button>
                </div>
            </div>
        </div>
    `).join('');
}

// ==================== Backups Tab ====================

async function loadBackups(engine = null) {
    try {
        const data = await api.getDbBackups(engine);
        backups = data.backups || [];
    } catch (error) {
        console.error('Failed to load backups:', error);
        backups = [];
    }
}

function renderBackupsTab() {
    const container = document.getElementById('databases-list');
    if (!container) return;
    
    if (backups.length === 0) {
        setEmpty('#databases-list', 'No database backups found', 'fa-archive');
        return;
    }
    
    container.innerHTML = backups.map(backup => `
        <div class="card p-4" data-backup="${escapeHtml(backup.path)}">
            <div class="flex items-center justify-between">
                <div class="flex items-center gap-4">
                    <div class="w-10 h-10 rounded-lg bg-blue-500/20 flex items-center justify-center">
                        <i class="fas fa-archive text-blue-400"></i>
                    </div>
                    <div>
                        <h4 class="font-medium">${escapeHtml(backup.database)}</h4>
                        <div class="flex items-center gap-3 text-sm text-slate-400">
                            <span>${backup.size_human}</span>
                            ${backup.compressed ? '<span class="text-green-400"><i class="fas fa-compress-alt"></i> Compressed</span>' : ''}
                            <span>${backup.created}</span>
                        </div>
                    </div>
                </div>
                <div class="flex items-center gap-2">
                    <button onclick="window.databasesPage.showRestoreBackup('${escapeHtml(backup.database)}', '${escapeHtml(backup.path)}')" 
                        class="icon-btn" title="Restore">
                        <i class="fas fa-upload text-green-400"></i>
                    </button>
                </div>
            </div>
            <div class="mt-2 text-xs text-slate-500 font-mono truncate" title="${escapeHtml(backup.path)}">
                ${escapeHtml(backup.path)}
            </div>
        </div>
    `).join('');
}

// ==================== Engine Actions ====================

export async function installEngine(engine) {
    try {
        showToast(`Installing ${engine}...`, 'info');
        await api.installDbEngine(engine);
        showToast(`${engine} installed successfully`, 'success');
        await loadEngines();
        renderEnginesView();
    } catch (error) {
        showToast(`Failed to install ${engine}: ${error.message}`, 'error');
    }
}

export function showUninstallEngine(engine) {
    document.getElementById('uninstall-engine-name').value = engine;
    document.getElementById('uninstall-engine-display').textContent = engine;
    document.getElementById('uninstall-purge').checked = false;
    showModal('uninstall-engine-modal');
}

export function hideUninstallEngine() {
    hideModal('uninstall-engine-modal');
}

export async function confirmUninstallEngine() {
    const engine = document.getElementById('uninstall-engine-name').value;
    const purge = document.getElementById('uninstall-purge').checked;
    
    const confirmMsg = purge 
        ? `Are you sure you want to uninstall ${engine} AND DELETE ALL DATA?`
        : `Are you sure you want to uninstall ${engine}?`;
    
    if (!await confirm(confirmMsg)) {
        hideUninstallEngine();
        return;
    }
    
    try {
        await api.uninstallDbEngine(engine, purge);
        showToast(`${engine} uninstalled`, 'success');
        hideUninstallEngine();
        await loadEngines();
        renderEnginesView();
    } catch (error) {
        showToast(`Failed to uninstall ${engine}: ${error.message}`, 'error');
    }
}

export async function startEngine(engine) {
    try {
        await api.startDbEngine(engine);
        showToast(`${engine} started`, 'success');
        await loadEngines();
        renderEnginesView();
    } catch (error) {
        showToast(`Failed to start ${engine}: ${error.message}`, 'error');
    }
}

export async function stopEngine(engine) {
    try {
        await api.stopDbEngine(engine);
        showToast(`${engine} stopped`, 'success');
        await loadEngines();
        if (selectedEngine === engine) {
            selectedEngine = null;
        }
        renderEnginesView();
    } catch (error) {
        showToast(`Failed to stop ${engine}: ${error.message}`, 'error');
    }
}

export async function restartEngine(engine) {
    try {
        await api.restartDbEngine(engine);
        showToast(`${engine} restarted`, 'success');
        await loadEngines();
        if (selectedEngine) {
            await loadEngineDetail(selectedEngine);
        } else {
            renderEnginesView();
        }
    } catch (error) {
        showToast(`Failed to restart ${engine}: ${error.message}`, 'error');
    }
}

export async function showEngineLogs(engine) {
    try {
        const data = await api.getDbEngineLogs(engine, 200);
        
        document.getElementById('engine-logs-title').textContent = `${engine} Logs`;
        document.getElementById('engine-logs-content').textContent = data.logs || 'No logs available';
        showModal('engine-logs-modal');
    } catch (error) {
        showToast(`Failed to get logs: ${error.message}`, 'error');
    }
}

export function hideEngineLogs() {
    hideModal('engine-logs-modal');
}

// ==================== Database Actions ====================

export function showCreateDb() {
    const engineInput = document.getElementById('create-db-engine');
    if (engineInput) engineInput.value = selectedEngine;
    showModal('create-db-modal');
}

export function hideCreateDb() {
    hideModal('create-db-modal');
    document.getElementById('create-db-form')?.reset();
}

export async function createDb(event) {
    event.preventDefault();
    
    const form = event.target;
    const formData = new FormData(form);
    
    const data = {
        name: formData.get('name'),
        engine: selectedEngine,
        owner: formData.get('owner') || null,
        encoding: formData.get('encoding') || null,
    };
    
    if (!data.name) {
        showToast('Database name is required', 'error');
        return;
    }
    
    try {
        await api.createDatabase(data);
        showToast(`Database '${data.name}' created`, 'success');
        hideCreateDb();
        await loadDatabases(selectedEngine);
        renderDatabasesTab();
    } catch (error) {
        showToast(`Failed to create database: ${error.message}`, 'error');
    }
}

export async function dropDb(name) {
    if (!await confirm(`Are you sure you want to drop database '${name}'? This action cannot be undone.`)) {
        return;
    }
    
    try {
        await api.dropDatabase(selectedEngine, name, true);
        showToast(`Database '${name}' dropped`, 'success');
        await loadDatabases(selectedEngine);
        renderDatabasesTab();
    } catch (error) {
        showToast(`Failed to drop database: ${error.message}`, 'error');
    }
}

// ==================== Import SQL ====================

export function showImportSql() {
    document.getElementById('import-sql-engine').value = selectedEngine;
    document.getElementById('import-sql-database').value = '';
    document.getElementById('import-sql-path').value = '';
    document.getElementById('import-sql-drop').checked = false;
    showModal('import-sql-modal');
}

export function hideImportSql() {
    hideModal('import-sql-modal');
}

export async function importSql(event) {
    event.preventDefault();
    
    const database = document.getElementById('import-sql-database').value;
    const sqlPath = document.getElementById('import-sql-path').value;
    const dropExisting = document.getElementById('import-sql-drop').checked;
    
    if (!database || !sqlPath) {
        showToast('Database name and SQL file path are required', 'error');
        return;
    }
    
    const confirmMsg = dropExisting 
        ? `This will DROP and recreate database '${database}'. Continue?`
        : `Import SQL into database '${database}'?`;
    
    if (!await confirm(confirmMsg)) {
        return;
    }
    
    try {
        showToast(`Importing SQL into '${database}'...`, 'info');
        await api.restoreDbBackup({
            engine: selectedEngine,
            database,
            backup_path: sqlPath,
            drop_existing: dropExisting,
        });
        showToast(`SQL imported successfully into '${database}'`, 'success');
        hideImportSql();
        await loadDatabases(selectedEngine);
        renderDatabasesTab();
    } catch (error) {
        showToast(`Failed to import SQL: ${error.message}`, 'error');
    }
}

// ==================== Backup Actions ====================

export function showBackupDb(database) {
    document.getElementById('backup-db-engine').value = selectedEngine;
    // Show input, hide select (backup from database row)
    document.getElementById('backup-db-input-container').classList.remove('hidden');
    document.getElementById('backup-db-select-container').classList.add('hidden');
    document.getElementById('backup-db-name').value = database;
    document.getElementById('backup-db-compress').checked = true;
    showModal('backup-db-modal');
}

export function hideBackupDb() {
    hideModal('backup-db-modal');
}

export async function createDbBackup(event) {
    event.preventDefault();
    
    // Check which input is visible
    const selectContainer = document.getElementById('backup-db-select-container');
    const database = selectContainer.classList.contains('hidden') 
        ? document.getElementById('backup-db-name').value
        : document.getElementById('backup-db-select').value;
    
    if (!database) {
        showToast('Please select a database', 'error');
        return;
    }
    
    const compress = document.getElementById('backup-db-compress').checked;
    
    try {
        showToast(`Creating backup of '${database}'...`, 'info');
        const result = await api.createDbBackup({ engine: selectedEngine, database, compress });
        showToast(`Backup created: ${result.size_human}`, 'success');
        hideBackupDb();
        if (currentTab === 'backups') {
            await loadBackups(selectedEngine);
            renderBackupsTab();
        }
    } catch (error) {
        showToast(`Failed to create backup: ${error.message}`, 'error');
    }
}

export function showCreateBackup() {
    // For creating backup from backups tab - need to select database
    const selectEl = document.getElementById('backup-db-select');
    selectEl.innerHTML = '<option value="">Select database...</option>' +
        databases.map(db => `<option value="${escapeHtml(db.name)}">${escapeHtml(db.name)}</option>`).join('');
    
    // Show select, hide input
    document.getElementById('backup-db-input-container').classList.add('hidden');
    document.getElementById('backup-db-select-container').classList.remove('hidden');
    
    document.getElementById('backup-db-engine').value = selectedEngine;
    document.getElementById('backup-db-compress').checked = true;
    showModal('backup-db-modal');
}

export function showRestoreBackup(database, path) {
    document.getElementById('restore-engine').value = selectedEngine;
    document.getElementById('restore-database').value = database;
    document.getElementById('restore-path').value = path;
    document.getElementById('restore-drop-existing').checked = false;
    showModal('restore-backup-modal');
}

export function hideRestoreBackup() {
    hideModal('restore-backup-modal');
}

export async function restoreBackup(event) {
    event.preventDefault();
    
    const database = document.getElementById('restore-database').value;
    const backupPath = document.getElementById('restore-path').value;
    const dropExisting = document.getElementById('restore-drop-existing').checked;
    
    const confirmMsg = dropExisting 
        ? `Are you sure you want to DROP and restore database '${database}'?`
        : `Are you sure you want to restore database '${database}'?`;
    
    if (!await confirm(confirmMsg)) {
        return;
    }
    
    try {
        showToast(`Restoring '${database}'...`, 'info');
        await api.restoreDbBackup({
            engine: selectedEngine,
            database,
            backup_path: backupPath,
            drop_existing: dropExisting,
        });
        showToast(`Database '${database}' restored successfully`, 'success');
        hideRestoreBackup();
        await loadDatabases(selectedEngine);
        renderDatabasesTab();
    } catch (error) {
        showToast(`Failed to restore: ${error.message}`, 'error');
    }
}

// ==================== Query Modal ====================

export function showQueryModal(database) {
    document.getElementById('query-modal-engine').value = selectedEngine;
    document.getElementById('query-modal-database').value = database;
    document.getElementById('query-modal-input').value = '';
    document.getElementById('query-modal-results').innerHTML = 
        '<div class="text-slate-400 text-center py-4">Execute a query to see results</div>';
    showModal('query-modal');
}

export function hideQueryModal() {
    hideModal('query-modal');
}

export async function executeModalQuery() {
    const database = document.getElementById('query-modal-database').value;
    const query = document.getElementById('query-modal-input').value.trim();
    const resultsContainer = document.getElementById('query-modal-results');
    
    if (!query) {
        showToast('Please enter a query', 'error');
        return;
    }
    
    resultsContainer.innerHTML = '<div class="text-slate-400 text-center py-4"><i class="fas fa-spinner fa-spin mr-2"></i>Executing...</div>';
    
    try {
        const result = await api.executeDbQuery({ engine: selectedEngine, database, query });
        
        if (result.success) {
            resultsContainer.innerHTML = `
                <div class="bg-slate-900 rounded-lg p-4 font-mono text-sm overflow-auto max-h-64">
                    <pre class="whitespace-pre-wrap text-green-400">${escapeHtml(result.output || 'Query executed successfully')}</pre>
                </div>
            `;
        } else {
            resultsContainer.innerHTML = `
                <div class="bg-red-900/20 border border-red-500/30 rounded-lg p-4">
                    <div class="text-red-400 font-semibold mb-2">Query failed</div>
                    <pre class="font-mono text-sm text-red-300 whitespace-pre-wrap">${escapeHtml(result.output)}</pre>
                </div>
            `;
        }
    } catch (error) {
        resultsContainer.innerHTML = `
            <div class="bg-red-900/20 border border-red-500/30 rounded-lg p-4">
                <div class="text-red-400">${error.message}</div>
            </div>
        `;
    }
}

// ==================== Connection String ====================

export function showConnectionString(database) {
    document.getElementById('conn-engine').value = selectedEngine;
    document.getElementById('conn-engine-display').value = selectedEngine;
    document.getElementById('conn-database').value = database || '';
    document.getElementById('conn-username').value = '';
    document.getElementById('conn-password').value = '';
    document.getElementById('conn-host').value = 'localhost';
    document.getElementById('conn-string-output').value = '';
    showModal('connection-string-modal');
}

export function hideConnectionString() {
    hideModal('connection-string-modal');
}

export async function generateConnectionString() {
    const database = document.getElementById('conn-database').value;
    const username = document.getElementById('conn-username').value;
    const password = document.getElementById('conn-password').value;
    const host = document.getElementById('conn-host').value || 'localhost';
    
    if (!database || !username) {
        showToast('Database and username are required', 'error');
        return;
    }
    
    try {
        const result = await api.getDbConnectionString({
            engine: selectedEngine,
            database,
            username,
            password: password || 'YOUR_PASSWORD',
            host,
        });
        
        document.getElementById('conn-string-output').value = result.connection_string;
    } catch (error) {
        showToast(`Failed to generate connection string: ${error.message}`, 'error');
    }
}

export function copyConnectionString() {
    const output = document.getElementById('conn-string-output');
    if (output.value) {
        navigator.clipboard.writeText(output.value);
        showToast('Connection string copied!', 'success');
    }
}

// ==================== User Actions ====================

export function showCreateUser() {
    const engineInput = document.getElementById('create-user-engine');
    if (engineInput) engineInput.value = selectedEngine;
    showModal('create-user-modal');
}

export function hideCreateUser() {
    hideModal('create-user-modal');
    document.getElementById('create-user-form')?.reset();
}

export async function createUser(event) {
    event.preventDefault();
    
    const form = event.target;
    const formData = new FormData(form);
    
    const data = {
        username: formData.get('username'),
        engine: selectedEngine,
        password: formData.get('password') || null,
        database: formData.get('database') || null,
        host: formData.get('host') || 'localhost',
    };
    
    if (!data.username) {
        showToast('Username is required', 'error');
        return;
    }
    
    try {
        const result = await api.createDbUser(data);
        showToast(`User '${data.username}' created. Password: ${result.password}`, 'success');
        hideCreateUser();
        await loadUsers(selectedEngine);
        renderUsersTab();
    } catch (error) {
        showToast(`Failed to create user: ${error.message}`, 'error');
    }
}

export async function deleteUser(username, host) {
    if (!await confirm(`Are you sure you want to delete user '${username}'?`)) {
        return;
    }
    
    try {
        await api.deleteDbUser(selectedEngine, username, host);
        showToast(`User '${username}' deleted`, 'success');
        await loadUsers(selectedEngine);
        renderUsersTab();
    } catch (error) {
        showToast(`Failed to delete user: ${error.message}`, 'error');
    }
}

export function showGrantPrivileges(username) {
    document.getElementById('grant-engine').value = selectedEngine;
    document.getElementById('grant-username').value = username;
    document.getElementById('grant-database').value = '';
    document.getElementById('grant-host').value = 'localhost';
    
    // Populate database dropdown
    const dbSelect = document.getElementById('grant-database');
    if (dbSelect && dbSelect.tagName === 'SELECT') {
        dbSelect.innerHTML = '<option value="">Select database...</option>' +
            databases.map(db => `<option value="${db.name}">${db.name}</option>`).join('');
    }
    
    showModal('grant-privileges-modal');
}

export function hideGrantPrivileges() {
    hideModal('grant-privileges-modal');
}

export async function grantPrivileges(event) {
    event.preventDefault();
    
    const form = event.target;
    const formData = new FormData(form);
    
    const data = {
        engine: selectedEngine,
        username: formData.get('username'),
        database: formData.get('database'),
        host: formData.get('host') || 'localhost',
    };
    
    if (!data.database) {
        showToast('Database is required', 'error');
        return;
    }
    
    try {
        await api.grantDbPrivileges(data);
        showToast(`Privileges granted to '${data.username}' on '${data.database}'`, 'success');
        hideGrantPrivileges();
        await loadUsers(selectedEngine);
        renderUsersTab();
    } catch (error) {
        showToast(`Failed to grant privileges: ${error.message}`, 'error');
    }
}

export function showRevokePrivileges(username) {
    document.getElementById('revoke-engine').value = selectedEngine;
    document.getElementById('revoke-username').value = username;
    document.getElementById('revoke-database').value = '';
    document.getElementById('revoke-host').value = 'localhost';
    
    // Populate database dropdown
    const dbSelect = document.getElementById('revoke-database');
    if (dbSelect && dbSelect.tagName === 'SELECT') {
        dbSelect.innerHTML = '<option value="">Select database...</option>' +
            databases.map(db => `<option value="${db.name}">${db.name}</option>`).join('');
    }
    
    showModal('revoke-privileges-modal');
}

export function hideRevokePrivileges() {
    hideModal('revoke-privileges-modal');
}

export async function revokePrivileges(event) {
    event.preventDefault();
    
    const form = event.target;
    const formData = new FormData(form);
    
    const data = {
        engine: selectedEngine,
        username: formData.get('username'),
        database: formData.get('database'),
        host: formData.get('host') || 'localhost',
    };
    
    if (!data.database) {
        showToast('Database is required', 'error');
        return;
    }
    
    if (!await confirm(`Revoke all privileges on '${data.database}' from '${data.username}'?`)) {
        return;
    }
    
    try {
        await api.revokeDbPrivileges(data);
        showToast(`Privileges revoked from '${data.username}' on '${data.database}'`, 'success');
        hideRevokePrivileges();
        await loadUsers(selectedEngine);
        renderUsersTab();
    } catch (error) {
        showToast(`Failed to revoke privileges: ${error.message}`, 'error');
    }
}

// ==================== Helpers ====================

function escapeHtml(text) {
    if (!text) return '';
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// Export for global access
export default {
    load,
    refresh,
    selectEngine,
    switchTab,
    // Engine actions
    installEngine,
    showUninstallEngine,
    hideUninstallEngine,
    confirmUninstallEngine,
    startEngine,
    stopEngine,
    restartEngine,
    showEngineLogs,
    hideEngineLogs,
    // Database actions
    showCreateDb,
    hideCreateDb,
    createDb,
    dropDb,
    // Import SQL
    showImportSql,
    hideImportSql,
    importSql,
    // Backup actions
    showBackupDb,
    hideBackupDb,
    createDbBackup,
    showCreateBackup,
    showRestoreBackup,
    hideRestoreBackup,
    restoreBackup,
    // Query
    showQueryModal,
    hideQueryModal,
    executeModalQuery,
    // Connection string
    showConnectionString,
    hideConnectionString,
    generateConnectionString,
    copyConnectionString,
    // User actions
    showCreateUser,
    hideCreateUser,
    createUser,
    deleteUser,
    showGrantPrivileges,
    hideGrantPrivileges,
    grantPrivileges,
    showRevokePrivileges,
    hideRevokePrivileges,
    revokePrivileges,
};

/**
 * WASM Web Dashboard - Main Application Entry Point
 * 
 * This is the main entry point that initializes all modules
 * and sets up the application.
 */

// Core modules
import { api } from './core/api.js';
import { ws } from './core/websocket.js';
import { router } from './core/router.js';
import { showToast } from './core/ui.js';

// New UX modules
import { globalSearch } from './core/search.js';
import { keyboardShortcuts } from './core/shortcuts.js';
import { themeManager } from './core/theme.js';
import { notificationCenter } from './core/notifications.js';
import { showConfirmDialog } from './core/dialogs.js';

// Page modules
import dashboardPage from './pages/dashboard.js';
import appsPage from './pages/apps.js';
import servicesPage from './pages/services.js';
import sitesPage from './pages/sites.js';
import certsPage from './pages/certs.js';
import monitorPage from './pages/monitor.js';
import logsPage from './pages/logs.js';
import jobsPage from './pages/jobs.js';
import backupsPage from './pages/backups.js';
import configPage from './pages/config.js';
import databasesPage from './pages/databases.js';

// Metrics component
import { updateMetrics } from './components/metrics.js';

// ============ Global Exports for HTML onclick handlers ============
// These are needed because HTML onclick attributes can't access ES6 modules

window.appsPage = appsPage;
window.servicesPage = servicesPage;
window.sitesPage = sitesPage;
window.certsPage = certsPage;
window.monitorPage = monitorPage;
window.logsPage = logsPage;
window.jobsPage = jobsPage;
window.backupsPage = backupsPage;
window.configPage = configPage;
window.databasesPage = databasesPage;

// Global functions called from HTML
window.showCreateAppModal = () => appsPage.showCreate();
window.hideCreateAppModal = () => appsPage.hideCreate();
window.createApp = (e) => appsPage.create(e);
window.reloadWebserver = () => sitesPage.reloadServer();
window.renewAllCerts = () => certsPage.renewAll();
window.showCreateCertModal = () => certsPage.showCreate();
window.hideCreateCertModal = () => certsPage.hideCreate();
window.createCert = (e) => certsPage.create(e);
window.runScan = () => monitorPage.runScan();
window.showCreateServiceModal = () => servicesPage.showCreate();
window.hideCreateServiceModal = () => servicesPage.hideCreate();
window.createService = (e) => servicesPage.create(e);
window.hideServiceLogsModal = () => servicesPage.hideLogs();
window.hideServiceConfigModal = () => servicesPage.hideConfig();
window.toggleServiceCreateMode = (mode) => servicesPage.toggleCreateMode(mode);
window.toggleServiceConfigEdit = () => servicesPage.toggleConfigEdit();
window.cancelServiceConfigEdit = () => servicesPage.cancelConfigEdit();
window.saveServiceConfig = () => servicesPage.saveConfig();
window.showCreateSiteModal = () => sitesPage.showCreate();
window.hideCreateSiteModal = () => sitesPage.hideCreate();
window.createSite = (e) => sitesPage.create(e);
window.hideSiteConfigModal = () => sitesPage.hideConfig();
window.toggleSiteCreateMode = (mode) => sitesPage.toggleCreateMode(mode);
window.toggleSiteConfigEdit = () => sitesPage.toggleConfigEdit();
window.cancelSiteConfigEdit = () => sitesPage.cancelConfigEdit();
window.saveSiteConfig = () => sitesPage.saveConfig();
window.switchLogStream = () => logsPage.switchStream();
window.clearLogs = () => logsPage.clear();
window.filterJobs = () => jobsPage.filter();
window.showJobDetails = (id) => jobsPage.showDetails(id);
window.hideJobModal = () => jobsPage.hideDetails();
window.cancelCurrentJob = () => jobsPage.cancelCurrent();
window.saveConfig = () => configPage.save();
window.resetToDefaults = () => configPage.resetToDefaults();
window.showCreateBackupModal = () => backupsPage.showCreate();
window.hideCreateBackupModal = () => backupsPage.hideCreate();
window.createBackup = (e) => backupsPage.create(e);
window.filterBackups = () => backupsPage.filter();
window.hideBackupDetailsModal = () => backupsPage.hideDetails();
window.restoreBackup = () => backupsPage.restore();
window.deleteBackup = () => backupsPage.remove();
window.refreshData = () => {
    router.refresh();
    showToast('Data refreshed', 'success');
    notificationCenter.add('Data refreshed successfully', 'success');
};
window.logout = logout;

// Enhanced confirm dialog (replacing browser confirm)
window.confirm = async (message) => {
    return await showConfirmDialog({
        title: 'Confirm Action',
        message: message,
        type: 'warning'
    });
};

// Export router for keyboard shortcuts
window.router = router;

// ============ Application Initialization ============

document.addEventListener('DOMContentLoaded', () => {
    init();
});

async function init() {
    // Verify session
    await verifySession();

    // Initialize router with page handlers
    router
        .init('#content')
        .register('dashboard', dashboardPage.load, dashboardPage.cleanup)
        .register('apps', appsPage.load)
        .register('services', servicesPage.load)
        .register('sites', sitesPage.load)
        .register('certificates', certsPage.load)
        .register('monitor', monitorPage.load, monitorPage.cleanup)
        .register('logs', logsPage.load)
        .register('jobs', jobsPage.load)
        .register('backups', backupsPage.load)
        .register('databases', databasesPage.load)
        .register('config', configPage.load)
        .loadInitialPage();

    // Start WebSocket connections
    startSystemMetrics();
    jobsPage.startWebSocket();

    // Check active jobs
    jobsPage.checkActive();
}

// ============ Session Management ============

async function verifySession() {
    try {
        await api.verifySession();
    } catch (error) {
        // Session invalid, redirect to login
        localStorage.removeItem('wasm_session');
        window.location.href = '/login';
    }
}

async function logout() {
    try {
        await api.logout();
    } catch (error) {
        // Ignore errors
    }

    localStorage.removeItem('wasm_session');
    ws.closeAll();
    window.location.href = '/login';
}

// ============ System Metrics ============

function startSystemMetrics() {
    ws.connectSystem(
        (data) => {
            if (data.type === 'metrics') {
                updateMetrics(data);
            }
        },
        (error) => {
            console.error('System WebSocket error:', error);
        }
    );
}

// ============ Exports ============

export { api, ws, router };

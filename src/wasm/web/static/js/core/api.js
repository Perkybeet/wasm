/**
 * WASM Web Dashboard - API Client Module
 * Handles all API communication with authentication.
 */

class WasmAPI {
    constructor() {
        this.baseUrl = '';
        this.sessionToken = localStorage.getItem('wasm_session');
    }

    getHeaders() {
        const headers = { 'Content-Type': 'application/json' };
        if (this.sessionToken) {
            headers['Authorization'] = `Bearer ${this.sessionToken}`;
        }
        return headers;
    }

    async handleResponse(response) {
        if (response.status === 401) {
            localStorage.removeItem('wasm_session');
            window.location.href = '/login';
            throw new Error('Session expired');
        }
        
        const data = await response.json();
        if (!response.ok) {
            throw new Error(data.detail || 'API request failed');
        }
        return data;
    }

    async get(endpoint) {
        const response = await fetch(`${this.baseUrl}${endpoint}`, {
            method: 'GET',
            headers: this.getHeaders(),
        });
        return this.handleResponse(response);
    }

    async post(endpoint, data = {}) {
        const response = await fetch(`${this.baseUrl}${endpoint}`, {
            method: 'POST',
            headers: this.getHeaders(),
            body: JSON.stringify(data),
        });
        return this.handleResponse(response);
    }

    async put(endpoint, data = {}) {
        const response = await fetch(`${this.baseUrl}${endpoint}`, {
            method: 'PUT',
            headers: this.getHeaders(),
            body: JSON.stringify(data),
        });
        return this.handleResponse(response);
    }

    async patch(endpoint, data = {}) {
        const response = await fetch(`${this.baseUrl}${endpoint}`, {
            method: 'PATCH',
            headers: this.getHeaders(),
            body: JSON.stringify(data),
        });
        return this.handleResponse(response);
    }

    async delete(endpoint) {
        const response = await fetch(`${this.baseUrl}${endpoint}`, {
            method: 'DELETE',
            headers: this.getHeaders(),
        });
        return this.handleResponse(response);
    }

    // Auth
    verifySession() { return this.get('/api/auth/verify'); }
    logout() { return this.post('/api/auth/logout'); }

    // System
    getSystemInfo() { return this.get('/api/system'); }
    getCpuInfo() { return this.get('/api/system/cpu'); }
    getMemoryInfo() { return this.get('/api/system/memory'); }
    getDisks() { return this.get('/api/system/disks'); }
    getProcesses(limit = 50, sortBy = 'cpu') {
        return this.get(`/api/system/processes?limit=${limit}&sort_by=${sortBy}`);
    }
    killProcess(pid, signal = 15) {
        return this.post(`/api/system/processes/${pid}/kill?signal=${signal}`);
    }

    // Apps
    getApps() { return this.get('/api/apps'); }
    getApp(domain) { return this.get(`/api/apps/${encodeURIComponent(domain)}`); }
    createApp(data) { return this.post('/api/apps', data); }
    restartApp(domain) { return this.post(`/api/apps/${encodeURIComponent(domain)}/restart`); }
    stopApp(domain) { return this.post(`/api/apps/${encodeURIComponent(domain)}/stop`); }
    startApp(domain) { return this.post(`/api/apps/${encodeURIComponent(domain)}/start`); }
    getAppLogs(domain, lines = 100) {
        return this.get(`/api/apps/${encodeURIComponent(domain)}/logs?lines=${lines}`);
    }
    deleteApp(domain, removeFiles = false) {
        return this.delete(`/api/apps/${encodeURIComponent(domain)}?remove_files=${removeFiles}`);
    }

    // Services
    getServices(wasmOnly = true) { return this.get(`/api/services?wasm_only=${wasmOnly}`); }
    getService(name) { return this.get(`/api/services/${encodeURIComponent(name)}`); }
    getServiceConfig(name) { return this.get(`/api/services/${encodeURIComponent(name)}/config`); }
    updateServiceConfig(name, config) { return this.put(`/api/services/${encodeURIComponent(name)}/config`, { config }); }
    createService(data) { return this.post('/api/services', data); }
    startService(name) { return this.post(`/api/services/${encodeURIComponent(name)}/start`); }
    stopService(name) { return this.post(`/api/services/${encodeURIComponent(name)}/stop`); }
    restartService(name) { return this.post(`/api/services/${encodeURIComponent(name)}/restart`); }
    enableService(name) { return this.post(`/api/services/${encodeURIComponent(name)}/enable`); }
    disableService(name) { return this.post(`/api/services/${encodeURIComponent(name)}/disable`); }
    deleteService(name) { return this.delete(`/api/services/${encodeURIComponent(name)}`); }
    getServiceLogs(name, lines = 100) {
        return this.get(`/api/services/${encodeURIComponent(name)}/logs?lines=${lines}`);
    }

    // Sites
    getSites() { return this.get('/api/sites'); }
    getSite(name) { return this.get(`/api/sites/${encodeURIComponent(name)}`); }
    getSiteConfig(name) { return this.get(`/api/sites/${encodeURIComponent(name)}/config`); }
    updateSiteConfig(name, config) { return this.put(`/api/sites/${encodeURIComponent(name)}/config`, { config }); }
    createSite(data) { return this.post('/api/sites', data); }
    enableSite(name) { return this.post(`/api/sites/${encodeURIComponent(name)}/enable`); }
    disableSite(name) { return this.post(`/api/sites/${encodeURIComponent(name)}/disable`); }
    deleteSite(name) { return this.delete(`/api/sites/${encodeURIComponent(name)}`); }
    reloadWebserver() { return this.post('/api/sites/reload'); }

    // Certificates
    getCertificates() { return this.get('/api/certs'); }
    getCertificate(domain) { return this.get(`/api/certs/${encodeURIComponent(domain)}`); }
    createCertificate(domain, data = {}) { return this.post(`/api/certs/${encodeURIComponent(domain)}`, data); }
    renewCertificate(domain) { return this.post(`/api/certs/${encodeURIComponent(domain)}/renew`); }
    revokeCertificate(domain) { return this.post(`/api/certs/${encodeURIComponent(domain)}/revoke`); }
    deleteCertificate(domain) { return this.delete(`/api/certs/${encodeURIComponent(domain)}`); }
    renewAllCertificates() { return this.post('/api/certs/renew-all'); }

    // Monitor
    getMonitorStatus() { return this.get('/api/monitor/status'); }
    getMonitorConfig() { return this.get('/api/monitor/config'); }
    runScan(dryRun = true, forceAi = false, analyzeAll = false) { 
        return this.post(`/api/monitor/scan?dry_run=${dryRun}&force_ai=${forceAi}&analyze_all=${analyzeAll}`); 
    }
    enableMonitor() { return this.post('/api/monitor/enable'); }
    disableMonitor() { return this.post('/api/monitor/disable'); }
    startMonitor() { return this.post('/api/monitor/start'); }
    stopMonitor() { return this.post('/api/monitor/stop'); }
    getProcesses(limit = 50, sortBy = 'cpu') {
        return this.get(`/api/monitor/processes?limit=${limit}&sort_by=${sortBy}`);
    }
    killProcess(pid, signal = 15) {
        return this.post(`/api/system/processes/${pid}/kill?signal=${signal}`);
    }

    // Jobs
    getJobs(limit = 50, status = null) {
        let url = `/api/jobs?limit=${limit}`;
        if (status) url += `&status=${status}`;
        return this.get(url);
    }
    getActiveJobs() { return this.get('/api/jobs/active'); }
    getJob(jobId) { return this.get(`/api/jobs/${jobId}`); }
    cancelJob(jobId) { return this.post(`/api/jobs/${jobId}/cancel`); }
    deployApp(data) { return this.post('/api/jobs/deploy', data); }
    updateApp(domain) { return this.post('/api/jobs/update', { domain }); }
    deleteAppJob(domain, removeFiles = true, removeSsl = true) {
        return this.post('/api/jobs/delete', { domain, remove_files: removeFiles, remove_ssl: removeSsl });
    }
    backupApp(domain) { return this.post('/api/jobs/backup', { domain }); }
    rollbackApp(domain, backupId = null) {
        return this.post('/api/jobs/rollback', { domain, backup_id: backupId });
    }
    createCertJob(domain, email = null) {
        return this.post('/api/jobs/cert', { domain, email });
    }

    // Configuration
    getConfig() { return this.get('/api/config'); }
    updateConfig(config) { return this.put('/api/config', { config }); }
    patchConfig(path, value) { return this.patch('/api/config', { path, value }); }
    getConfigDefaults() { return this.get('/api/config/defaults'); }
    reloadConfig() { return this.post('/api/config/reload'); }

    // Backups
    getBackups(domain = null, limit = 100) {
        let url = `/api/backups?limit=${limit}`;
        if (domain) url += `&domain=${encodeURIComponent(domain)}`;
        return this.get(url);
    }
    getBackup(backupId) { return this.get(`/api/backups/${backupId}`); }
    getBackupStorage() { return this.get('/api/backups/storage'); }
    createBackup(data) { return this.post('/api/backups', data); }
    verifyBackup(backupId) { return this.post(`/api/backups/${backupId}/verify`); }
    restoreBackup(backupId, targetDomain = null) {
        return this.post(`/api/backups/${backupId}/restore`, { target_domain: targetDomain });
    }
    deleteBackup(backupId) { return this.delete(`/api/backups/${backupId}`); }

    // Monitor extended
    installMonitor() { return this.post('/api/monitor/install'); }
    uninstallMonitor() { return this.post('/api/monitor/uninstall'); }
    testEmail() { return this.post('/api/monitor/test-email'); }
}

// Export singleton instance
export const api = new WasmAPI();
export default api;

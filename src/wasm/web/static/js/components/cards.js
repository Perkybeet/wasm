/**
 * WASM Web Dashboard - Card Components
 */

import { escapeHtml, getStatusDotClass } from '../core/ui.js';

/**
 * Render an app card
 */
export function renderAppCard(app, actions = {}) {
    const statusDot = getStatusDotClass(app.active);
    
    return `
        <div class="card p-4" data-domain="${escapeHtml(app.domain)}">
            <div class="flex items-center justify-between">
                <div class="flex items-center gap-4">
                    <div class="w-12 h-12 rounded-lg bg-indigo-500/20 flex items-center justify-center">
                        <i class="fas fa-globe text-indigo-400"></i>
                    </div>
                    <div>
                        <h4 class="font-semibold">${escapeHtml(app.domain)}</h4>
                        <div class="flex items-center gap-3 text-sm text-slate-400">
                            <span class="flex items-center gap-1">
                                <span class="w-2 h-2 rounded-full ${statusDot}"></span>
                                ${app.status || (app.active ? 'Running' : 'Stopped')}
                            </span>
                            <span>${escapeHtml(app.app_type || 'unknown')}</span>
                            ${app.port ? `<span>:${app.port}</span>` : ''}
                        </div>
                    </div>
                </div>
                <div class="flex items-center gap-2">
                    ${app.active ? `
                        <button onclick="${actions.restart}" class="icon-btn" title="Restart">
                            <i class="fas fa-sync-alt text-yellow-400"></i>
                        </button>
                        <button onclick="${actions.stop}" class="icon-btn" title="Stop">
                            <i class="fas fa-stop text-red-400"></i>
                        </button>
                    ` : `
                        <button onclick="${actions.start}" class="icon-btn" title="Start">
                            <i class="fas fa-play text-green-400"></i>
                        </button>
                    `}
                    <button onclick="${actions.update}" class="icon-btn" title="Update">
                        <i class="fas fa-cloud-download-alt text-blue-400"></i>
                    </button>
                    <button onclick="${actions.backup}" class="icon-btn" title="Backup">
                        <i class="fas fa-archive text-purple-400"></i>
                    </button>
                    <button onclick="${actions.logs}" class="icon-btn" title="Logs">
                        <i class="fas fa-terminal text-slate-400"></i>
                    </button>
                    <button onclick="${actions.delete}" class="icon-btn danger" title="Delete">
                        <i class="fas fa-trash text-red-400"></i>
                    </button>
                </div>
            </div>
        </div>
    `;
}

/**
 * Render a service card
 */
export function renderServiceCard(service, actions = {}, isWasmService = true) {
    const statusDot = getStatusDotClass(service.active);
    const iconColor = isWasmService ? 'text-green-400' : 'text-slate-400';
    const bgColor = isWasmService ? 'bg-green-500/20' : 'bg-slate-500/20';
    const badge = isWasmService ? '' : '<span class="text-xs bg-slate-700 px-2 py-0.5 rounded ml-2">System</span>';
    
    return `
        <div class="card p-4" data-service="${escapeHtml(service.name)}">
            <div class="flex items-center justify-between">
                <div class="flex items-center gap-4">
                    <div class="w-10 h-10 rounded-lg ${bgColor} flex items-center justify-center">
                        <i class="fas fa-cog ${iconColor}"></i>
                    </div>
                    <div>
                        <h4 class="font-medium flex items-center">${escapeHtml(service.name)}${badge}</h4>
                        <div class="flex items-center gap-3 text-sm text-slate-400">
                            <span class="flex items-center gap-1">
                                <span class="w-2 h-2 rounded-full ${statusDot}"></span>
                                ${service.status || (service.active ? 'Active' : 'Inactive')}
                            </span>
                            ${service.pid ? `<span>PID: ${service.pid}</span>` : ''}
                            ${service.uptime ? `<span>${escapeHtml(service.uptime)}</span>` : ''}
                        </div>
                    </div>
                </div>
                <div class="flex items-center gap-2">
                    ${service.active ? `
                        <button onclick="${actions.restart}" class="icon-btn" title="Restart">
                            <i class="fas fa-sync-alt text-yellow-400"></i>
                        </button>
                        <button onclick="${actions.stop}" class="icon-btn" title="Stop">
                            <i class="fas fa-stop text-red-400"></i>
                        </button>
                    ` : `
                        <button onclick="${actions.start}" class="icon-btn" title="Start">
                            <i class="fas fa-play text-green-400"></i>
                        </button>
                    `}
                    ${actions.config ? `
                        <button onclick="${actions.config}" class="icon-btn" title="View Config">
                            <i class="fas fa-file-code text-indigo-400"></i>
                        </button>
                    ` : ''}
                    ${actions.logs ? `
                        <button onclick="${actions.logs}" class="icon-btn" title="Logs">
                            <i class="fas fa-terminal text-slate-400"></i>
                        </button>
                    ` : ''}
                    ${actions.remove ? `
                        <button onclick="${actions.remove}" class="icon-btn danger" title="Delete">
                            <i class="fas fa-trash text-red-400"></i>
                        </button>
                    ` : ''}
                </div>
            </div>
        </div>
    `;
}

/**
 * Render a site card
 */
export function renderSiteCard(site, webserver, actions = {}) {
    return `
        <div class="card p-4" data-site="${escapeHtml(site.name)}">
            <div class="flex items-center justify-between">
                <div class="flex items-center gap-4">
                    <div class="w-10 h-10 rounded-lg bg-blue-500/20 flex items-center justify-center">
                        <i class="fas fa-globe text-blue-400"></i>
                    </div>
                    <div>
                        <h4 class="font-medium">${escapeHtml(site.name)}</h4>
                        <div class="flex items-center gap-3 text-sm text-slate-400">
                            <span>${escapeHtml(webserver)}</span>
                            <span class="flex items-center gap-1">
                                <span class="w-2 h-2 rounded-full ${site.enabled ? 'bg-green-500' : 'bg-slate-500'}"></span>
                                ${site.enabled ? 'Enabled' : 'Disabled'}
                            </span>
                            ${site.has_ssl ? '<span class="text-green-400"><i class="fas fa-lock"></i> SSL</span>' : ''}
                        </div>
                    </div>
                </div>
                <div class="flex items-center gap-2">
                    ${site.enabled ? `
                        <button onclick="${actions.disable}" class="icon-btn" title="Disable">
                            <i class="fas fa-toggle-on text-green-400"></i>
                        </button>
                    ` : `
                        <button onclick="${actions.enable}" class="icon-btn" title="Enable">
                            <i class="fas fa-toggle-off text-slate-400"></i>
                        </button>
                    `}
                    ${actions.viewConfig ? `
                        <button onclick="${actions.viewConfig}" class="icon-btn" title="View Config">
                            <i class="fas fa-file-code text-blue-400"></i>
                        </button>
                    ` : ''}
                    ${actions.remove ? `
                        <button onclick="${actions.remove}" class="icon-btn danger" title="Delete">
                            <i class="fas fa-trash text-red-400"></i>
                        </button>
                    ` : ''}
                </div>
            </div>
        </div>
    `;
}

/**
 * Render a certificate card
 */
export function renderCertCard(cert, actions = {}) {
    const daysClass = cert.days_remaining < 30 ? 'text-red-400' : 
                      cert.days_remaining < 60 ? 'text-yellow-400' : 'text-green-400';
    
    return `
        <div class="card p-4" data-cert="${escapeHtml(cert.domain)}">
            <div class="flex items-center justify-between">
                <div class="flex items-center gap-4">
                    <div class="w-10 h-10 rounded-lg bg-green-500/20 flex items-center justify-center">
                        <i class="fas fa-shield-alt text-green-400"></i>
                    </div>
                    <div>
                        <h4 class="font-medium">${escapeHtml(cert.domain)}</h4>
                        <div class="flex items-center gap-3 text-sm text-slate-400">
                            ${cert.days_remaining !== null ? `
                                <span class="${daysClass}">${cert.days_remaining} days remaining</span>
                            ` : ''}
                            ${cert.valid_until ? `<span>Expires: ${escapeHtml(cert.valid_until)}</span>` : ''}
                        </div>
                    </div>
                </div>
                <div class="flex items-center gap-2">
                    <button onclick="${actions.renew}" class="icon-btn" title="Renew">
                        <i class="fas fa-sync-alt text-blue-400"></i>
                    </button>
                    ${actions.revoke ? `
                        <button onclick="${actions.revoke}" class="icon-btn" title="Revoke">
                            <i class="fas fa-ban text-yellow-400"></i>
                        </button>
                    ` : ''}
                    ${actions.remove ? `
                        <button onclick="${actions.remove}" class="icon-btn danger" title="Delete">
                            <i class="fas fa-trash text-red-400"></i>
                        </button>
                    ` : ''}
                </div>
            </div>
        </div>
    `;
}

export default {
    renderAppCard,
    renderServiceCard,
    renderSiteCard,
    renderCertCard
};

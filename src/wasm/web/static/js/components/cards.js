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

/**
 * Database engine config
 */
const DB_ENGINE_CONFIG = {
    mysql: { icon: 'fa-database', color: 'text-orange-400', bgColor: 'bg-orange-500/20', displayName: 'MySQL' },
    postgresql: { icon: 'fa-database', color: 'text-blue-400', bgColor: 'bg-blue-500/20', displayName: 'PostgreSQL' },
    redis: { icon: 'fa-bolt', color: 'text-red-400', bgColor: 'bg-red-500/20', displayName: 'Redis' },
    mongodb: { icon: 'fa-leaf', color: 'text-green-400', bgColor: 'bg-green-500/20', displayName: 'MongoDB' },
};

/**
 * Render a database engine card
 */
export function renderDbEngineCard(engine, actions = {}) {
    const config = DB_ENGINE_CONFIG[engine.name] || { icon: 'fa-database', color: 'text-slate-400', bgColor: 'bg-slate-500/20' };
    const statusDot = engine.running ? 'bg-green-500' : (engine.installed ? 'bg-yellow-500' : 'bg-slate-500');
    const statusText = engine.running ? 'Running' : (engine.installed ? 'Stopped' : 'Not installed');
    
    return `
        <div class="card p-4" data-engine="${escapeHtml(engine.name)}">
            <div class="flex items-center justify-between">
                <div class="flex items-center gap-4">
                    <div class="w-10 h-10 rounded-lg ${config.bgColor} flex items-center justify-center cursor-pointer hover:opacity-80" 
                        onclick="${actions.status || ''}" title="View status">
                        <i class="fas ${config.icon} ${config.color}"></i>
                    </div>
                    <div>
                        <h4 class="font-medium">${escapeHtml(engine.display_name)}</h4>
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
                <div class="flex items-center gap-2">
                    ${engine.installed ? `
                        ${actions.status ? `
                            <button onclick="${actions.status}" class="icon-btn" title="Status">
                                <i class="fas fa-info-circle text-blue-400"></i>
                            </button>
                        ` : ''}
                        ${engine.running ? `
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
                        <button onclick="${actions.uninstall}" class="icon-btn danger" title="Uninstall">
                            <i class="fas fa-trash text-red-400"></i>
                        </button>
                    ` : `
                        <button onclick="${actions.install}" class="btn-primary px-3 py-1.5 rounded-lg text-sm flex items-center gap-2">
                            <i class="fas fa-download"></i>
                            Install
                        </button>
                    `}
                </div>
            </div>
        </div>
    `;
}

/**
 * Render a database card
 */
export function renderDatabaseCard(db, actions = {}) {
    const config = DB_ENGINE_CONFIG[db.engine] || { icon: 'fa-database', color: 'text-slate-400', bgColor: 'bg-slate-500/20' };
    
    return `
        <div class="card p-4" data-database="${escapeHtml(db.name)}" data-engine="${escapeHtml(db.engine)}">
            <div class="flex items-center justify-between">
                <div class="flex items-center gap-4">
                    <div class="w-10 h-10 rounded-lg ${config.bgColor} flex items-center justify-center cursor-pointer hover:opacity-80" 
                        onclick="${actions.info || ''}" title="View info">
                        <i class="fas ${config.icon} ${config.color}"></i>
                    </div>
                    <div>
                        <h4 class="font-medium">${escapeHtml(db.name)}</h4>
                        <div class="flex items-center gap-3 text-sm text-slate-400">
                            <span>${escapeHtml(db.engine)}</span>
                            ${db.size ? `<span>${escapeHtml(db.size)}</span>` : ''}
                            ${db.tables ? `<span>${db.tables} tables</span>` : ''}
                            ${db.owner ? `<span>Owner: ${escapeHtml(db.owner)}</span>` : ''}
                        </div>
                    </div>
                </div>
                <div class="flex items-center gap-2">
                    ${actions.info ? `
                        <button onclick="${actions.info}" class="icon-btn" title="Info">
                            <i class="fas fa-info-circle text-slate-400"></i>
                        </button>
                    ` : ''}
                    ${actions.backup ? `
                        <button onclick="${actions.backup}" class="icon-btn" title="Backup">
                            <i class="fas fa-download text-blue-400"></i>
                        </button>
                    ` : ''}
                    ${actions.query ? `
                        <button onclick="${actions.query}" class="icon-btn" title="Query">
                            <i class="fas fa-terminal text-green-400"></i>
                        </button>
                    ` : ''}
                    ${actions.drop ? `
                        <button onclick="${actions.drop}" class="icon-btn danger" title="Drop">
                            <i class="fas fa-trash text-red-400"></i>
                        </button>
                    ` : ''}
                </div>
            </div>
        </div>
    `;
}

/**
 * Render a database user card
 */
export function renderDbUserCard(user, actions = {}) {
    const config = DB_ENGINE_CONFIG[user.engine] || { icon: 'fa-database', color: 'text-slate-400', bgColor: 'bg-slate-500/20' };
    
    return `
        <div class="card p-4" data-user="${escapeHtml(user.username)}" data-engine="${escapeHtml(user.engine)}">
            <div class="flex items-center justify-between">
                <div class="flex items-center gap-4">
                    <div class="w-10 h-10 rounded-lg bg-purple-500/20 flex items-center justify-center">
                        <i class="fas fa-user text-purple-400"></i>
                    </div>
                    <div>
                        <h4 class="font-medium">${escapeHtml(user.username)}</h4>
                        <div class="flex items-center gap-3 text-sm text-slate-400">
                            <span class="flex items-center gap-1">
                                <i class="fas ${config.icon} ${config.color} text-xs"></i>
                                ${escapeHtml(user.engine)}
                            </span>
                            <span>@${escapeHtml(user.host || 'localhost')}</span>
                            ${user.databases?.length > 0 ? `<span>${user.databases.length} databases</span>` : ''}
                        </div>
                    </div>
                </div>
                <div class="flex items-center gap-2">
                    ${actions.grant ? `
                        <button onclick="${actions.grant}" class="icon-btn" title="Grant Privileges">
                            <i class="fas fa-key text-green-400"></i>
                        </button>
                    ` : ''}
                    ${actions.revoke ? `
                        <button onclick="${actions.revoke}" class="icon-btn" title="Revoke Privileges">
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
    renderCertCard,
    renderDbEngineCard,
    renderDatabaseCard,
    renderDbUserCard
};

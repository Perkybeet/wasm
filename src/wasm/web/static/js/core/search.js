/**
 * WASM Web Dashboard - Global Search Module
 * Provides quick search functionality across apps, services, sites, and certificates.
 */

import { api } from './api.js';
import { escapeHtml } from './ui.js';
import { router } from './router.js';

class GlobalSearch {
    constructor() {
        this.isOpen = false;
        this.results = [];
        this.selectedIndex = 0;
        this.cache = {
            apps: [],
            services: [],
            sites: [],
            certs: []
        };
        this.lastCacheTime = 0;
        this.cacheTimeout = 30000; // 30 seconds
        
        this.init();
    }

    init() {
        this.createSearchModal();
        this.setupKeyboardShortcuts();
    }

    createSearchModal() {
        const modal = document.createElement('div');
        modal.id = 'global-search-modal';
        modal.className = 'fixed inset-0 z-[100] hidden';
        modal.innerHTML = `
            <div class="absolute inset-0 bg-black/60 backdrop-blur-sm" onclick="window.globalSearch.close()"></div>
            <div class="relative max-w-2xl mx-auto mt-20 mx-4">
                <div class="bg-slate-800 rounded-xl shadow-2xl border border-slate-700 overflow-hidden">
                    <!-- Search Input -->
                    <div class="flex items-center gap-3 px-4 py-3 border-b border-slate-700">
                        <i class="fas fa-search text-slate-400"></i>
                        <input 
                            type="text" 
                            id="global-search-input"
                            class="flex-1 bg-transparent text-white placeholder-slate-400 focus:outline-none text-lg"
                            placeholder="Search apps, services, sites..."
                            autocomplete="off"
                        >
                        <kbd class="hidden sm:inline-block px-2 py-1 text-xs bg-slate-700 rounded text-slate-400">ESC</kbd>
                    </div>
                    
                    <!-- Results -->
                    <div id="global-search-results" class="max-h-96 overflow-y-auto">
                        <div class="p-4 text-center text-slate-400">
                            <p>Type to search...</p>
                            <p class="text-sm mt-2">
                                <kbd class="px-1.5 py-0.5 bg-slate-700 rounded text-xs">↑</kbd>
                                <kbd class="px-1.5 py-0.5 bg-slate-700 rounded text-xs">↓</kbd>
                                to navigate,
                                <kbd class="px-1.5 py-0.5 bg-slate-700 rounded text-xs">Enter</kbd>
                                to select
                            </p>
                        </div>
                    </div>
                    
                    <!-- Footer -->
                    <div class="px-4 py-2 border-t border-slate-700 text-xs text-slate-500 flex justify-between">
                        <span>
                            <i class="fas fa-cube mr-1"></i> Apps
                            <i class="fas fa-cogs ml-3 mr-1"></i> Services
                            <i class="fas fa-globe ml-3 mr-1"></i> Sites
                            <i class="fas fa-shield-alt ml-3 mr-1"></i> Certs
                        </span>
                        <span>
                            <kbd class="px-1.5 py-0.5 bg-slate-700 rounded">Ctrl+K</kbd> to open
                        </span>
                    </div>
                </div>
            </div>
        `;
        document.body.appendChild(modal);

        // Setup input listener
        const input = document.getElementById('global-search-input');
        input.addEventListener('input', (e) => this.search(e.target.value));
        input.addEventListener('keydown', (e) => this.handleKeydown(e));
    }

    setupKeyboardShortcuts() {
        document.addEventListener('keydown', (e) => {
            // Ctrl+K or Cmd+K to open search
            if ((e.ctrlKey || e.metaKey) && e.key === 'k') {
                e.preventDefault();
                this.open();
            }
            
            // ESC to close
            if (e.key === 'Escape' && this.isOpen) {
                this.close();
            }
        });
    }

    open() {
        const modal = document.getElementById('global-search-modal');
        const input = document.getElementById('global-search-input');
        
        modal.classList.remove('hidden');
        this.isOpen = true;
        
        // Focus and select input
        setTimeout(() => {
            input.focus();
            input.select();
        }, 50);
        
        // Refresh cache if needed
        this.refreshCache();
    }

    close() {
        const modal = document.getElementById('global-search-modal');
        modal.classList.add('hidden');
        this.isOpen = false;
        this.selectedIndex = 0;
        
        // Clear input
        const input = document.getElementById('global-search-input');
        if (input) input.value = '';
    }

    async refreshCache() {
        const now = Date.now();
        if (now - this.lastCacheTime < this.cacheTimeout) {
            return; // Cache is still fresh
        }

        try {
            const [apps, services, sites, certs] = await Promise.all([
                api.getApps().catch(() => ({ apps: [] })),
                api.getServices(true).catch(() => ({ services: [] })),
                api.getSites().catch(() => ({ sites: [] })),
                api.getCertificates().catch(() => ({ certificates: [] }))
            ]);

            this.cache = {
                apps: apps.apps || [],
                services: services.services || [],
                sites: sites.sites || [],
                certs: certs.certificates || []
            };
            this.lastCacheTime = now;
        } catch (error) {
            console.error('Failed to refresh search cache:', error);
        }
    }

    search(query) {
        const resultsContainer = document.getElementById('global-search-results');
        
        if (!query || query.length < 1) {
            resultsContainer.innerHTML = `
                <div class="p-4 text-center text-slate-400">
                    <p>Type to search...</p>
                    <p class="text-sm mt-2">
                        <kbd class="px-1.5 py-0.5 bg-slate-700 rounded text-xs">↑</kbd>
                        <kbd class="px-1.5 py-0.5 bg-slate-700 rounded text-xs">↓</kbd>
                        to navigate,
                        <kbd class="px-1.5 py-0.5 bg-slate-700 rounded text-xs">Enter</kbd>
                        to select
                    </p>
                </div>
            `;
            this.results = [];
            return;
        }

        const q = query.toLowerCase();
        this.results = [];

        // Search apps
        this.cache.apps.forEach(app => {
            if (app.domain.toLowerCase().includes(q) || 
                (app.app_type && app.app_type.toLowerCase().includes(q))) {
                this.results.push({
                    type: 'app',
                    icon: 'fa-cube',
                    iconColor: 'text-indigo-400',
                    title: app.domain,
                    subtitle: app.app_type || 'Application',
                    status: app.active,
                    action: () => {
                        router.navigate('apps');
                        this.close();
                    }
                });
            }
        });

        // Search services
        this.cache.services.forEach(svc => {
            if (svc.name.toLowerCase().includes(q)) {
                this.results.push({
                    type: 'service',
                    icon: 'fa-cogs',
                    iconColor: 'text-green-400',
                    title: svc.name,
                    subtitle: svc.description || 'Service',
                    status: svc.is_active,
                    action: () => {
                        router.navigate('services');
                        this.close();
                    }
                });
            }
        });

        // Search sites
        this.cache.sites.forEach(site => {
            if (site.name.toLowerCase().includes(q)) {
                this.results.push({
                    type: 'site',
                    icon: 'fa-globe',
                    iconColor: 'text-blue-400',
                    title: site.name,
                    subtitle: `${site.webserver} site`,
                    status: site.enabled,
                    action: () => {
                        router.navigate('sites');
                        this.close();
                    }
                });
            }
        });

        // Search certs
        this.cache.certs.forEach(cert => {
            const domains = cert.domains?.join(', ') || cert.domain || '';
            if (domains.toLowerCase().includes(q)) {
                this.results.push({
                    type: 'cert',
                    icon: 'fa-shield-alt',
                    iconColor: 'text-yellow-400',
                    title: domains,
                    subtitle: cert.valid ? 'Valid Certificate' : 'Certificate',
                    status: cert.valid,
                    action: () => {
                        router.navigate('certificates');
                        this.close();
                    }
                });
            }
        });

        // Add navigation shortcuts
        const pages = [
            { name: 'Dashboard', page: 'dashboard', icon: 'fa-home' },
            { name: 'Applications', page: 'apps', icon: 'fa-cube' },
            { name: 'Services', page: 'services', icon: 'fa-cogs' },
            { name: 'Sites', page: 'sites', icon: 'fa-globe' },
            { name: 'Certificates', page: 'certificates', icon: 'fa-shield-alt' },
            { name: 'Monitor', page: 'monitor', icon: 'fa-heartbeat' },
            { name: 'Logs', page: 'logs', icon: 'fa-terminal' },
            { name: 'Jobs', page: 'jobs', icon: 'fa-tasks' },
            { name: 'Backups', page: 'backups', icon: 'fa-archive' },
            { name: 'Databases', page: 'databases', icon: 'fa-database' },
            { name: 'Settings', page: 'config', icon: 'fa-sliders-h' }
        ];

        pages.forEach(p => {
            if (p.name.toLowerCase().includes(q)) {
                this.results.push({
                    type: 'page',
                    icon: p.icon,
                    iconColor: 'text-slate-400',
                    title: p.name,
                    subtitle: 'Navigate to page',
                    action: () => {
                        router.navigate(p.page);
                        this.close();
                    }
                });
            }
        });

        this.selectedIndex = 0;
        this.renderResults();
    }

    renderResults() {
        const container = document.getElementById('global-search-results');
        
        if (this.results.length === 0) {
            container.innerHTML = `
                <div class="p-8 text-center text-slate-400">
                    <i class="fas fa-search text-3xl mb-3 opacity-50"></i>
                    <p>No results found</p>
                </div>
            `;
            return;
        }

        container.innerHTML = this.results.map((result, index) => `
            <div class="search-result px-4 py-3 flex items-center gap-3 cursor-pointer transition-colors
                ${index === this.selectedIndex ? 'bg-indigo-500/20' : 'hover:bg-slate-700/50'}"
                onclick="window.globalSearch.selectResult(${index})"
                onmouseenter="window.globalSearch.setSelected(${index})">
                <div class="w-8 h-8 rounded-lg bg-slate-700 flex items-center justify-center">
                    <i class="fas ${result.icon} ${result.iconColor}"></i>
                </div>
                <div class="flex-1 min-w-0">
                    <div class="font-medium text-white truncate">${escapeHtml(result.title)}</div>
                    <div class="text-sm text-slate-400 truncate">${escapeHtml(result.subtitle)}</div>
                </div>
                ${result.status !== undefined ? `
                    <span class="w-2 h-2 rounded-full ${result.status ? 'bg-green-500' : 'bg-red-500'}"></span>
                ` : ''}
                <i class="fas fa-arrow-right text-slate-500 text-sm"></i>
            </div>
        `).join('');
    }

    handleKeydown(e) {
        if (e.key === 'ArrowDown') {
            e.preventDefault();
            this.selectedIndex = Math.min(this.selectedIndex + 1, this.results.length - 1);
            this.renderResults();
            this.scrollToSelected();
        } else if (e.key === 'ArrowUp') {
            e.preventDefault();
            this.selectedIndex = Math.max(this.selectedIndex - 1, 0);
            this.renderResults();
            this.scrollToSelected();
        } else if (e.key === 'Enter') {
            e.preventDefault();
            this.selectResult(this.selectedIndex);
        }
    }

    scrollToSelected() {
        const container = document.getElementById('global-search-results');
        const selected = container.querySelector('.bg-indigo-500\\/20');
        if (selected) {
            selected.scrollIntoView({ block: 'nearest' });
        }
    }

    setSelected(index) {
        this.selectedIndex = index;
        this.renderResults();
    }

    selectResult(index) {
        const result = this.results[index];
        if (result && result.action) {
            result.action();
        }
    }
}

// Export singleton
export const globalSearch = new GlobalSearch();
window.globalSearch = globalSearch;
export default globalSearch;

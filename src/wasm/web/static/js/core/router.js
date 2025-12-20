/**
 * WASM Web Dashboard - Router Module
 * Handles SPA navigation and page management.
 */

class Router {
    constructor() {
        this.routes = {};
        this.cleanupHandlers = {};
        this.currentPage = null;
        this.pageContainer = null;
        this.navItems = [];
        this.onPageChange = null;
    }

    /**
     * Initialize the router
     */
    init(containerSelector = '#content') {
        this.pageContainer = document.querySelector(containerSelector);
        this.navItems = document.querySelectorAll('.nav-item[data-page]');
        
        // Setup navigation click handlers
        this.navItems.forEach(item => {
            item.addEventListener('click', (e) => {
                e.preventDefault();
                const page = item.getAttribute('data-page');
                if (page) this.navigate(page);
            });
        });

        // Handle browser back/forward
        window.addEventListener('popstate', () => {
            const hash = window.location.hash.slice(1) || 'dashboard';
            this.navigate(hash, false);
        });

        return this;
    }

    /**
     * Register a page handler with optional cleanup
     */
    register(pageName, handler, cleanup = null) {
        this.routes[pageName] = handler;
        if (cleanup) {
            this.cleanupHandlers[pageName] = cleanup;
        }
        return this;
    }

    /**
     * Navigate to a page
     */
    navigate(pageName, updateHistory = true) {
        // Run cleanup for current page before switching
        if (this.currentPage && this.cleanupHandlers[this.currentPage]) {
            try {
                this.cleanupHandlers[this.currentPage]();
            } catch (e) {
                console.error(`Cleanup error for ${this.currentPage}:`, e);
            }
        }

        // Hide all pages
        document.querySelectorAll('.page').forEach(p => p.classList.add('hidden'));

        // Show target page
        const targetPage = document.getElementById(`page-${pageName}`);
        if (!targetPage) {
            console.warn(`Page not found: ${pageName}`);
            return;
        }

        targetPage.classList.remove('hidden');
        this.currentPage = pageName;

        // Update nav active state
        this.navItems.forEach(item => {
            item.classList.toggle('active', item.getAttribute('data-page') === pageName);
        });

        // Update page title in header
        const titleEl = document.getElementById('page-title');
        if (titleEl) {
            titleEl.textContent = this.formatPageTitle(pageName);
        }

        // Update breadcrumb
        const breadcrumbEl = document.getElementById('breadcrumb-current');
        if (breadcrumbEl) {
            breadcrumbEl.textContent = this.formatPageTitle(pageName);
        }

        // Update URL
        if (updateHistory) {
            window.location.hash = pageName;
        }

        // Execute page handler
        const handler = this.routes[pageName];
        if (handler) {
            handler();
        }

        // Emit page change event
        this.onPageChange?.(pageName);
    }

    /**
     * Format page name for display
     */
    formatPageTitle(pageName) {
        const titles = {
            dashboard: 'Dashboard',
            apps: 'Applications',
            services: 'Services',
            sites: 'Sites',
            certificates: 'Certificates',
            monitor: 'Monitor',
            logs: 'Logs',
            jobs: 'Jobs',
            backups: 'Backups',
            config: 'Settings'
        };
        return titles[pageName] || pageName.charAt(0).toUpperCase() + pageName.slice(1);
    }

    /**
     * Get current page
     */
    getCurrentPage() {
        return this.currentPage;
    }

    /**
     * Load initial page from URL hash
     */
    loadInitialPage() {
        const hash = window.location.hash.slice(1) || 'dashboard';
        this.navigate(hash, false);
        return this;
    }

    /**
     * Refresh current page
     */
    refresh() {
        if (this.currentPage) {
            const handler = this.routes[this.currentPage];
            if (handler) handler();
        }
    }
}

// Export singleton instance
export const router = new Router();
export default router;

/**
 * WASM Web Dashboard - Theme Manager
 * Provides dark/light theme toggle functionality.
 */

class ThemeManager {
    constructor() {
        this.currentTheme = 'dark';
        this.storageKey = 'wasm_theme';
        
        this.init();
    }

    init() {
        // Load saved theme
        const savedTheme = localStorage.getItem(this.storageKey);
        if (savedTheme) {
            this.currentTheme = savedTheme;
        } else {
            // Check system preference
            if (window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches) {
                this.currentTheme = 'light';
            }
        }

        this.applyTheme();
        this.createToggleButton();
        
        // Listen for system preference changes
        window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', (e) => {
            if (!localStorage.getItem(this.storageKey)) {
                this.currentTheme = e.matches ? 'dark' : 'light';
                this.applyTheme();
            }
        });
    }

    createToggleButton() {
        // Find the header to add the toggle
        const header = document.querySelector('header');
        if (!header) {
            // Retry after DOM is ready
            setTimeout(() => this.createToggleButton(), 100);
            return;
        }

        const controlsDiv = header.querySelector('.flex.items-center.gap-4');
        if (!controlsDiv) return;

        // Check if toggle already exists
        if (document.getElementById('theme-toggle')) return;

        // Create toggle button
        const toggle = document.createElement('button');
        toggle.id = 'theme-toggle';
        toggle.className = 'p-2 rounded-lg hover:bg-slate-700 transition-colors';
        toggle.title = 'Toggle theme';
        toggle.innerHTML = this.getToggleIcon();
        toggle.onclick = () => this.toggle();

        // Insert before the refresh button
        const refreshBtn = controlsDiv.querySelector('button[onclick="refreshData()"]');
        if (refreshBtn) {
            controlsDiv.insertBefore(toggle, refreshBtn);
        } else {
            controlsDiv.appendChild(toggle);
        }
    }

    getToggleIcon() {
        return this.currentTheme === 'dark' 
            ? '<i class="fas fa-sun text-yellow-400"></i>'
            : '<i class="fas fa-moon text-indigo-400"></i>';
    }

    toggle() {
        this.currentTheme = this.currentTheme === 'dark' ? 'light' : 'dark';
        localStorage.setItem(this.storageKey, this.currentTheme);
        this.applyTheme();
        
        // Update toggle icon
        const toggle = document.getElementById('theme-toggle');
        if (toggle) toggle.innerHTML = this.getToggleIcon();
    }

    applyTheme() {
        const root = document.documentElement;
        const body = document.body;

        if (this.currentTheme === 'light') {
            root.classList.add('light-theme');
            body.classList.add('light-theme');
            this.applyLightTheme();
        } else {
            root.classList.remove('light-theme');
            body.classList.remove('light-theme');
            this.applyDarkTheme();
        }
    }

    applyDarkTheme() {
        document.documentElement.style.setProperty('--bg-primary', '#0f172a');
        document.documentElement.style.setProperty('--bg-secondary', '#1e293b');
        document.documentElement.style.setProperty('--bg-tertiary', '#334155');
        document.documentElement.style.setProperty('--text-primary', '#f8fafc');
        document.documentElement.style.setProperty('--text-secondary', '#94a3b8');
    }

    applyLightTheme() {
        document.documentElement.style.setProperty('--bg-primary', '#f1f5f9');
        document.documentElement.style.setProperty('--bg-secondary', '#ffffff');
        document.documentElement.style.setProperty('--bg-tertiary', '#e2e8f0');
        document.documentElement.style.setProperty('--text-primary', '#0f172a');
        document.documentElement.style.setProperty('--text-secondary', '#475569');
    }

    isDark() {
        return this.currentTheme === 'dark';
    }

    isLight() {
        return this.currentTheme === 'light';
    }
}

// Export singleton
export const themeManager = new ThemeManager();
window.themeManager = themeManager;
export default themeManager;

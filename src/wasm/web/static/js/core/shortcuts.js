/**
 * WASM Web Dashboard - Keyboard Shortcuts Module
 * Provides keyboard navigation and shortcuts for common actions.
 */

class KeyboardShortcuts {
    constructor() {
        this.shortcuts = new Map();
        this.isEnabled = true;
        this.helpModalId = 'keyboard-shortcuts-modal';
        
        this.init();
    }

    init() {
        this.createHelpModal();
        this.registerDefaultShortcuts();
        this.setupListener();
    }

    createHelpModal() {
        const modal = document.createElement('div');
        modal.id = this.helpModalId;
        modal.className = 'fixed inset-0 z-[100] hidden';
        modal.innerHTML = `
            <div class="absolute inset-0 bg-black/60 backdrop-blur-sm" onclick="window.keyboardShortcuts.hideHelp()"></div>
            <div class="relative max-w-lg mx-auto mt-20 mx-4">
                <div class="bg-slate-800 rounded-xl shadow-2xl border border-slate-700 overflow-hidden">
                    <div class="px-6 py-4 border-b border-slate-700 flex items-center justify-between">
                        <h3 class="text-lg font-semibold text-white flex items-center gap-2">
                            <i class="fas fa-keyboard text-indigo-400"></i>
                            Keyboard Shortcuts
                        </h3>
                        <button onclick="window.keyboardShortcuts.hideHelp()" class="text-slate-400 hover:text-white">
                            <i class="fas fa-times"></i>
                        </button>
                    </div>
                    <div class="p-4 max-h-96 overflow-y-auto">
                        <div class="space-y-4" id="shortcuts-list">
                            <!-- Shortcuts will be rendered here -->
                        </div>
                    </div>
                </div>
            </div>
        `;
        document.body.appendChild(modal);
    }

    registerDefaultShortcuts() {
        // Navigation shortcuts
        this.register('g d', 'Go to Dashboard', () => this.navigate('dashboard'), 'Navigation');
        this.register('g a', 'Go to Applications', () => this.navigate('apps'), 'Navigation');
        this.register('g s', 'Go to Services', () => this.navigate('services'), 'Navigation');
        this.register('g i', 'Go to Sites', () => this.navigate('sites'), 'Navigation');
        this.register('g c', 'Go to Certificates', () => this.navigate('certificates'), 'Navigation');
        this.register('g m', 'Go to Monitor', () => this.navigate('monitor'), 'Navigation');
        this.register('g l', 'Go to Logs', () => this.navigate('logs'), 'Navigation');
        this.register('g j', 'Go to Jobs', () => this.navigate('jobs'), 'Navigation');
        this.register('g b', 'Go to Backups', () => this.navigate('backups'), 'Navigation');
        this.register('g ,', 'Go to Settings', () => this.navigate('config'), 'Navigation');

        // Actions
        this.register('n a', 'New Application', () => window.showCreateAppModal?.(), 'Actions');
        this.register('n s', 'New Service', () => window.showCreateServiceModal?.(), 'Actions');
        this.register('n i', 'New Site', () => window.showCreateSiteModal?.(), 'Actions');
        this.register('n c', 'New Certificate', () => window.showCreateCertModal?.(), 'Actions');
        this.register('n b', 'New Backup', () => window.showCreateBackupModal?.(), 'Actions');

        // Global
        this.register('/', 'Focus Search', () => window.globalSearch?.open(), 'Global');
        this.register('?', 'Show Shortcuts', () => this.showHelp(), 'Global');
        this.register('r', 'Refresh Data', () => window.refreshData?.(), 'Global');
        this.register('Escape', 'Close Modal', () => this.closeActiveModal(), 'Global');
    }

    register(keys, description, callback, category = 'General') {
        this.shortcuts.set(keys, { keys, description, callback, category });
    }

    setupListener() {
        let keySequence = '';
        let keySequenceTimeout = null;

        document.addEventListener('keydown', (e) => {
            // Skip if disabled or in input/textarea
            if (!this.isEnabled) return;
            if (this.isInputFocused(e.target)) return;
            
            // Handle special keys
            if (e.key === 'Escape') {
                const shortcut = this.shortcuts.get('Escape');
                if (shortcut) shortcut.callback();
                return;
            }

            // Build key sequence
            clearTimeout(keySequenceTimeout);
            
            const key = e.key === ' ' ? 'Space' : e.key;
            keySequence += (keySequence ? ' ' : '') + key;

            // Check for exact match
            if (this.shortcuts.has(keySequence)) {
                e.preventDefault();
                this.shortcuts.get(keySequence).callback();
                keySequence = '';
                return;
            }

            // Check if this could be a prefix for a longer shortcut
            let isPrefix = false;
            for (const [shortcutKeys] of this.shortcuts) {
                if (shortcutKeys.startsWith(keySequence + ' ')) {
                    isPrefix = true;
                    break;
                }
            }

            if (!isPrefix) {
                keySequence = '';
            }

            // Reset sequence after timeout
            keySequenceTimeout = setTimeout(() => {
                keySequence = '';
            }, 1000);
        });
    }

    isInputFocused(target) {
        if (!target) return false;
        const tagName = target.tagName.toLowerCase();
        return tagName === 'input' || tagName === 'textarea' || tagName === 'select' || target.isContentEditable;
    }

    navigate(page) {
        if (window.router) {
            window.router.navigate(page);
        } else {
            window.location.hash = page;
        }
    }

    closeActiveModal() {
        // Try to close any open modals
        const modals = document.querySelectorAll('[id$="-modal"]:not(.hidden)');
        modals.forEach(modal => {
            if (modal.id !== 'keyboard-shortcuts-modal') {
                modal.classList.add('hidden');
            }
        });
        this.hideHelp();
        window.globalSearch?.close();
    }

    showHelp() {
        const modal = document.getElementById(this.helpModalId);
        const list = document.getElementById('shortcuts-list');
        
        if (!modal || !list) return;

        // Group shortcuts by category
        const categories = new Map();
        for (const [, shortcut] of this.shortcuts) {
            if (!categories.has(shortcut.category)) {
                categories.set(shortcut.category, []);
            }
            categories.get(shortcut.category).push(shortcut);
        }

        // Render shortcuts
        list.innerHTML = Array.from(categories.entries()).map(([category, shortcuts]) => `
            <div>
                <h4 class="text-sm font-medium text-slate-400 mb-2">${category}</h4>
                <div class="space-y-1">
                    ${shortcuts.map(s => `
                        <div class="flex items-center justify-between py-1.5 px-2 rounded hover:bg-slate-700/50">
                            <span class="text-slate-300">${s.description}</span>
                            <div class="flex items-center gap-1">
                                ${s.keys.split(' ').map(k => `
                                    <kbd class="px-2 py-0.5 bg-slate-700 rounded text-xs text-slate-300 font-mono">${k}</kbd>
                                `).join('<span class="text-slate-500 text-xs mx-0.5">then</span>')}
                            </div>
                        </div>
                    `).join('')}
                </div>
            </div>
        `).join('');

        modal.classList.remove('hidden');
    }

    hideHelp() {
        const modal = document.getElementById(this.helpModalId);
        if (modal) modal.classList.add('hidden');
    }

    enable() {
        this.isEnabled = true;
    }

    disable() {
        this.isEnabled = false;
    }
}

// Export singleton
export const keyboardShortcuts = new KeyboardShortcuts();
window.keyboardShortcuts = keyboardShortcuts;
export default keyboardShortcuts;

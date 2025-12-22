/**
 * WASM Web Dashboard - Notification Center
 * Provides notification history and management.
 */

import { escapeHtml, formatTime } from './ui.js';

class NotificationCenter {
    constructor() {
        this.notifications = [];
        this.maxNotifications = 50;
        this.isOpen = false;
        this.unreadCount = 0;
        this.storageKey = 'wasm_notifications';
        
        this.init();
    }

    init() {
        this.loadFromStorage();
        this.createUI();
    }

    createUI() {
        // Wait for header to be ready
        const header = document.querySelector('header');
        if (!header) {
            setTimeout(() => this.createUI(), 100);
            return;
        }

        // Use specific ID for header controls
        const controlsDiv = document.getElementById('header-controls');
        if (!controlsDiv || document.getElementById('notification-bell')) return;

        // Create notification bell
        const bellContainer = document.createElement('div');
        bellContainer.className = 'relative';
        bellContainer.innerHTML = `
            <button id="notification-bell" class="p-2 rounded-lg hover:bg-slate-700 transition-colors relative" onclick="window.notificationCenter.toggle()">
                <i class="fas fa-bell"></i>
                <span id="notification-badge" class="hidden absolute -top-1 -right-1 w-5 h-5 bg-red-500 text-white text-xs rounded-full flex items-center justify-center">0</span>
            </button>
        `;

        // Append at the end of controls (rightmost position)
        controlsDiv.appendChild(bellContainer);

        // Create dropdown panel
        const panel = document.createElement('div');
        panel.id = 'notification-panel';
        panel.className = 'hidden absolute right-0 top-12 w-96 bg-slate-800 rounded-xl shadow-2xl border border-slate-700 z-50';
        panel.innerHTML = `
            <div class="px-4 py-3 border-b border-slate-700 flex items-center justify-between">
                <h3 class="font-semibold text-white">Notifications</h3>
                <div class="flex items-center gap-2">
                    <button onclick="window.notificationCenter.markAllRead()" class="text-sm text-slate-400 hover:text-white" title="Mark all as read">
                        <i class="fas fa-check-double"></i>
                    </button>
                    <button onclick="window.notificationCenter.clearAll()" class="text-sm text-slate-400 hover:text-red-400" title="Clear all">
                        <i class="fas fa-trash"></i>
                    </button>
                </div>
            </div>
            <div id="notification-list" class="max-h-96 overflow-y-auto">
                <div class="p-4 text-center text-slate-400">No notifications</div>
            </div>
        `;
        bellContainer.appendChild(panel);

        // Close on outside click
        document.addEventListener('click', (e) => {
            if (!bellContainer.contains(e.target)) {
                this.close();
            }
        });

        this.updateBadge();
        this.renderList();
    }

    toggle() {
        if (this.isOpen) {
            this.close();
        } else {
            this.open();
        }
    }

    open() {
        const panel = document.getElementById('notification-panel');
        if (panel) {
            panel.classList.remove('hidden');
            this.isOpen = true;
        }
    }

    close() {
        const panel = document.getElementById('notification-panel');
        if (panel) {
            panel.classList.add('hidden');
            this.isOpen = false;
        }
    }

    add(message, type = 'info', persistent = false) {
        const notification = {
            id: Date.now().toString(),
            message,
            type,
            timestamp: new Date().toISOString(),
            read: false,
            persistent
        };

        this.notifications.unshift(notification);
        this.unreadCount++;

        // Trim old notifications
        if (this.notifications.length > this.maxNotifications) {
            this.notifications = this.notifications.slice(0, this.maxNotifications);
        }

        this.saveToStorage();
        this.updateBadge();
        this.renderList();

        return notification.id;
    }

    remove(id) {
        const index = this.notifications.findIndex(n => n.id === id);
        if (index !== -1) {
            if (!this.notifications[index].read) {
                this.unreadCount--;
            }
            this.notifications.splice(index, 1);
            this.saveToStorage();
            this.updateBadge();
            this.renderList();
        }
    }

    markRead(id) {
        const notification = this.notifications.find(n => n.id === id);
        if (notification && !notification.read) {
            notification.read = true;
            this.unreadCount--;
            this.saveToStorage();
            this.updateBadge();
            this.renderList();
        }
    }

    markAllRead() {
        this.notifications.forEach(n => n.read = true);
        this.unreadCount = 0;
        this.saveToStorage();
        this.updateBadge();
        this.renderList();
    }

    clearAll() {
        this.notifications = [];
        this.unreadCount = 0;
        this.saveToStorage();
        this.updateBadge();
        this.renderList();
    }

    updateBadge() {
        const badge = document.getElementById('notification-badge');
        if (badge) {
            if (this.unreadCount > 0) {
                badge.classList.remove('hidden');
                badge.textContent = this.unreadCount > 9 ? '9+' : this.unreadCount;
            } else {
                badge.classList.add('hidden');
            }
        }
    }

    renderList() {
        const list = document.getElementById('notification-list');
        if (!list) return;

        if (this.notifications.length === 0) {
            list.innerHTML = `
                <div class="p-8 text-center text-slate-400">
                    <i class="fas fa-bell-slash text-3xl mb-2 opacity-50"></i>
                    <p>No notifications</p>
                </div>
            `;
            return;
        }

        const icons = {
            success: 'fa-check-circle text-green-400',
            error: 'fa-exclamation-circle text-red-400',
            warning: 'fa-exclamation-triangle text-yellow-400',
            info: 'fa-info-circle text-blue-400'
        };

        list.innerHTML = this.notifications.map(n => `
            <div class="px-4 py-3 border-b border-slate-700/50 hover:bg-slate-700/30 transition-colors ${!n.read ? 'bg-indigo-500/10' : ''}" onclick="window.notificationCenter.markRead('${n.id}')">
                <div class="flex items-start gap-3">
                    <i class="fas ${icons[n.type] || icons.info} mt-0.5"></i>
                    <div class="flex-1 min-w-0">
                        <p class="text-sm text-slate-200">${escapeHtml(n.message)}</p>
                        <p class="text-xs text-slate-500 mt-1">${formatTime(n.timestamp)}</p>
                    </div>
                    <button onclick="event.stopPropagation(); window.notificationCenter.remove('${n.id}')" class="text-slate-500 hover:text-red-400 p-1">
                        <i class="fas fa-times text-xs"></i>
                    </button>
                </div>
            </div>
        `).join('');
    }

    saveToStorage() {
        try {
            localStorage.setItem(this.storageKey, JSON.stringify({
                notifications: this.notifications,
                unreadCount: this.unreadCount
            }));
        } catch (e) {
            // Storage might be full
            console.warn('Failed to save notifications:', e);
        }
    }

    loadFromStorage() {
        try {
            const data = localStorage.getItem(this.storageKey);
            if (data) {
                const parsed = JSON.parse(data);
                this.notifications = parsed.notifications || [];
                this.unreadCount = parsed.unreadCount || 0;
            }
        } catch (e) {
            console.warn('Failed to load notifications:', e);
        }
    }
}

// Export singleton
export const notificationCenter = new NotificationCenter();
window.notificationCenter = notificationCenter;
export default notificationCenter;

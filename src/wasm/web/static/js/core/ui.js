/**
 * WASM Web Dashboard - UI Utilities
 * Common UI helpers: toasts, modals, formatting.
 */

// ============ Toast Notifications ============

const toastContainer = () => document.getElementById('toast-container');

export function showToast(message, type = 'info', addToHistory = true) {
    const container = toastContainer();
    if (!container) return;

    const colors = {
        success: 'bg-green-500',
        error: 'bg-red-500',
        warning: 'bg-yellow-500',
        info: 'bg-blue-500'
    };

    const icons = {
        success: 'fa-check-circle',
        error: 'fa-exclamation-circle',
        warning: 'fa-exclamation-triangle',
        info: 'fa-info-circle'
    };

    const toast = document.createElement('div');
    toast.className = `toast ${colors[type]} text-white px-4 py-3 rounded-lg shadow-lg flex items-center gap-3 min-w-[300px]`;
    toast.innerHTML = `
        <i class="fas ${icons[type]}"></i>
        <span>${escapeHtml(message)}</span>
    `;

    container.appendChild(toast);

    // Also add to notification center if available
    if (addToHistory && window.notificationCenter) {
        window.notificationCenter.add(message, type);
    }

    setTimeout(() => {
        toast.classList.add('toast-exit');
        setTimeout(() => toast.remove(), 300);
    }, 3000);
}

// ============ Modal Helpers ============

export function showModal(modalId) {
    const modal = document.getElementById(modalId);
    if (modal) {
        modal.classList.remove('hidden');
        document.body.style.overflow = 'hidden';
    }
}

export function hideModal(modalId) {
    const modal = document.getElementById(modalId);
    if (modal) {
        modal.classList.add('hidden');
        document.body.style.overflow = '';
    }
}

// ============ Confirmation Dialog ============

export async function confirm(message) {
    // Use styled dialog if available, fallback to native
    if (window.showConfirmDialog) {
        return await window.showConfirmDialog({
            title: 'Confirm Action',
            message: message,
            type: 'warning'
        });
    }
    return window.confirm(message);
}

// ============ Time Formatting ============

export function formatTime(isoString) {
    if (!isoString) return '--';
    
    const date = new Date(isoString);
    const now = new Date();
    const diff = now - date;

    if (diff < 60000) return 'Just now';
    if (diff < 3600000) return `${Math.floor(diff / 60000)}m ago`;
    if (diff < 86400000) return `${Math.floor(diff / 3600000)}h ago`;

    return date.toLocaleDateString() + ' ' + date.toLocaleTimeString([], { 
        hour: '2-digit', 
        minute: '2-digit' 
    });
}

export function formatDuration(seconds) {
    if (seconds < 60) return `${seconds}s`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
    const hours = Math.floor(seconds / 3600);
    const mins = Math.floor((seconds % 3600) / 60);
    return `${hours}h ${mins}m`;
}

export function formatBytes(bytes) {
    if (bytes === 0) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
}

// ============ HTML Helpers ============

export function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

export function html(strings, ...values) {
    return strings.reduce((result, string, i) => {
        const value = values[i - 1];
        if (Array.isArray(value)) {
            return result + value.join('') + string;
        }
        return result + (value ?? '') + string;
    });
}

// ============ DOM Helpers ============

export function $(selector) {
    return document.querySelector(selector);
}

export function $$(selector) {
    return document.querySelectorAll(selector);
}

export function setContent(selector, content) {
    const el = $(selector);
    if (el) el.innerHTML = content;
}

export function addClass(selector, ...classes) {
    const el = $(selector);
    if (el) el.classList.add(...classes);
}

export function removeClass(selector, ...classes) {
    const el = $(selector);
    if (el) el.classList.remove(...classes);
}

export function toggleClass(selector, className, force) {
    const el = $(selector);
    if (el) el.classList.toggle(className, force);
}

// ============ Loading States ============

export function setLoading(selector, loading = true) {
    const el = $(selector);
    if (!el) return;

    if (loading) {
        el.innerHTML = `
            <div class="flex items-center justify-center py-8">
                <div class="loading-spinner"></div>
            </div>
        `;
    }
}

export function setError(selector, message) {
    const el = $(selector);
    if (el) {
        el.innerHTML = `
            <div class="text-red-400 text-center py-8">
                <i class="fas fa-exclamation-circle text-2xl mb-2"></i>
                <p>${escapeHtml(message)}</p>
            </div>
        `;
    }
}

export function setEmpty(selector, message, icon = 'fa-inbox') {
    const el = $(selector);
    if (el) {
        el.innerHTML = `
            <div class="empty-state">
                <i class="fas ${icon} empty-state-icon"></i>
                <p class="empty-state-text">${escapeHtml(message)}</p>
            </div>
        `;
    }
}

// ============ Status Helpers ============

export function getStatusColor(status) {
    const colors = {
        running: 'text-green-400',
        active: 'text-green-400',
        enabled: 'text-green-400',
        completed: 'text-green-400',
        stopped: 'text-red-400',
        inactive: 'text-red-400',
        disabled: 'text-slate-400',
        failed: 'text-red-400',
        pending: 'text-yellow-400',
        warning: 'text-yellow-400',
        cancelled: 'text-slate-400'
    };
    return colors[status?.toLowerCase()] || 'text-slate-400';
}

export function getStatusBadgeClass(status) {
    const classes = {
        running: 'badge-info',
        active: 'badge-success',
        completed: 'badge-success',
        stopped: 'badge-danger',
        failed: 'badge-danger',
        pending: 'badge-warning',
        cancelled: 'badge-neutral'
    };
    return classes[status?.toLowerCase()] || 'badge-neutral';
}

export function getStatusDotClass(active) {
    return active ? 'bg-green-500' : 'bg-red-500';
}

// ============ Form Helpers ============

export function getFormData(formId) {
    const form = $(formId);
    if (!form) return {};
    
    const formData = new FormData(form);
    const data = {};
    
    for (const [key, value] of formData.entries()) {
        if (value !== '') {
            data[key] = value;
        }
    }
    
    return data;
}

export function resetForm(formId) {
    const form = $(formId);
    if (form) form.reset();
}

export function setFormValues(formId, values) {
    const form = $(formId);
    if (!form) return;
    
    Object.entries(values).forEach(([key, value]) => {
        const field = form.elements[key];
        if (field) {
            if (field.type === 'checkbox') {
                field.checked = !!value;
            } else {
                field.value = value ?? '';
            }
        }
    });
}

// Export all
export default {
    showToast,
    showModal,
    hideModal,
    confirm,
    formatTime,
    formatDuration,
    formatBytes,
    escapeHtml,
    html,
    $,
    $$,
    setContent,
    addClass,
    removeClass,
    toggleClass,
    setLoading,
    setError,
    setEmpty,
    getStatusColor,
    getStatusBadgeClass,
    getStatusDotClass,
    getFormData,
    resetForm,
    setFormValues
};

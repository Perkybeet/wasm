/**
 * WASM Web Dashboard - Modal Dialogs
 * Enhanced confirmation dialogs replacing browser confirm().
 */

import { escapeHtml } from './ui.js';

/**
 * Show a styled confirmation dialog
 * @param {Object} options - Dialog options
 * @param {string} options.title - Dialog title
 * @param {string} options.message - Dialog message
 * @param {string} options.confirmText - Confirm button text
 * @param {string} options.cancelText - Cancel button text
 * @param {string} options.type - Dialog type: 'danger', 'warning', 'info'
 * @returns {Promise<boolean>} - Resolves to true if confirmed, false otherwise
 */
export function showConfirmDialog({
    title = 'Confirm Action',
    message = 'Are you sure you want to proceed?',
    confirmText = 'Confirm',
    cancelText = 'Cancel',
    type = 'warning'
}) {
    return new Promise((resolve) => {
        // Remove any existing dialog
        const existing = document.getElementById('confirm-dialog');
        if (existing) existing.remove();

        const icons = {
            danger: 'fa-exclamation-triangle text-red-400',
            warning: 'fa-exclamation-circle text-yellow-400',
            info: 'fa-info-circle text-blue-400',
            success: 'fa-check-circle text-green-400'
        };

        const buttonColors = {
            danger: 'bg-red-500 hover:bg-red-600',
            warning: 'bg-yellow-500 hover:bg-yellow-600',
            info: 'bg-blue-500 hover:bg-blue-600',
            success: 'bg-green-500 hover:bg-green-600'
        };

        const dialog = document.createElement('div');
        dialog.id = 'confirm-dialog';
        dialog.className = 'fixed inset-0 z-[200] flex items-center justify-center';
        dialog.innerHTML = `
            <div class="absolute inset-0 bg-black/60 backdrop-blur-sm animate-fadeIn" id="confirm-backdrop"></div>
            <div class="relative bg-slate-800 rounded-xl shadow-2xl border border-slate-700 max-w-md w-full mx-4 animate-slideUp">
                <div class="p-6">
                    <div class="flex items-start gap-4">
                        <div class="w-12 h-12 rounded-full bg-slate-700 flex items-center justify-center flex-shrink-0">
                            <i class="fas ${icons[type] || icons.warning} text-xl"></i>
                        </div>
                        <div class="flex-1">
                            <h3 class="text-lg font-semibold text-white">${escapeHtml(title)}</h3>
                            <p class="mt-2 text-slate-300">${escapeHtml(message)}</p>
                        </div>
                    </div>
                </div>
                <div class="px-6 py-4 bg-slate-900/50 rounded-b-xl flex justify-end gap-3">
                    <button id="confirm-cancel" class="px-4 py-2 bg-slate-700 hover:bg-slate-600 text-white rounded-lg transition-colors">
                        ${escapeHtml(cancelText)}
                    </button>
                    <button id="confirm-ok" class="${buttonColors[type] || buttonColors.warning} px-4 py-2 text-white rounded-lg transition-colors">
                        ${escapeHtml(confirmText)}
                    </button>
                </div>
            </div>
        `;

        document.body.appendChild(dialog);
        document.body.style.overflow = 'hidden';

        const cleanup = () => {
            dialog.remove();
            document.body.style.overflow = '';
        };

        // Event handlers
        document.getElementById('confirm-cancel').onclick = () => {
            cleanup();
            resolve(false);
        };

        document.getElementById('confirm-ok').onclick = () => {
            cleanup();
            resolve(true);
        };

        document.getElementById('confirm-backdrop').onclick = () => {
            cleanup();
            resolve(false);
        };

        // Handle ESC key
        const handleEsc = (e) => {
            if (e.key === 'Escape') {
                cleanup();
                resolve(false);
                document.removeEventListener('keydown', handleEsc);
            }
        };
        document.addEventListener('keydown', handleEsc);

        // Focus the confirm button
        document.getElementById('confirm-ok').focus();
    });
}

/**
 * Show an input dialog
 * @param {Object} options - Dialog options
 * @returns {Promise<string|null>} - Resolves to input value or null if cancelled
 */
export function showInputDialog({
    title = 'Enter Value',
    message = '',
    placeholder = '',
    defaultValue = '',
    confirmText = 'OK',
    cancelText = 'Cancel',
    inputType = 'text'
}) {
    return new Promise((resolve) => {
        const existing = document.getElementById('input-dialog');
        if (existing) existing.remove();

        const dialog = document.createElement('div');
        dialog.id = 'input-dialog';
        dialog.className = 'fixed inset-0 z-[200] flex items-center justify-center';
        dialog.innerHTML = `
            <div class="absolute inset-0 bg-black/60 backdrop-blur-sm animate-fadeIn" id="input-backdrop"></div>
            <div class="relative bg-slate-800 rounded-xl shadow-2xl border border-slate-700 max-w-md w-full mx-4 animate-slideUp">
                <div class="p-6">
                    <h3 class="text-lg font-semibold text-white mb-2">${escapeHtml(title)}</h3>
                    ${message ? `<p class="text-slate-400 mb-4">${escapeHtml(message)}</p>` : ''}
                    <input 
                        type="${inputType}" 
                        id="input-dialog-value" 
                        class="w-full bg-slate-700 border border-slate-600 rounded-lg px-4 py-2 text-white focus:outline-none focus:border-indigo-500"
                        placeholder="${escapeHtml(placeholder)}"
                        value="${escapeHtml(defaultValue)}"
                    >
                </div>
                <div class="px-6 py-4 bg-slate-900/50 rounded-b-xl flex justify-end gap-3">
                    <button id="input-cancel" class="px-4 py-2 bg-slate-700 hover:bg-slate-600 text-white rounded-lg transition-colors">
                        ${escapeHtml(cancelText)}
                    </button>
                    <button id="input-ok" class="px-4 py-2 bg-indigo-500 hover:bg-indigo-600 text-white rounded-lg transition-colors">
                        ${escapeHtml(confirmText)}
                    </button>
                </div>
            </div>
        `;

        document.body.appendChild(dialog);
        document.body.style.overflow = 'hidden';

        const input = document.getElementById('input-dialog-value');
        const cleanup = () => {
            dialog.remove();
            document.body.style.overflow = '';
        };

        const submit = () => {
            const value = input.value;
            cleanup();
            resolve(value);
        };

        document.getElementById('input-cancel').onclick = () => {
            cleanup();
            resolve(null);
        };

        document.getElementById('input-ok').onclick = submit;
        document.getElementById('input-backdrop').onclick = () => {
            cleanup();
            resolve(null);
        };

        input.onkeydown = (e) => {
            if (e.key === 'Enter') submit();
        };

        const handleEsc = (e) => {
            if (e.key === 'Escape') {
                cleanup();
                resolve(null);
                document.removeEventListener('keydown', handleEsc);
            }
        };
        document.addEventListener('keydown', handleEsc);

        // Focus input
        setTimeout(() => {
            input.focus();
            input.select();
        }, 50);
    });
}

// Make dialogs available globally
window.showConfirmDialog = showConfirmDialog;
window.showInputDialog = showInputDialog;

export default { showConfirmDialog, showInputDialog };

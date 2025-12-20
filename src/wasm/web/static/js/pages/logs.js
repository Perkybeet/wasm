/**
 * WASM Web Dashboard - Logs Page
 */

import { api } from '../core/api.js';
import { ws } from '../core/websocket.js';

let currentLogWs = null;

/**
 * Load logs page
 */
export async function load() {
    try {
        const data = await api.getApps();
        updateAppSelect(data.apps);
    } catch (error) {
        console.error('Failed to load apps for logs:', error);
    }
}

/**
 * Update app selector
 */
function updateAppSelect(apps) {
    const select = document.getElementById('log-app-select');
    if (select) {
        select.innerHTML = '<option value="">Select Application...</option>' +
            apps.map(app => `<option value="${app.domain}">${app.domain}</option>`).join('');
    }
}

/**
 * Switch log stream to selected app
 */
export function switchStream() {
    const select = document.getElementById('log-app-select');
    const domain = select?.value;
    const container = document.getElementById('logs-container');

    // Close existing connection
    if (currentLogWs) {
        ws.close(`logs-${currentLogWs.domain}`);
        currentLogWs = null;
    }

    if (!domain) {
        if (container) {
            container.innerHTML = '<div class="text-slate-500 text-center py-8">Select an application to view logs</div>';
        }
        return;
    }

    // Clear and start streaming
    if (container) container.innerHTML = '';

    currentLogWs = ws.connectLogs(
        domain,
        (data) => {
            if (!container) return;
            
            if (data.type === 'log') {
                const line = document.createElement('div');
                line.className = 'log-line';
                line.textContent = data.data;
                container.appendChild(line);
                container.scrollTop = container.scrollHeight;
            } else if (data.type === 'connected') {
                const info = document.createElement('div');
                info.className = 'text-slate-500 text-center py-2';
                info.textContent = `Connected to ${data.service}`;
                container.appendChild(info);
            } else if (data.type === 'error') {
                const errLine = document.createElement('div');
                errLine.className = 'text-red-400 py-2';
                errLine.textContent = `Error: ${data.message}`;
                container.appendChild(errLine);
            } else if (data.type === 'warning') {
                const warnLine = document.createElement('div');
                warnLine.className = 'text-yellow-400 py-2';
                warnLine.textContent = data.data;
                container.appendChild(warnLine);
            }
        },
        (error) => {
            if (!container) return;
            const errLine = document.createElement('div');
            errLine.className = 'text-red-400 py-2';
            errLine.textContent = 'WebSocket error. Reconnecting...';
            container.appendChild(errLine);
        }
    );
    currentLogWs.domain = domain;
}

/**
 * Clear logs display
 */
export function clear() {
    const container = document.getElementById('logs-container');
    if (container) container.innerHTML = '';
}

/**
 * Get current log connection
 */
export function getCurrentConnection() {
    return currentLogWs;
}

export default { load, switchStream, clear, getCurrentConnection };

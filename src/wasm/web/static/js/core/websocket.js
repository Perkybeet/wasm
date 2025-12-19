/**
 * WASM Web Dashboard - WebSocket Manager
 * Handles real-time connections for metrics, logs, and jobs.
 */

class WasmWebSocket {
    constructor() {
        this.connections = {};
        this.reconnectAttempts = {};
        this.maxReconnectAttempts = 5;
        this.reconnectDelay = 3000;
    }

    /**
     * Get current session token (always fresh from localStorage)
     */
    getSessionToken() {
        return localStorage.getItem('wasm_session');
    }

    getWsUrl() {
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        return `${protocol}//${window.location.host}`;
    }

    /**
     * Generic connection handler with auto-reconnect
     */
    connect(key, url, handlers = {}) {
        // Close existing connection
        this.close(key);

        // Log the connection attempt (mask token for security)
        const sanitizedUrl = url.replace(/token=[^&]+/, 'token=***');
        console.log(`[WS] Connecting to: ${key} (${sanitizedUrl})`);

        const ws = new WebSocket(url);
        this.reconnectAttempts[key] = 0;

        ws.onopen = () => {
            console.log(`[WS] Connected: ${key}`);
            this.reconnectAttempts[key] = 0;
            handlers.onOpen?.();
        };

        ws.onmessage = (event) => {
            try {
                const data = JSON.parse(event.data);
                handlers.onMessage?.(data);
            } catch (e) {
                handlers.onMessage?.(event.data);
            }
        };

        ws.onerror = (error) => {
            console.error(`[WS] Error: ${key}`, error);
            handlers.onError?.(error);
        };

        ws.onclose = (event) => {
            console.log(`[WS] Disconnected: ${key}, code: ${event.code}, reason: ${event.reason}, wasClean: ${event.wasClean}`);
            delete this.connections[key];
            handlers.onClose?.(event);

            // Auto-reconnect if not intentionally closed
            if (!event.wasClean && handlers.autoReconnect !== false) {
                this.attemptReconnect(key, url, handlers);
            }
        };

        this.connections[key] = ws;
        return ws;
    }

    attemptReconnect(key, url, handlers) {
        if (this.reconnectAttempts[key] >= this.maxReconnectAttempts) {
            console.warn(`[WS] Max reconnect attempts reached for: ${key}`);
            return;
        }

        this.reconnectAttempts[key]++;
        const delay = this.reconnectDelay * this.reconnectAttempts[key];
        
        console.log(`[WS] Reconnecting ${key} in ${delay}ms (attempt ${this.reconnectAttempts[key]})`);
        
        setTimeout(() => {
            if (!this.connections[key]) {
                this.connect(key, url, handlers);
            }
        }, delay);
    }

    /**
     * Connect to system metrics stream
     */
    connectSystem(onMessage, onError, interval = 2) {
        const url = `${this.getWsUrl()}/ws/system?token=${this.getSessionToken()}&interval=${interval}`;
        return this.connect('system', url, {
            onMessage,
            onError,
            autoReconnect: true
        });
    }

    /**
     * Connect to log stream for an app
     */
    connectLogs(domain, onMessage, onError) {
        const key = `logs-${domain}`;
        const url = `${this.getWsUrl()}/ws/logs/${encodeURIComponent(domain)}?token=${this.getSessionToken()}`;
        const ws = this.connect(key, url, {
            onMessage,
            onError,
            autoReconnect: false
        });
        ws.domain = domain;
        return ws;
    }

    /**
     * Connect to a specific job's updates
     */
    connectJob(jobId, onMessage, onError) {
        const key = `job-${jobId}`;
        const url = `${this.getWsUrl()}/ws/jobs/${jobId}?token=${this.getSessionToken()}`;
        return this.connect(key, url, {
            onMessage,
            onError,
            autoReconnect: false
        });
    }

    /**
     * Connect to all jobs updates stream
     */
    connectAllJobs(onMessage, onError) {
        const url = `${this.getWsUrl()}/ws/jobs?token=${this.getSessionToken()}`;
        return this.connect('all-jobs', url, {
            onMessage,
            onError,
            autoReconnect: true
        });
    }

    /**
     * Close a specific connection
     */
    close(key) {
        if (this.connections[key]) {
            this.connections[key].close();
            delete this.connections[key];
        }
    }

    /**
     * Close all connections
     */
    closeAll() {
        Object.keys(this.connections).forEach(key => this.close(key));
    }

    /**
     * Check if a connection is active
     */
    isConnected(key) {
        return this.connections[key]?.readyState === WebSocket.OPEN;
    }
}

// Export singleton instance
export const ws = new WasmWebSocket();
export default ws;

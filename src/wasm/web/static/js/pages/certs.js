/**
 * WASM Web Dashboard - Certificates Page
 */

import { api } from '../core/api.js';
import { showToast, showModal, hideModal, setLoading, setEmpty, setError, confirm } from '../core/ui.js';
import { renderCertCard } from '../components/cards.js';
import { router } from '../core/router.js';

/**
 * Load certificates list
 */
export async function load() {
    const container = document.getElementById('certs-list');
    if (!container) return;

    setLoading('#certs-list');

    try {
        const data = await api.getCertificates();

        if (data.certificates.length === 0) {
            setEmpty('#certs-list', 'No SSL certificates found', 'fa-shield-alt');
            return;
        }

        container.innerHTML = data.certificates.map(cert => renderCertCard(cert, {
            renew: `window.certsPage.renew('${cert.domain}')`,
            revoke: `window.certsPage.revoke('${cert.domain}')`,
            remove: `window.certsPage.remove('${cert.domain}')`
        })).join('');

    } catch (error) {
        setError('#certs-list', `Failed to load certificates: ${error.message}`);
    }
}

/**
 * Show create certificate modal
 */
export function showCreate() {
    showModal('create-cert-modal');
}

/**
 * Hide create certificate modal
 */
export function hideCreate() {
    hideModal('create-cert-modal');
    document.getElementById('create-cert-form')?.reset();
}

/**
 * Create a new certificate
 */
export async function create(event) {
    event.preventDefault();

    const form = event.target;
    const formData = new FormData(form);

    const domainsStr = formData.get('domains');
    if (!domainsStr) {
        showToast('Please enter at least one domain', 'error');
        return;
    }

    const domains = domainsStr.split(',').map(d => d.trim()).filter(d => d);
    const data = {
        domains,
        email: formData.get('email') || null,
        method: formData.get('method') || 'nginx',
        dry_run: formData.get('dry_run') === 'on',
    };

    try {
        const result = await api.createCertJob(domains[0], data.email);
        showToast(`Certificate job started for ${domains[0]}`, 'info');
        hideCreate();
        router.navigate('jobs');
        if (result.job?.id) {
            setTimeout(() => window.jobsPage?.showDetails(result.job.id), 500);
        }
    } catch (error) {
        showToast(`Failed to create certificate: ${error.message}`, 'error');
    }
}

/**
 * Renew a certificate
 */
export async function renew(domain) {
    try {
        showToast(`Renewing certificate for ${domain}...`, 'info');
        await api.renewCertificate(domain);
        showToast(`Certificate renewed: ${domain}`, 'success');
        load();
    } catch (error) {
        showToast(`Failed: ${error.message}`, 'error');
    }
}

/**
 * Renew all certificates
 */
export async function renewAll() {
    try {
        showToast('Renewing all certificates...', 'info');
        await api.renewAllCertificates();
        showToast('Certificates renewed', 'success');
        load();
    } catch (error) {
        showToast(`Failed: ${error.message}`, 'error');
    }
}

/**
 * Revoke a certificate
 */
export async function revoke(domain) {
    if (!await confirm(`Are you sure you want to revoke the certificate for ${domain}? This action cannot be undone.`)) {
        return;
    }

    try {
        showToast(`Revoking certificate for ${domain}...`, 'info');
        await api.revokeCertificate(domain);
        showToast(`Certificate revoked: ${domain}`, 'success');
        load();
    } catch (error) {
        showToast(`Failed: ${error.message}`, 'error');
    }
}

/**
 * Delete a certificate
 */
export async function remove(domain) {
    if (!await confirm(`Are you sure you want to delete the certificate for ${domain}?`)) {
        return;
    }

    try {
        await api.deleteCertificate(domain);
        showToast(`Certificate deleted: ${domain}`, 'success');
        load();
    } catch (error) {
        showToast(`Failed: ${error.message}`, 'error');
    }
}

export default { load, showCreate, hideCreate, create, renew, renewAll, revoke, remove };

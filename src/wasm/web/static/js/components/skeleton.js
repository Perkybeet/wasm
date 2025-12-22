/**
 * WASM Web Dashboard - Skeleton Loading Components
 * Provides skeleton loading states for better UX.
 */

/**
 * Generate skeleton card HTML
 */
export function skeletonCard() {
    return `
        <div class="skeleton-card">
            <div class="flex items-center gap-4 mb-4">
                <div class="skeleton-circle w-10 h-10"></div>
                <div class="flex-1">
                    <div class="skeleton-line w-3/4 mb-2"></div>
                    <div class="skeleton-line w-1/2 h-3"></div>
                </div>
            </div>
            <div class="space-y-2">
                <div class="skeleton-line w-full"></div>
                <div class="skeleton-line w-5/6"></div>
            </div>
        </div>
    `;
}

/**
 * Generate multiple skeleton cards
 */
export function skeletonCards(count = 3) {
    return Array(count).fill(skeletonCard()).join('');
}

/**
 * Generate skeleton list item
 */
export function skeletonListItem() {
    return `
        <div class="flex items-center gap-4 p-4 bg-slate-800/50 rounded-lg">
            <div class="skeleton-circle w-8 h-8"></div>
            <div class="flex-1">
                <div class="skeleton-line w-2/3 mb-2"></div>
                <div class="skeleton-line w-1/3 h-3"></div>
            </div>
            <div class="skeleton-line w-16 h-6 rounded-full"></div>
        </div>
    `;
}

/**
 * Generate skeleton table rows
 */
export function skeletonTable(rows = 5, cols = 4) {
    const headerCells = Array(cols).fill('<th class="skeleton-line h-4 w-20"></th>').join('');
    const rowCells = Array(cols).fill('<td><div class="skeleton-line h-4"></div></td>').join('');
    const tableRows = Array(rows).fill(`<tr class="border-t border-slate-700">${rowCells}</tr>`).join('');
    
    return `
        <table class="w-full">
            <thead>
                <tr>${headerCells}</tr>
            </thead>
            <tbody>
                ${tableRows}
            </tbody>
        </table>
    `;
}

/**
 * Generate skeleton metric cards
 */
export function skeletonMetrics(count = 4) {
    const card = `
        <div class="skeleton-card">
            <div class="flex justify-between items-center mb-4">
                <div class="skeleton-line w-20 h-4"></div>
                <div class="skeleton-circle w-6 h-6"></div>
            </div>
            <div class="skeleton-line w-16 h-8 mb-2"></div>
            <div class="skeleton-line w-full h-2 rounded-full"></div>
        </div>
    `;
    return Array(count).fill(card).join('');
}

/**
 * Generate skeleton app overview item
 */
export function skeletonAppItem() {
    return `
        <div class="flex items-center justify-between p-3 bg-slate-800/50 rounded-lg">
            <div class="flex items-center gap-3">
                <div class="skeleton-circle w-2 h-2"></div>
                <div class="skeleton-line w-32"></div>
            </div>
            <div class="skeleton-line w-16 h-4"></div>
        </div>
    `;
}

/**
 * Generate skeleton for dashboard page
 */
export function skeletonDashboard() {
    return `
        <!-- Metrics -->
        <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6 mb-8">
            ${skeletonMetrics(4)}
        </div>
        
        <!-- Two column layout -->
        <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
            <div class="skeleton-card">
                <div class="skeleton-line w-24 h-5 mb-4"></div>
                <div class="space-y-3">
                    ${Array(4).fill(skeletonAppItem()).join('')}
                </div>
            </div>
            <div class="skeleton-card">
                <div class="skeleton-line w-24 h-5 mb-4"></div>
                <div class="space-y-3">
                    ${Array(4).fill(skeletonListItem()).join('')}
                </div>
            </div>
        </div>
    `;
}

/**
 * Show skeleton loading in a container
 */
export function showSkeleton(containerId, type = 'cards', count = 3) {
    const container = document.getElementById(containerId) || document.querySelector(containerId);
    if (!container) return;

    let content;
    switch (type) {
        case 'card':
            content = skeletonCard();
            break;
        case 'cards':
            content = skeletonCards(count);
            break;
        case 'list':
            content = Array(count).fill(skeletonListItem()).join('');
            break;
        case 'table':
            content = skeletonTable(count);
            break;
        case 'metrics':
            content = skeletonMetrics(count);
            break;
        case 'dashboard':
            content = skeletonDashboard();
            break;
        default:
            content = skeletonCards(count);
    }

    container.innerHTML = `<div class="space-y-4">${content}</div>`;
}

export default {
    skeletonCard,
    skeletonCards,
    skeletonListItem,
    skeletonTable,
    skeletonMetrics,
    skeletonDashboard,
    showSkeleton
};

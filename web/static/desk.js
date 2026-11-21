(() => {
    const feed = document.getElementById('latest-activity');
    let checking = false;
    const inUse = () => feed.contains(document.activeElement) ||
        Array.from(feed.querySelectorAll('video')).some(video => !video.paused && !video.ended);
    const refresh = async () => {
        if (checking || document.hidden || inUse()) return;
        checking = true;
        try {
            const response = await fetch('/latest-activity?workspace=' + encodeURIComponent(feed.dataset.workspace || 'gta6'));
            if (!response.ok) return;
            const next = document.createElement('template');
            next.innerHTML = await response.text();
            if (!inUse() && next.innerHTML !== feed.innerHTML && next.content.querySelector('.latest-groups')) {
                feed.replaceChildren(next.content);
            }
            const tabs = document.querySelector('.creator-tabs');
            if (tabs && !tabs.contains(document.activeElement)) {
                const order = await fetch(tabs.dataset.refresh);
                if (order.ok) {
                    const updated = document.createElement('template');
                    updated.innerHTML = await order.text();
                    const replacement = updated.content.querySelector('.creator-tabs');
                    if (replacement && !tabs.contains(document.activeElement) && replacement.innerHTML !== tabs.innerHTML) {
                        tabs.replaceWith(replacement);
                    }
                }
            }
        } catch {
            // Keep the previews in place until the next connection succeeds.
        } finally {
            checking = false;
        }
    };
    window.setInterval(refresh, 30000);
    document.addEventListener('visibilitychange', refresh);
})();

document.addEventListener('submit', event => {
    const label = event.target.dataset.busy;
    if (event.defaultPrevented || !label) return;
    const button = event.submitter || event.target.querySelector('button');
    if (button) {
        button.dataset.idleLabel = button.textContent;
        button.disabled = true;
        button.textContent = label;
    }
});

window.addEventListener('pageshow', () => {
    for (const button of document.querySelectorAll('button[data-idle-label]')) {
        button.disabled = false;
        button.textContent = button.dataset.idleLabel;
        delete button.dataset.idleLabel;
    }
});

document.querySelector('[data-copy-caption]')?.addEventListener('click', async () => {
    const field = document.getElementById('post-copy');
    const status = document.getElementById('copy-status');
    field.focus();
    field.select();
    status.textContent = 'Press Ctrl+C to copy the selected caption.';
    try {
        await navigator.clipboard.writeText(field.value);
        status.textContent = 'Caption copied.';
    } catch {
        // Keep the caption selected for manual copying.
    }
});

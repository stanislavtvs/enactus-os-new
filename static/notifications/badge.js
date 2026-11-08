const nav = document.querySelector("#notification-nav");
const badge = document.querySelector("#notification-nav-badge");
const refreshInterval = 60_000;
let unread = null;
let revision = 0;
let pending = false;
let interval;

export function getUnreadCount() {
    return unread;
}

export function updateUnreadCount(count) {
    if (!Number.isSafeInteger(count) || count < 0) {
        return;
    }
    revision++;
    unread = count;
    if (nav && badge) {
        badge.hidden = count === 0;
        badge.textContent = count > 99 ? "99+" : String(count);
        nav.setAttribute("aria-label", count ? `Уведомления. Непрочитанных: ${count}` : "Уведомления");
    }
    document.dispatchEvent(new CustomEvent("notifications:count", {detail: count}));
}

async function poll(refreshList = true) {
    if (pending || document.visibilityState !== "visible") {
        return;
    }
    const observedRevision = revision;
    pending = true;
    try {
        const response = await fetch(nav.dataset.countUrl, {mode: "same-origin", headers: {Accept: "application/json"}});
        if (!response.ok) {
            if ([401, 403].includes(response.status) && revision === observedRevision) {
                updateUnreadCount(0);
            }
            return;
        }
        const {unread: count} = await response.json();
        if (revision === observedRevision && document.visibilityState === "visible") {
            updateUnreadCount(count);
        }
    } catch {
        // The next visible-tab refresh retries a transient connection error.
    } finally {
        pending = false;
        if (refreshList && document.visibilityState === "visible") {
            document.dispatchEvent(new Event("notifications:refresh"));
        }
    }
}

function schedule(refreshList) {
    clearInterval(interval);
    if (document.visibilityState === "visible") {
        poll(refreshList);
        interval = setInterval(poll, refreshInterval);
    }
}

if (nav?.dataset.countUrl) {
    document.addEventListener("visibilitychange", () => schedule(true));
    schedule(false);
}

(() => {
    const shell = document.querySelector("#workspace-shell");
    if (!shell) {
        return;
    }

    const sidebar = shell.querySelector("#workspace-sidebar");
    const backdrop = shell.querySelector("#workspace-sidebar-backdrop");
    const content = shell.querySelector("#workspace-content");
    const toggle = shell.querySelector("#workspace-sidebar-toggle");
    const close = shell.querySelector("#workspace-sidebar-close");
    const skip = document.querySelector(".skip-link");
    const mobile = window.matchMedia("(max-width: 47rem)");
    const storageKey = "enactus-os-sidebar";
    let compact = false;
    let open = false;

    try {
        compact = localStorage.getItem(storageKey) === "compact";
    } catch {
        // The current page still supports collapsing the navigation.
    }

    function render() {
        const drawer = mobile.matches;
        const overlay = drawer && open;
        shell.dataset.sidebar = drawer ? (open ? "open" : "closed") : (compact ? "compact" : "expanded");
        sidebar.inert = drawer && !open;
        content.inert = overlay;
        skip.inert = overlay;
        backdrop.hidden = !overlay;
        close.hidden = !overlay;
        if (overlay) {
            sidebar.setAttribute("role", "dialog");
            sidebar.setAttribute("aria-modal", "true");
        } else {
            sidebar.removeAttribute("role");
            sidebar.removeAttribute("aria-modal");
        }
        document.body.classList.toggle("workspace-drawer-open", overlay);
        toggle.setAttribute("aria-expanded", String(drawer ? open : !compact));
        toggle.setAttribute("aria-label", drawer
            ? "Открыть навигацию"
            : (compact ? "Развернуть боковое меню" : "Свернуть боковое меню"));
        toggle.title = toggle.getAttribute("aria-label");
    }

    function dismiss() {
        open = false;
        render();
        toggle.focus();
    }

    toggle.addEventListener("click", () => {
        if (mobile.matches) {
            open = true;
            render();
            close.focus();
        } else {
            compact = !compact;
            render();
            try {
                localStorage.setItem(storageKey, compact ? "compact" : "expanded");
            } catch {
                // The current page already reflects the selected width.
            }
        }
    });
    close.addEventListener("click", dismiss);
    backdrop.addEventListener("click", dismiss);
    mobile.addEventListener("change", () => {
        const focusWasInSidebar = sidebar.contains(document.activeElement);
        open = false;
        render();
        if (focusWasInSidebar) {
            toggle.focus();
        }
    });

    document.addEventListener("keydown", (event) => {
        if (!mobile.matches || !open) {
            return;
        }
        if (event.key === "Escape") {
            event.preventDefault();
            dismiss();
        } else if (event.key === "Tab") {
            const controls = [...sidebar.querySelectorAll("a[href], button:not([disabled]), select:not([disabled])")]
                .filter((control) => control.getClientRects().length > 0);
            const first = controls[0];
            const last = controls.at(-1);
            const target = event.shiftKey && document.activeElement === first ? last
                : !event.shiftKey && document.activeElement === last ? first : null;
            if (target) {
                event.preventDefault();
                target.focus();
            }
        }
    });

    render();
    toggle.hidden = false;
})();

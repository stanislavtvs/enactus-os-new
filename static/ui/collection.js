function element(tag, className, text) {
    const node = document.createElement(tag);
    node.className = className;
    if (text !== undefined) {
        node.textContent = text;
    }
    return node;
}

function closedDetails(details, focus = false) {
    details.open = false;
    if (focus) {
        details.querySelector("summary").focus();
    }
}

export function setupCollection(app, list, storageKey) {
    const buttons = app.querySelectorAll("[data-collection-mode]");
    function setMode(mode) {
        list.dataset.mode = mode === "grid" ? "grid" : "list";
        for (const button of buttons) {
            button.setAttribute("aria-pressed", String(button.dataset.collectionMode === list.dataset.mode));
        }
    }

    let savedMode;
    try {
        savedMode = localStorage.getItem(storageKey);
    } catch {
        // The switch still works for the current page.
    }
    setMode(savedMode);

    for (const button of buttons) {
        button.addEventListener("click", () => {
            setMode(button.dataset.collectionMode);
            try {
                localStorage.setItem(storageKey, list.dataset.mode);
            } catch {
                // This page already reflects the selected mode.
            }
        });
    }
    setupDisclosures(app);
}

export function setupDisclosures(app) {
    const disclosures = () => app.querySelectorAll("details.collection-actions[open], details.collection-filters[open]");
    document.addEventListener("click", (event) => {
        for (const details of disclosures()) {
            if (!details.contains(event.target)) {
                closedDetails(details);
            }
        }
    });
    app.addEventListener("keydown", (event) => {
        if (event.key !== "Escape" || event.defaultPrevented) {
            return;
        }
        for (const details of disclosures()) {
            if (details.contains(event.target)) {
                event.preventDefault();
                closedDetails(details, true);
                break;
            }
        }
    });
}

export function setupDialog(dialog, opener = null) {
    let returnFocus;
    let busy = false;
    const buttons = dialog.querySelectorAll("[data-dialog-close], button[type='submit']");
    function open(trigger = opener, initialField = dialog.querySelector("[autofocus]")
        ?? dialog.querySelector("form input:not([type='hidden'])")) {
        returnFocus = trigger;
        dialog.showModal();
        initialField?.focus();
    }
    opener?.addEventListener("click", () => open());
    for (const close of dialog.querySelectorAll("[data-dialog-close]")) {
        close.addEventListener("click", () => dialog.close());
    }
    dialog.addEventListener("cancel", (event) => {
        if (busy) {
            event.preventDefault();
        }
    });
    dialog.addEventListener("close", () => returnFocus?.focus());
    return {
        open,
        close() { dialog.close(); },
        setBusy(value) {
            busy = value;
            for (const button of buttons) {
                button.disabled = value;
            }
        },
    };
}

function menuLink(label, href) {
    const link = element("a", "collection-menu-item", label);
    link.href = href;
    return link;
}

export function collectionRow({title, href, status, details = [], badges = [], canEdit, canArchive, onArchive}) {
    const row = element("li", "collection-item");
    const link = element("a", "collection-title", title);
    link.href = href;
    const body = element("div", "collection-item-body");
    body.append(link);

    const visibleDetails = details.filter(({value}) => value);
    if (visibleDetails.length) {
        const facts = element("dl", "collection-item-facts");
        for (const {label, value} of visibleDetails) {
            const fact = element("div", "");
            fact.append(element("dt", "", label), element("dd", "", value));
            facts.append(fact);
        }
        body.append(facts);
    }

    const labels = element("div", "collection-item-labels");
    if (status) {
        labels.append(element("span", "collection-badge", status));
    }
    for (const {text, kind} of badges) {
        const className = kind === "danger" ? "collection-badge collection-badge-danger" : "collection-badge";
        labels.append(element("span", className, text));
    }

    const items = [{label: "Открыть", href}];
    if (canEdit) {
        items.push({label: "Редактировать", href: href + "#edit"});
    }
    if (canArchive) {
        items.push({label: "Архивировать", danger: true, onSelect: (trigger) => onArchive(trigger.parentElement)});
    }
    const actions = collectionActions(title, items);
    row.append(body, labels, actions);
    return row;
}

export function collectionActions(title, items) {
    const actions = element("details", "collection-actions");
    const summary = element("summary", "collection-action-toggle", "⋯");
    summary.setAttribute("aria-label", `Действия: ${title}`);
    summary.title = "Действия";
    const menu = element("div", "collection-menu");
    for (const {label, href, onSelect, danger} of items) {
        if (href) {
            menu.append(menuLink(label, href));
            continue;
        }
        const classes = danger ? "collection-menu-item collection-archive-action" : "collection-menu-item";
        const button = element("button", classes, label);
        button.type = "button";
        button.addEventListener("click", () => {
            closedDetails(actions, true);
            onSelect(summary);
        });
        menu.append(button);
    }
    actions.append(summary, menu);
    return actions;
}

export function confirmArchive(title, {
    heading = "Архивировать объект?",
    description = `«${title}» переместится в архив и останется доступен при показе архивных записей.`,
    confirmLabel = "Архивировать",
} = {}) {
    const dialog = element("dialog", "collection-dialog collection-confirmation");
    dialog.id = "collection-archive-dialog";
    dialog.setAttribute("aria-labelledby", "collection-archive-heading");
    dialog.setAttribute("aria-describedby", "collection-archive-description");
    const titleElement = element("h2", "", heading);
    titleElement.id = "collection-archive-heading";
    const descriptionElement = element("p", "muted", description);
    descriptionElement.id = "collection-archive-description";
    const form = element("form", "collection-dialog-actions");
    form.method = "dialog";
    const cancel = element("button", "button collection-secondary", "Отмена");
    cancel.type = "submit";
    cancel.value = "cancel";
    cancel.autofocus = true;
    const confirm = element("button", "button button-primary", confirmLabel);
    confirm.type = "submit";
    confirm.value = "archive";
    form.append(cancel, confirm);
    dialog.append(titleElement, descriptionElement, form);
    document.body.append(dialog);
    dialog.showModal();
    return new Promise((resolve) => {
        dialog.addEventListener("close", () => {
            const confirmed = dialog.returnValue === "archive";
            dialog.remove();
            resolve(confirmed);
        }, {once: true});
    });
}

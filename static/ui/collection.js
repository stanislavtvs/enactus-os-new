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

export function setupDialog(dialog, opener) {
    let busy = false;
    const buttons = dialog.querySelectorAll("[data-dialog-close], button[type='submit']");
    opener.addEventListener("click", () => {
        dialog.showModal();
        const initialField = dialog.querySelector("[autofocus]")
            ?? dialog.querySelector("form input:not([type='hidden'])");
        initialField?.focus();
    });
    for (const close of dialog.querySelectorAll("[data-dialog-close]")) {
        close.addEventListener("click", () => dialog.close());
    }
    dialog.addEventListener("cancel", (event) => {
        if (busy) {
            event.preventDefault();
        }
    });
    dialog.addEventListener("close", () => opener.focus());
    return {
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

    const actions = element("details", "collection-actions");
    const summary = element("summary", "collection-action-toggle", "⋯");
    summary.setAttribute("aria-label", `Действия: ${title}`);
    summary.title = "Действия";
    const menu = element("div", "collection-menu");
    menu.append(menuLink("Открыть", href));
    if (canEdit) {
        menu.append(menuLink("Редактировать", href + "#edit"));
    }
    if (canArchive) {
        const archive = element("button", "collection-menu-item collection-archive-action", "Архивировать");
        archive.type = "button";
        archive.addEventListener("click", () => {
            closedDetails(actions, true);
            onArchive(actions);
        });
        menu.append(archive);
    }
    actions.append(summary, menu);
    row.append(body, labels, actions);
    return row;
}

export function confirmArchive(title) {
    const dialog = element("dialog", "collection-dialog collection-confirmation");
    dialog.id = "collection-archive-dialog";
    dialog.setAttribute("aria-labelledby", "collection-archive-heading");
    dialog.setAttribute("aria-describedby", "collection-archive-description");
    const heading = element("h2", "", "Архивировать объект?");
    heading.id = "collection-archive-heading";
    const description = element("p", "muted", `«${title}» переместится в архив и останется доступен при показе архивных записей.`);
    description.id = "collection-archive-description";
    const form = element("form", "collection-dialog-actions");
    form.method = "dialog";
    const cancel = element("button", "button collection-secondary", "Отмена");
    cancel.type = "submit";
    cancel.value = "cancel";
    cancel.autofocus = true;
    const confirm = element("button", "button button-primary", "Архивировать");
    confirm.type = "submit";
    confirm.value = "archive";
    form.append(cancel, confirm);
    dialog.append(heading, description, form);
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

import {collectionActions, confirmArchive, setupDialog, setupDisclosures} from "../ui/collection.js";
import {element, errorMessage, request} from "./directory.js";

const app = document.querySelector("#department-app");
const listUrl = app.dataset.apiUrl;
const tree = document.querySelector("#department-tree");
const treeHeading = document.querySelector("#department-tree-heading");
const toolbar = document.querySelector("#department-toolbar");
const search = document.querySelector("#department-search");
const filter = document.querySelector("#department-show-archived");
const status = document.querySelector("#department-status");
const results = document.querySelector("#department-results");
const feedback = document.querySelector("#department-feedback");
const loadError = document.querySelector("#department-load-error");
const retry = document.querySelector("#department-retry");
const createButton = document.querySelector("#department-create-open");
const editor = document.querySelector("#department-editor");
const form = document.querySelector("#department-form");
const formHeading = document.querySelector("#department-form-heading");
const formError = document.querySelector("#department-form-error");
const saveButton = document.querySelector("#department-save");
const dialog = editor ? setupDialog(editor) : null;
const token = form?.elements.namedItem("csrfmiddlewaretoken").value;

const expanded = new Set();
const searchCollapsed = new Set();
let departments = [];
let capabilities = {root: {}, nodes: {}};
let original = null;
let operation = "create";
let busy = false;
let confirming = false;

setupDisclosures(app);

function allowed(action, id) {
    return Boolean(id === null ? capabilities.root[action] : capabilities.nodes[id]?.[action]);
}

function childIndex(items) {
    const index = new Map();
    const ids = new Set(items.map(({id}) => id));
    for (const item of items) {
        const parent = ids.has(item.parent_id) ? item.parent_id : null;
        if (!index.has(parent)) {
            index.set(parent, []);
        }
        index.get(parent).push(item);
    }
    return index;
}

function walk(index) {
    const stack = [...(index.get(null) ?? [])].reverse().map((item) => ({item, depth: 0}));
    const ordered = [];
    while (stack.length) {
        const entry = stack.pop();
        ordered.push(entry);
        const children = index.get(entry.item.id) ?? [];
        for (let i = children.length - 1; i >= 0; i--) {
            stack.push({item: children[i], depth: entry.depth + 1});
        }
    }
    return ordered;
}

function matchingPaths(items, query) {
    if (!query) {
        return {items, count: items.length};
    }
    const byId = new Map(items.map((item) => [item.id, item]));
    const keep = new Set();
    let count = 0;
    for (const item of items) {
        if (!item.name.toLocaleLowerCase().includes(query)) {
            continue;
        }
        count++;
        let current = item;
        while (current && !keep.has(current.id)) {
            keep.add(current.id);
            current = byId.get(current.parent_id);
        }
    }
    return {items: items.filter(({id}) => keep.has(id)), count};
}

function icon(symbol, className) {
    const namespace = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(namespace, "svg");
    svg.setAttribute("class", className);
    svg.setAttribute("aria-hidden", "true");
    const use = document.createElementNS(namespace, "use");
    use.setAttribute("href", `#icon-${symbol}`);
    svg.append(use);
    return svg;
}

function disclosure(item, branch, searching) {
    const button = element("button", "structure-toggle");
    button.type = "button";
    button.setAttribute("aria-controls", branch.id);
    button.append(icon("arrow", "icon"));
    function show(open) {
        branch.hidden = !open;
        button.setAttribute("aria-expanded", String(open));
        button.setAttribute("aria-label", `${open ? "Свернуть" : "Развернуть"}: ${item.name}`);
    }
    show(searching ? !searchCollapsed.has(item.id) : expanded.has(item.id));
    button.addEventListener("click", () => {
        const open = branch.hidden;
        if (searching) {
            if (open) {
                searchCollapsed.delete(item.id);
            } else {
                searchCollapsed.add(item.id);
            }
        } else if (open) {
            expanded.add(item.id);
        } else {
            expanded.delete(item.id);
        }
        show(open);
    });
    return button;
}

function menuItems(item, membersUrl) {
    const items = [];
    if (membersUrl) {
        items.push({label: "Открыть участников", href: membersUrl});
    }
    if (form && allowed("create", item.id)) {
        items.push({label: "Добавить дочернее", onSelect: (trigger) => openEditor("create", trigger, null, item.id)});
    }
    if (form && allowed("update", item.id)) {
        items.push({label: "Редактировать", onSelect: (trigger) => openEditor("update", trigger, item)});
    }
    if (form && allowed("move", item.id)) {
        items.push({label: "Переместить", onSelect: (trigger) => openEditor("move", trigger, item)});
    }
    if (form && allowed("archive", item.id)) {
        items.push({label: "Архивировать ветку", danger: true, onSelect: () => archive(item)});
    }
    return items;
}

function renderTree() {
    const query = search.value.trim().toLocaleLowerCase();
    const visible = departments.filter((item) => filter.checked || !item.is_archived);
    const {items, count} = matchingPaths(visible, query);
    const index = childIndex(items);
    const visibleIds = new Set(items.map(({id}) => id));
    const root = element("ul", "structure-list structure-list-root");
    root.setAttribute("role", "list");
    const lists = new Map([[null, root]]);
    for (const {item, depth} of walk(index)) {
        const entry = document.createElement("li");
        const row = element("div", "structure-row");
        row.id = `department-node-${item.id}`;
        row.style.setProperty("--structure-depth", depth);
        if (depth > 0) {
            row.dataset.nested = "";
        }
        if (item.is_archived) {
            row.dataset.archived = "";
        }

        entry.append(row);
        lists.get(visibleIds.has(item.parent_id) ? item.parent_id : null).append(entry);
        if (index.has(item.id)) {
            const branch = element("ul", "structure-list");
            branch.id = `department-children-${item.id}`;
            branch.setAttribute("role", "list");
            lists.set(item.id, branch);
            row.append(disclosure(item, branch, Boolean(query)));
            entry.append(branch);
        } else {
            row.append(element("span", ""));
        }

        const membersUrl = !item.is_archived && allowed("open_members", item.id)
            ? `${app.dataset.structureUrl}${item.id}/members/` : "";
        const body = element("div", "structure-body");
        const title = element(membersUrl ? "a" : "span", "structure-name", item.name);
        if (membersUrl) {
            title.href = membersUrl;
        } else {
            title.tabIndex = -1;
        }
        body.append(title);
        if (item.is_archived) {
            body.append(element("span", "collection-badge", "Архив"));
        }
        if (depth > 3) {
            body.append(element("span", "structure-level", `Уровень ${depth + 1}`));
        }
        if (item.description) {
            body.append(element("p", "structure-description", item.description));
        }
        row.append(icon("org", "icon structure-icon"), body);
        const actions = item.is_archived ? [] : menuItems(item, membersUrl);
        if (actions.length) {
            row.append(collectionActions(item.name, actions));
        }
    }

    tree.replaceChildren(root);
    if (query) {
        results.textContent = count ? `Найдено: ${count}. Показаны пути к результатам.` : "По этому запросу подразделений нет.";
    } else {
        results.textContent = count ? "" : "Подразделений для отображения пока нет.";
    }
}

function parentChoices(selected) {
    const index = childIndex(departments.filter((item) => !item.is_archived));
    const excluded = new Set();
    if (operation === "move") {
        const stack = [original.id];
        while (stack.length) {
            const id = stack.pop();
            excluded.add(id);
            for (const child of index.get(id) ?? []) {
                stack.push(child.id);
            }
        }
    }

    const select = form.elements.namedItem("parent_id");
    select.replaceChildren();
    if (selected === null || allowed(operation, null)) {
        select.add(new Option("Без родительского подразделения", ""));
    }
    for (const {item, depth} of walk(index)) {
        if (!excluded.has(item.id) && (item.id === selected || allowed(operation, item.id))) {
            const level = depth > 3 ? ` · уровень ${depth + 1}` : "";
            select.add(new Option(`${"— ".repeat(Math.min(depth, 3))}${item.name}${level}`, String(item.id)));
        }
    }
    select.value = selected === null ? "" : String(selected);
    return select;
}

function updateMoveAvailability() {
    if (operation === "move") {
        const select = form.elements.namedItem("parent_id");
        saveButton.disabled = select.value === String(original.parent_id ?? "");
    }
}

function openEditor(mode, trigger, item = null, parentId = null) {
    if (busy || confirming) {
        return;
    }
    operation = mode;
    original = item;
    const moving = mode === "move";
    const updating = mode === "update";
    const context = document.querySelector("#department-edit-context");
    context.hidden = !moving;
    context.textContent = moving ? item.name : "";
    formHeading.textContent = moving ? "Перемещение подразделения"
        : updating ? "Редактирование подразделения" : "Новое подразделение";

    const name = form.elements.namedItem("name");
    const description = form.elements.namedItem("description");
    name.value = item?.name ?? "";
    description.value = item?.description ?? "";
    name.disabled = moving;
    description.disabled = moving;
    document.querySelector("#department-name-field").hidden = moving;
    document.querySelector("#department-description-field").hidden = moving;
    document.querySelector("#department-parent-field").hidden = updating;

    const parent = form.elements.namedItem("parent_id");
    parent.disabled = updating;
    if (!updating) {
        parentChoices(moving ? item.parent_id : parentId);
    }
    formError.textContent = "";
    saveButton.textContent = moving ? "Переместить" : updating ? "Сохранить" : "Создать";
    saveButton.disabled = false;
    updateMoveAvailability();
    if (moving && parent.options.length <= 1) {
        formError.textContent = "Нет доступных подразделений для перемещения.";
    }
    dialog.open(trigger, moving ? parent : name);
}

function revealPath(parentId) {
    const byId = new Map(departments.map((item) => [item.id, item]));
    let current = byId.get(parentId);
    while (current) {
        expanded.add(current.id);
        current = byId.get(current.parent_id);
    }
}

function setBusy(value) {
    busy = value;
    tree.setAttribute("aria-busy", String(value));
    retry.disabled = value;
    if (createButton) {
        createButton.disabled = value;
    }
}

async function reload() {
    setBusy(true);
    loadError.textContent = "";
    retry.hidden = true;
    status.textContent = "Загружаем структуру…";
    try {
        [departments, capabilities] = await Promise.all([
            request(listUrl),
            request(app.dataset.actionsUrl),
        ]);
        renderTree();
        toolbar.hidden = false;
        if (createButton) {
            createButton.hidden = !allowed("create", null);
        }
        return true;
    } catch (error) {
        loadError.textContent = errorMessage(error);
        retry.hidden = false;
        return false;
    } finally {
        status.textContent = "";
        setBusy(false);
    }
}

async function archive(item) {
    if (busy || confirming) {
        return;
    }
    confirming = true;
    let confirmed;
    try {
        confirmed = await confirmArchive(item.name, {
            heading: "Архивировать ветку?",
            description: `«${item.name}» и все вложенные подразделения будут архивированы. Они останутся доступны через фильтр архива.`,
            confirmLabel: "Архивировать ветку",
        });
    } finally {
        confirming = false;
    }
    if (!confirmed) {
        return;
    }
    setBusy(true);
    loadError.textContent = "";
    try {
        await request(`${listUrl}${item.id}/`, "DELETE", token);
        feedback.textContent = "Ветка перемещена в архив.";
        await reload();
        treeHeading.focus();
    } catch (error) {
        loadError.textContent = errorMessage(error);
        retry.hidden = false;
    } finally {
        setBusy(false);
    }
}

if (form) {
    const parent = form.elements.namedItem("parent_id");
    parent.addEventListener("change", updateMoveAvailability);
    createButton.addEventListener("click", () => openEditor("create", createButton));
    form.addEventListener("submit", async (event) => {
        event.preventDefault();
        if (busy) {
            return;
        }
        formError.textContent = "";
        const payload = {};
        if (operation !== "move") {
            const name = form.elements.namedItem("name");
            payload.name = name.value.trim();
            if (!payload.name || Array.from(payload.name).length > 200) {
                formError.textContent = "Введите название длиной от 1 до 200 символов.";
                name.focus();
                return;
            }
            payload.description = form.elements.namedItem("description").value;
        }
        if (operation !== "update") {
            payload.parent_id = parent.value ? Number(parent.value) : null;
        }
        const creating = operation === "create";
        const moving = operation === "move";
        if (moving && payload.parent_id === original.parent_id) {
            formError.textContent = "Выберите нового родителя.";
            return;
        }

        setBusy(true);
        dialog.setBusy(true);
        try {
            const updated = await request(creating ? listUrl : `${listUrl}${original.id}/`,
                creating ? "POST" : "PATCH", token, payload);
            dialog.close();
            revealPath(updated.parent_id);
            if (creating || moving) {
                search.value = "";
                searchCollapsed.clear();
            }
            feedback.textContent = creating ? "Подразделение создано."
                : moving ? "Подразделение перемещено." : "Изменения сохранены.";
            if (await reload()) {
                const title = document.querySelector(`#department-node-${updated.id} .structure-name`);
                (title ?? treeHeading).focus();
            }
        } catch (error) {
            formError.textContent = errorMessage(error);
        } finally {
            dialog.setBusy(false);
            setBusy(false);
        }
    });
}

search.addEventListener("input", () => {
    searchCollapsed.clear();
    renderTree();
});
filter.addEventListener("change", renderTree);
retry.addEventListener("click", () => reload());
reload();

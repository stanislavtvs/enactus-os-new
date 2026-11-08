const app = document.querySelector("#department-app");
const listUrl = app.dataset.apiUrl;
const tree = document.querySelector("#department-tree");
const treeHeading = document.querySelector("#department-tree-heading");
const status = document.querySelector("#department-status");
const loadError = document.querySelector("#department-load-error");
const retry = document.querySelector("#department-retry");
const filter = document.querySelector("#department-show-archived");
const editor = document.querySelector("#department-editor");
const form = document.querySelector("#department-form");
const formHeading = document.querySelector("#department-form-heading");
const formError = document.querySelector("#department-form-error");
const saveButton = document.querySelector("#department-save");
const cancelButton = document.querySelector("#department-cancel");

const expanded = new Map();
let departments = [];
let editing = null;

function element(tag, className, content) {
    const node = document.createElement(tag);
    node.className = className;
    if (content !== undefined) {
        node.textContent = content;
    }
    return node;
}

function childIndex(items) {
    const index = new Map();
    const ids = new Set(items.map((item) => item.id));
    for (const item of items) {
        const key = ids.has(item.parent_id) ? item.parent_id : null;
        if (!index.has(key)) {
            index.set(key, []);
        }
        index.get(key).push(item);
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

function action(label, item, callback) {
    const button = element("button", "button department-secondary", label);
    button.type = "button";
    button.setAttribute("aria-label", `${label}: ${item.name}`);
    button.addEventListener("click", () => callback(item));
    return button;
}

function disclosure(item, branch) {
    const button = element("button", "department-toggle");
    button.type = "button";
    button.setAttribute("aria-controls", branch.id);
    const toggle = (open) => {
        branch.hidden = !open;
        button.textContent = open ? "▾" : "▸";
        button.setAttribute("aria-expanded", String(open));
        button.setAttribute("aria-label", `${open ? "Свернуть" : "Развернуть"}: ${item.name}`);
    };
    toggle(expanded.get(item.id) ?? true);
    button.addEventListener("click", () => {
        const open = branch.hidden;
        expanded.set(item.id, open);
        toggle(open);
    });
    return button;
}

function renderTree() {
    const visible = departments.filter((item) => filter.checked || !item.is_archived);
    const index = childIndex(visible);
    const root = element("ul", "department-list department-list-root");
    root.setAttribute("role", "list");
    const lists = new Map([[null, root]]);
    const visibleIds = new Set(visible.map((item) => item.id));
    filter.closest("label").hidden = !departments.some((item) => item.is_archived);

    for (const {item, depth} of walk(index)) {
        const entry = document.createElement("li");
        const row = element("div", "department-row");
        row.style.setProperty("--department-depth", depth);

        const info = element("div", "department-info");
        const title = element("p", "department-name", item.name);
        if (item.is_archived) {
            title.append(element("span", "department-archived", "Архив"));
        }
        info.append(title);
        if (item.description) {
            info.append(element("p", "department-description", item.description));
        }

        entry.append(row);
        const list = lists.get(visibleIds.has(item.parent_id) ? item.parent_id : null);
        list.append(entry);

        if (index.has(item.id)) {
            const branch = element("ul", "department-list");
            branch.id = `department-children-${item.id}`;
            branch.setAttribute("role", "list");
            lists.set(item.id, branch);
            row.append(disclosure(item, branch));
            entry.append(branch);
        } else {
            const placeholder = element("span", "department-toggle-placeholder");
            placeholder.setAttribute("aria-hidden", "true");
            row.append(placeholder);
        }

        row.append(info);
        if (form && !item.is_archived) {
            const actions = element("div", "department-actions");
            actions.append(action("Редактировать", item, edit), action("Архивировать ветку", item, archive));
            row.append(actions);
        }
    }

    const emptyMessage = "Пока нет подразделений для отображения.";
    tree.replaceChildren(root.childElementCount ? root : element("p", "muted department-message", emptyMessage));
}

function parentChoices(selected) {
    const index = childIndex(departments.filter((item) => !item.is_archived));
    const excluded = new Set();
    if (editing !== null) {
        const stack = [editing];
        while (stack.length) {
            const id = stack.pop();
            excluded.add(id);
            for (const child of index.get(id) ?? []) {
                stack.push(child.id);
            }
        }
    }

    const select = form.elements.namedItem("parent_id");
    select.replaceChildren(new Option("Без родительского подразделения", ""));
    for (const {item, depth} of walk(index)) {
        if (!excluded.has(item.id)) {
            const prefix = depth ? `${"— ".repeat(Math.min(depth, 3))}` : "";
            select.add(new Option(prefix + item.name, String(item.id)));
        }
    }
    select.value = selected === null ? "" : String(selected);
}

function edit(item) {
    editing = item.id;
    formHeading.textContent = "Редактирование подразделения";
    formError.textContent = "";
    form.elements.namedItem("name").value = item.name;
    form.elements.namedItem("description").value = item.description;
    parentChoices(item.parent_id);
    saveButton.textContent = "Сохранить";
    cancelButton.hidden = false;
    form.elements.namedItem("name").focus();
}

function resetEditor() {
    if (!form) {
        return;
    }
    editing = null;
    formHeading.textContent = "Новое подразделение";
    form.elements.namedItem("name").value = "";
    form.elements.namedItem("description").value = "";
    formError.textContent = "";
    parentChoices(null);
    saveButton.textContent = "Создать";
    cancelButton.hidden = true;
}

function setBusy(busy) {
    tree.setAttribute("aria-busy", String(busy));
    filter.disabled = busy;
    for (const button of tree.querySelectorAll("button")) {
        button.disabled = busy;
    }
    if (form) {
        form.querySelector("fieldset").disabled = busy;
    }
}

async function request(method, id = null, payload = null) {
    const headers = {Accept: "application/json"};
    if (method !== "GET") {
        headers["X-CSRFToken"] = form.elements.namedItem("csrfmiddlewaretoken").value;
    }
    if (payload !== null) {
        headers["Content-Type"] = "application/json";
    }
    const url = id === null ? listUrl : `${listUrl}${id}/`;
    const response = await fetch(url, {
        method,
        headers,
        mode: "same-origin",
        body: payload === null ? undefined : JSON.stringify(payload),
    });
    if (!response.ok) {
        const errors = {
            401: "Сессия завершилась. Войдите снова.",
            403: "Не удалось подтвердить доступ. Обновите страницу и проверьте права.",
            404: "Запись недоступна. Обновите структуру.",
            409: "Не удалось применить изменение. Проверьте состояние подразделения и его родителя.",
            422: "Проверьте название и выбранное родительское подразделение.",
        };
        const data = await response.json().catch(() => null);
        const detail = response.status === 409 && typeof data?.detail === "string" ? data.detail : "";
        throw new Error(detail || errors[response.status] || "Не удалось выполнить запрос.");
    }
    return response.status === 204 ? null : response.json();
}

async function reload(message = "") {
    setBusy(true);
    loadError.textContent = "";
    retry.hidden = true;
    status.textContent = message || "Загружаем структуру…";

    try {
        departments = await request("GET");
        renderTree();
        resetEditor();
        if (editor) {
            editor.hidden = false;
        }
        status.textContent = message;
        return true;
    } catch (error) {
        status.textContent = "";
        loadError.textContent = message
            ? `${message} Обновите структуру, чтобы увидеть результат.`
            : error instanceof TypeError
                ? "Не удалось загрузить структуру. Проверьте соединение и повторите попытку."
                : error.message;
        retry.hidden = false;
        return false;
    } finally {
        setBusy(false);
    }
}

async function mutate(method, id, payload, message, errorElement) {
    setBusy(true);
    errorElement.textContent = "";
    try {
        await request(method, id, payload);
    } catch (error) {
        errorElement.textContent = error instanceof TypeError
            ? "Соединение прервалось. Обновите структуру перед повторной попыткой."
            : error.message;
        retry.hidden = false;
        setBusy(false);
        return;
    }
    if (await reload(message)) {
        const focusTarget = method === "DELETE" ? treeHeading : form.elements.namedItem("name");
        focusTarget.focus();
    }
}

function archive(item) {
    const question = `Архивировать «${item.name}» вместе со всеми вложенными подразделениями? Ветка останется доступна при включённом показе архива.`;
    if (window.confirm(question)) {
        return mutate("DELETE", item.id, null, "Ветка перемещена в архив.", loadError);
    }
}

if (form) {
    form.addEventListener("submit", (event) => {
        event.preventDefault();
        const nameInput = form.elements.namedItem("name");
        const name = nameInput.value.trim();
        if (!name || Array.from(name).length > 200) {
            formError.textContent = "Введите название длиной от 1 до 200 символов.";
            nameInput.focus();
            return;
        }
        const parent = form.elements.namedItem("parent_id").value;
        const payload = {
            name,
            description: form.elements.namedItem("description").value,
            parent_id: parent ? Number(parent) : null,
        };
        if (payload.parent_id !== null) {
            expanded.set(payload.parent_id, true);
        }
        const creating = editing === null;
        const message = creating ? "Подразделение создано." : "Изменения сохранены.";
        return mutate(creating ? "POST" : "PATCH", editing, payload, message, formError);
    });
    cancelButton.addEventListener("click", () => {
        resetEditor();
        form.elements.namedItem("name").focus();
    });
}

filter.addEventListener("change", renderTree);
retry.addEventListener("click", () => reload());
reload();

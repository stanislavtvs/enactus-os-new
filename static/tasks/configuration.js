import {collectionActions, confirmArchive, setupDialog, setupDisclosures} from "../ui/collection.js";
import {setupStatusConfiguration} from "./status-configuration.js";
import {element, errorMessage, priorityChip, request} from "./shared.js";

const app = document.querySelector("#task-configuration-app");
const endpoint = `${app.dataset.orgUrl}task-priorities/`;
const token = app.querySelector('[name="csrfmiddlewaretoken"]').value;
const list = document.querySelector("#task-priorities-list");
const heading = document.querySelector("#task-priorities-heading");
const notice = document.querySelector("#task-priorities-notice");
const status = document.querySelector("#task-priorities-status");
const errorBox = document.querySelector("#task-priorities-error");
const retry = document.querySelector("#task-priorities-retry");
const add = document.querySelector("#task-add-priority");
const form = document.querySelector("#task-priority-form");
const formError = document.querySelector("#task-priority-form-error");
const codeField = document.querySelector("#task-priority-code-field");
const dialog = setupDialog(document.querySelector("#task-priority-dialog"));
const standardCodes = new Set(["low", "normal", "high"]);

let data;
let editing;
let busy = false;

setupDisclosures(app);
setupStatusConfiguration(app, token);

function field(name) {
    return form.elements.namedItem(name);
}

function render() {
    list.replaceChildren();
    for (const item of data.items) {
        const row = element("li", "task-row task-configuration-item");
        const summary = element("div", "");
        const label = priorityChip(item);
        label.classList.add("task-configuration-label");
        const details = [`Ранг ${item.rank}`, `порядок ${item.sort_order}`, `код ${item.code}`];
        if (item.is_default) {
            details.push("По умолчанию");
        }
        if (!item.is_active) {
            details.push("Архив");
        }
        summary.append(label, element("p", "task-meta task-configuration-meta", details.join(" · ")));
        row.append(summary);
        if (data.can_manage && item.is_active) {
            const actions = [{label: "Редактировать", onSelect: (trigger) => openEditor(trigger, item)}];
            if (!item.is_default) {
                actions.push({label: "Назначить по умолчанию", onSelect: () => mutate(
                    item.id, "PATCH", {is_default: true}, "Приоритет по умолчанию обновлён.",
                )});
                if (!standardCodes.has(item.code)) {
                    actions.push({label: "Архивировать", danger: true, onSelect: () => archive(item)});
                }
            }
            row.append(collectionActions(item.name, actions));
        }
        list.append(row);
    }
    add.hidden = !data.can_manage;
    status.textContent = data.items.length ? "" : "Приоритетов пока нет.";
}

function setBusy(value) {
    busy = value;
    add.disabled = value;
    retry.disabled = value;
    list.inert = value;
    list.setAttribute("aria-busy", String(value));
}

async function load() {
    setBusy(true);
    status.textContent = "Загружаем приоритеты…";
    errorBox.textContent = "";
    retry.hidden = true;
    try {
        data = await request(endpoint);
        render();
        return true;
    } catch (error) {
        status.textContent = "";
        errorBox.textContent = errorMessage(error);
        retry.hidden = false;
        return false;
    } finally {
        setBusy(false);
    }
}

function openEditor(trigger, item = null) {
    if (busy || !data.can_manage) {
        return;
    }
    editing = item;
    form.reset();
    formError.textContent = "";
    document.querySelector("#task-priority-dialog-heading").textContent = item ? "Редактировать приоритет" : "Новый приоритет";
    field("name").value = item?.name ?? "";
    field("code").value = item?.code ?? "";
    field("code").readOnly = Boolean(item);
    field("code").required = !item;
    codeField.hidden = false;
    field("rank").value = item?.rank ?? 100;
    const maxOrder = data.items.reduce((current, priority) => Math.max(current, priority.sort_order), 0);
    field("sort_order").value = item?.sort_order ?? Math.min(2147483647, maxOrder + 1);
    field("color").value = item?.color ?? "#6b7280";
    dialog.open(trigger, field("name"));
}

async function mutate(id, method, payload, message, fromDialog = false) {
    if (busy) {
        return;
    }
    setBusy(true);
    errorBox.textContent = "";
    notice.textContent = "";
    if (fromDialog) {
        dialog.setBusy(true);
    }
    try {
        await request(id ? `${endpoint}${id}/` : endpoint, method, token, payload);
        if (fromDialog) {
            dialog.close();
        }
        notice.textContent = message;
        if (await load()) {
            heading.focus();
        }
    } catch (error) {
        (fromDialog ? formError : errorBox).textContent = errorMessage(error);
    } finally {
        setBusy(false);
        if (fromDialog) {
            dialog.setBusy(false);
        }
    }
}

async function archive(item) {
    if (busy) {
        return;
    }
    if (await confirmArchive(item.name, {
        heading: "Архивировать приоритет?",
        description: `«${item.name}» исчезнет из списка выбора для новых задач. Архивировать можно только неиспользуемый уровень.`,
        confirmLabel: "Архивировать",
    })) {
        return mutate(item.id, "DELETE", null, "Приоритет архивирован.");
    }
}

form.addEventListener("submit", (event) => {
    event.preventDefault();
    formError.textContent = "";
    const name = field("name").value.trim();
    const code = field("code").value.trim();
    if (!name || Array.from(name).length > 120) {
        formError.textContent = "Введите название длиной от 1 до 120 символов.";
        field("name").focus();
        return;
    }
    if (!editing && !/^[a-z0-9]+(?:[-_][a-z0-9]+)*$/.test(code)) {
        formError.textContent = "Введите технический код строчными латинскими буквами и цифрами.";
        field("code").focus();
        return;
    }
    const payload = {
        name,
        rank: Number(field("rank").value),
        sort_order: Number(field("sort_order").value),
        color: field("color").value,
    };
    if (!editing) {
        payload.code = code;
    }
    return mutate(editing?.id, editing ? "PATCH" : "POST", payload,
        editing ? "Приоритет обновлён." : "Приоритет создан.", true);
});

add.addEventListener("click", () => openEditor(add));
retry.addEventListener("click", load);
load();

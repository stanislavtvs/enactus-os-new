import {element, errorMessage, request} from "../departments/directory.js";
import {setupDialog} from "../ui/collection.js";

const app = document.querySelector("#modules-app");
const get = (id) => app.querySelector(`#modules-${id}`);
const api = app.dataset.apiUrl;
const token = app.querySelector("[name='csrfmiddlewaretoken']").value;
const dialog = get("dialog");
const modal = setupDialog(dialog);
const retry = get("retry");
let catalog = new Map();
let currentChanges = new Map();
let loading = false;
let saving = false;

function planChange(item, enabled) {
    const changes = new Map();
    const queue = [item];
    for (let index = 0; index < queue.length; index++) {
        const current = queue[index];
        if (changes.has(current.code) || current.enabled === enabled) {
            continue;
        }
        if (!current.configurable || (!enabled && current.required)) {
            throw new Error(`Сначала проверьте зависимость: «${current.name}» нельзя переключить.`);
        }
        changes.set(current.code, enabled);
        if (enabled) {
            queue.push(...current.dependencies.map((code) => catalog.get(code)).filter(Boolean));
        } else {
            queue.push(...[...catalog.values()].filter((candidate) => candidate.dependencies.includes(current.code)));
        }
    }
    return changes;
}

function openChange(item, trigger) {
    const enable = !item.enabled;
    get("error").textContent = "";
    try {
        currentChanges = planChange(item, enable);
    } catch (validationError) {
        get("error").textContent = validationError.message;
        return;
    }
    get("dialog-title").textContent = enable ? "Включить модуль?" : "Отключить модуль?";
    get("dialog-summary").textContent = enable
        ? "Участники с соответствующими правами получат доступ к указанным модулям."
        : "Указанные разделы и действия станут недоступны в этой организации. Сохранённые данные снова будут доступны после включения модулей.";
    get("dialog-changes").replaceChildren(...[...currentChanges.keys()].map((code) => element("li", "", catalog.get(code).name)));
    get("confirm").textContent = enable ? "Включить" : "Отключить";
    get("dialog-error").textContent = "";
    modal.open(trigger, get("cancel"));
}

function card(item, canManage) {
    const node = element("li", "modules-card");
    node.dataset.moduleCode = item.code;
    node.dataset.enabled = String(item.enabled);
    const heading = element("div", "modules-card-heading");
    heading.append(element("h3", "", item.name), element("span", "modules-state", item.enabled ? "Включён" : "Отключён"));
    node.append(heading, element("p", "", item.description));
    if (item.dependencies.length) {
        const dependencies = item.dependencies.map((code) => catalog.get(code)?.name ?? code);
        node.append(element("p", "muted modules-dependencies", `Зависимости: ${dependencies.join(", ")}`));
    }
    if (canManage && item.configurable && !item.required) {
        const label = item.enabled ? "Отключить" : "Включить";
        const action = element("button", "button collection-secondary", label);
        action.type = "button";
        action.dataset.moduleAction = item.enabled ? "disable" : "enable";
        action.setAttribute("aria-label", `${label} модуль «${item.name}»`);
        action.setAttribute("aria-haspopup", "dialog");
        action.addEventListener("click", () => openChange(item, action));
        node.append(action);
    }
    return node;
}

function render({items, can_manage: canManage}) {
    catalog = new Map(items.map((item) => [item.code, item]));
    for (const [name, required] of [["optional", false], ["required", true]]) {
        const rows = items.filter((item) => item.required === required).map((item) => card(item, canManage));
        get(`${name}-list`).replaceChildren(...rows);
        get(`${name}-section`).hidden = !rows.length;
    }
    get("status").textContent = canManage ? "" : "Каталог доступен для просмотра.";
}

async function load() {
    if (loading) {
        return;
    }
    loading = true;
    retry.hidden = true;
    get("status").textContent = "Загрузка модулей…";
    get("error").textContent = "";
    try {
        render(await request(api));
    } catch (requestError) {
        get("status").textContent = "";
        get("error").textContent = errorMessage(requestError);
        get("optional-section").hidden = true;
        get("required-section").hidden = true;
        retry.hidden = false;
    } finally {
        loading = false;
    }
}

get("change-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (saving || !currentChanges.size) {
        return;
    }
    saving = true;
    modal.setBusy(true);
    get("dialog-error").textContent = "";
    try {
        await request(api, "PATCH", token, {changes: Object.fromEntries(currentChanges)});
        window.location.reload();
    } catch (requestError) {
        get("dialog-error").textContent = errorMessage(requestError);
        retry.hidden = false;
        modal.setBusy(false);
        saving = false;
    }
});

retry.addEventListener("click", load);
load();

import {element, errorMessage, request} from "../departments/directory.js";
import {getUnreadCount, updateUnreadCount} from "./badge.js";

const app = document.querySelector("#notification-center");
const get = (id) => app.querySelector(`#notification-${id}`);
const api = app.dataset.apiUrl;
const token = app.querySelector("[name='csrfmiddlewaretoken']").value;
const list = get("list");
const category = get("category");
const organization = get("organization");
const readAll = get("read-all");
const status = get("status");
const error = get("error");
const retry = get("retry");
const controls = [...app.querySelectorAll("[data-notification-mode]")];
const categories = {
    tasks: {label: "Задачи и поручения", icon: "tasks"},
    reviews: {label: "Согласования", icon: "access"},
    projects: {label: "Проекты и запросы", icon: "projects"},
    calendar: {label: "Встречи и мероприятия", icon: "calendar"},
    organization: {label: "Организации и приглашения", icon: "org"},
};
const dateFormat = new Intl.DateTimeFormat("ru", {dateStyle: "medium", timeStyle: "short"});
const busyRows = new Set();
let page = 1;
let mode = "all";
let latestRequest = 0;
let lastList = "";
let lastOrganizations = "";
let bulkPending = false;
let loading = false;

for (const [value, {label}] of Object.entries(categories)) {
    category.add(new Option(label, value));
}

function showCount(count) {
    get("unread-count").textContent = count === null ? "Непрочитанные уведомления" : `Непрочитанных: ${count}`;
    readAll.disabled = !count || bulkPending;
}

function filterParams() {
    const params = new URLSearchParams();
    if (category.value) {
        params.set("category", category.value);
    }
    if (organization.value) {
        params.set("org_id", organization.value);
    }
    return params;
}

function replaceOrganizations(records) {
    const signature = JSON.stringify(records);
    if (signature === lastOrganizations) {
        return;
    }
    lastOrganizations = signature;
    const selected = organization.value;
    organization.replaceChildren(new Option("Все организации", ""), ...records.map(({id, name}) => new Option(name, id)));
    if (selected && records.some(({id}) => String(id) === selected)) {
        organization.value = selected;
    } else if (selected) {
        // The organization no longer belongs to the recipient's available filter options.
        page = 1;
        load();
    }
}

function icon(name) {
    const node = element("span", "notification-category-icon");
    const namespace = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(namespace, "svg");
    svg.setAttribute("class", "icon");
    svg.setAttribute("aria-hidden", "true");
    if (name === "tasks") {
        const path = document.createElementNS(namespace, "path");
        svg.setAttribute("viewBox", "0 0 24 24");
        path.setAttribute("d", "m3 7 2 2 4-4m2 3h10M3 14h5m3 0h10M3 20h5m3 0h10");
        svg.append(path);
    } else {
        const use = document.createElementNS(namespace, "use");
        use.setAttribute("href", `#icon-${name}`);
        svg.append(use);
    }
    node.append(svg);
    return node;
}

function button(label, action) {
    const node = element("button", "button collection-secondary", label);
    node.type = "button";
    node.dataset.notificationAction = action;
    return node;
}

function renderRow(row) {
    const item = element("li", "notification-row");
    item.dataset.notificationId = row.id;
    item.dataset.unread = String(!row.read_at);
    const kind = categories[row.category] ?? {label: "Уведомление", icon: "invite"};
    const copy = element("div", "notification-copy");
    const heading = element("h2", "", row.title);
    const details = element("p", "notification-details");
    const time = element("time", "", dateFormat.format(new Date(row.created_at)));
    time.dateTime = row.created_at;
    details.append(element("span", "", kind.label));
    if (row.organization_name) {
        details.append(element("span", "", row.organization_name));
    }
    details.append(time);
    if (!row.read_at) {
        details.append(element("span", "notification-unread", "Новое"));
    }
    copy.append(heading, details);
    if (row.description) {
        copy.append(element("p", "", row.description));
    }
    item.append(icon(kind.icon), copy);

    const actions = element("div", "notification-actions");
    if (row.available) {
        const open = button("Открыть", "open");
        open.setAttribute("aria-label", `Открыть: ${row.title}`);
        open.addEventListener("click", () => act(row, item, "open"));
        actions.append(open);
    }
    if (!row.read_at) {
        const mark = button("Прочитано", "read");
        mark.setAttribute("aria-label", `Отметить прочитанным: ${row.title}`);
        mark.addEventListener("click", () => act(row, item, "read"));
        actions.append(mark);
    }
    if (actions.children.length) {
        item.append(actions);
    }
    return item;
}

function restoreFocus(id, action, index) {
    const row = list.querySelector(`[data-notification-id="${id}"]`);
    const control = row?.querySelector(`[data-notification-action="${action}"]`) ?? row?.querySelector("button")
        ?? list.children[index]?.querySelector("button")
        ?? controls.find((button) => button.dataset.notificationMode === mode);
    control?.focus();
}

function renderRows(items) {
    const signature = JSON.stringify(items);
    if (signature === lastList) {
        return;
    }
    lastList = signature;
    const focused = document.activeElement;
    const previousRow = focused?.closest(".notification-row");
    const index = [...list.children].indexOf(previousRow);
    list.replaceChildren(...items.map(renderRow));
    if (index >= 0) {
        restoreFocus(previousRow.dataset.notificationId, focused.dataset.notificationAction, index);
    }
}

async function load({background = false} = {}) {
    const generation = ++latestRequest;
    const previousFocus = document.activeElement;
    const params = filterParams();
    params.set("page", page);
    if (mode === "unread") {
        params.set("unread", "true");
    }
    loading = true;
    retry.hidden = true;
    error.textContent = "";
    if (!background) {
        status.textContent = "Загрузка уведомлений…";
    }
    list.inert = !background;
    list.setAttribute("aria-busy", "true");
    try {
        const result = await request(`${api}?${params}`);
        if (generation !== latestRequest) {
            return;
        }
        replaceOrganizations(result.organizations);
        if (generation !== latestRequest) {
            return;
        }
        const pages = Math.max(1, Math.ceil(result.total / result.page_size));
        if (page > pages) {
            page = pages;
            load({background});
            return;
        }
        page = result.page;
        list.inert = false;
        renderRows(result.items);
        status.textContent = result.items.length ? ""
            : mode === "unread" ? "Непрочитанных уведомлений нет." : "Уведомлений в этой выборке пока нет.";
        get("pagination").hidden = pages <= 1;
        get("page").textContent = `Страница ${page} из ${pages}`;
        get("previous").disabled = page <= 1;
        get("next").disabled = page >= pages;
        if (document.activeElement === document.body && !background && get("pagination").contains(previousFocus)) {
            const nextFocus = [get("next"), get("previous")].find((button) => !button.disabled) ?? get("heading");
            nextFocus.focus();
        }
    } catch (requestError) {
        if (generation === latestRequest) {
            list.inert = false;
            renderRows([]);
            get("pagination").hidden = true;
            status.textContent = "";
            error.textContent = errorMessage(requestError);
            retry.hidden = false;
        }
    } finally {
        if (generation === latestRequest) {
            loading = false;
            list.inert = false;
            list.removeAttribute("aria-busy");
        }
    }
}

function safePath(value) {
    if (typeof value !== "string" || !value.startsWith("/") || value.startsWith("//")) {
        throw new Error("Связанный объект недоступен.");
    }
    const url = new URL(value, window.location.origin);
    if (url.origin !== window.location.origin || url.pathname.startsWith("//")) {
        throw new Error("Связанный объект недоступен.");
    }
    return url.pathname + url.search + url.hash;
}

async function act(row, item, action) {
    if (busyRows.has(row.id) || bulkPending) {
        return;
    }
    busyRows.add(row.id);
    error.textContent = "";
    get("feedback").textContent = "";
    const index = [...list.children].indexOf(item);
    item.querySelectorAll("button").forEach((control) => { control.disabled = true; });
    try {
        const opening = action === "open";
        const path = opening ? safePath((await request(`${api}${row.id}/link/`)).url) : "";
        if (!row.read_at) {
            const {unread} = await request(`${api}${row.id}/read/`, "POST", token);
            updateUnreadCount(unread);
        }
        if (opening) {
            window.location.assign(path);
            return;
        }
        get("feedback").textContent = "Уведомление отмечено как прочитанное.";
        await load({background: true});
        if (document.activeElement === document.body || item.contains(document.activeElement)) {
            restoreFocus(row.id, action, index);
        }
    } catch (requestError) {
        error.textContent = errorMessage(requestError);
        item.querySelectorAll("button").forEach((control) => { control.disabled = false; });
        if (document.activeElement === document.body || item.contains(document.activeElement)) {
            item.querySelector(`[data-notification-action="${action}"]`)?.focus();
        }
    } finally {
        busyRows.delete(row.id);
        item.querySelectorAll("button").forEach((control) => { control.disabled = false; });
    }
}

readAll.addEventListener("click", async () => {
    if (bulkPending || busyRows.size) {
        return;
    }
    bulkPending = true;
    showCount(getUnreadCount());
    error.textContent = "";
    get("feedback").textContent = "";
    try {
        const params = filterParams();
        const query = params.size ? `?${params}` : "";
        const {unread, updated} = await request(`${api}read-all/${query}`, "POST", token);
        updateUnreadCount(unread);
        get("feedback").textContent = updated ? `Отмечено как прочитанные: ${updated}.` : "В этой выборке нет непрочитанных уведомлений.";
        await load({background: true});
    } catch (requestError) {
        error.textContent = errorMessage(requestError);
    } finally {
        bulkPending = false;
        showCount(getUnreadCount());
        if (document.activeElement === document.body) {
            (readAll.disabled ? controls.find((control) => control.dataset.notificationMode === mode) : readAll).focus();
        }
    }
});

function filterChanged() {
    page = 1;
    renderRows([]);
    get("feedback").textContent = "";
    readAll.textContent = filterParams().size ? "Отметить все в этой выборке" : "Отметить все как прочитанные";
    load();
}
for (const control of controls) {
    control.addEventListener("click", () => {
        mode = control.dataset.notificationMode;
        controls.forEach((button) => button.setAttribute("aria-pressed", String(button === control)));
        filterChanged();
    });
}
category.addEventListener("change", filterChanged);
organization.addEventListener("change", filterChanged);
for (const [id, offset] of [["previous", -1], ["next", 1]]) {
    get(id).addEventListener("click", () => {
        if (!loading) {
            page += offset;
            load();
        }
    });
}
retry.addEventListener("click", () => load());
document.addEventListener("notifications:count", (event) => showCount(event.detail));
document.addEventListener("notifications:refresh", () => {
    if (!loading && !bulkPending && !busyRows.size) {
        load({background: true});
    }
});
showCount(getUnreadCount());
load();

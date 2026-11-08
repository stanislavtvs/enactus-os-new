import {element, errorMessage, request} from "../departments/directory.js";
import {confirmArchive, setupDialog, setupDisclosures} from "../ui/collection.js";
import {addDays, calendarPeriod, civilLabel, localDay, localTime, movePeriod, offsetLabel, validZone} from "./dates.js";
import {httpLink, setupEditor} from "./editor.js";
import {renderCalendar, sourceLabel} from "./views.js";

const app = document.querySelector("#calendar-app");
const get = (id) => app.querySelector(`#calendar-${id}`);
const api = app.dataset.orgUrl;
const token = app.querySelector("[name='csrfmiddlewaretoken']").value;
const view = get("view");
const periodHeading = get("period");
const create = get("create");
const browserZone = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
const modeButtons = [...app.querySelectorAll("[data-calendar-mode]")];
const timezoneField = get("timezone");
const filterFields = {
    source: get("source-filter"),
    project_id: get("project-filter"),
    department_id: get("department-filter"),
    include_archived: get("archive-filter"),
};

function preference(key, value) {
    try {
        if (value === undefined) {
            return localStorage.getItem(`enactus-calendar-${key}`);
        }
        localStorage.setItem(`enactus-calendar-${key}`, value);
    } catch {
        // Preferences remain usable for the current page without browser storage.
    }
    return null;
}

const savedMode = preference("view");
const savedZone = preference("zone");
let mode = modeButtons.some((button) => button.dataset.calendarMode === savedMode) ? savedMode : "month";
let zone = validZone(savedZone) ? savedZone : browserZone;
let anchor = localDay(Date.now(), zone);
let scopeOptions = {projects: [], departments: [], can_create: false, can_create_organization: false};
let scopesReady = false;
let loadGeneration = 0;

timezoneField.value = zone;
get("timezones").replaceChildren(...[...new Set(["UTC", browserZone, ...(Intl.supportedValuesOf?.("timeZone") ?? [])])]
    .sort().map((value) => new Option(value, value)));

function appendOptions(select, records) {
    for (const {id, name} of records) {
        select.add(new Option(name, id));
    }
}

async function loadScopes() {
    scopeOptions = await request(`${api}calendar/filters/`);
    appendOptions(filterFields.project_id, scopeOptions.projects);
    appendOptions(filterFields.department_id, scopeOptions.departments);
    create.hidden = !scopeOptions.can_create;
    scopesReady = true;
}

async function loadCalendar() {
    const generation = ++loadGeneration;
    const period = calendarPeriod(mode, anchor);
    const params = new URLSearchParams({start: period.start, end: period.end, timezone: zone});
    for (const [key, field] of Object.entries(filterFields)) {
        if (field.type === "checkbox" ? field.checked : field.value) {
            params.set(key, field.type === "checkbox" ? "true" : field.value);
        }
    }
    for (const button of modeButtons) {
        button.setAttribute("aria-pressed", String(button.dataset.calendarMode === mode));
    }
    periodHeading.textContent = period.label;
    get("period-zone").textContent = zone;
    get("error").textContent = "";
    get("retry").hidden = true;
    get("status").textContent = "Загрузка календаря…";
    view.setAttribute("aria-busy", "true");
    view.replaceChildren();
    try {
        const {items} = await request(`${api}calendar/?${params}`);
        if (generation !== loadGeneration) {
            return;
        }
        renderCalendar(view, mode, period, items, {
            zone, anchor, today: localDay(Date.now(), zone), selectDay, openEvent,
        });
        get("status").textContent = !items.length && mode !== "day" ? "В этом периоде записей нет." : "";
    } catch (error) {
        if (generation === loadGeneration) {
            get("status").textContent = "";
            get("error").textContent = errorMessage(error);
            get("retry").hidden = false;
        }
    } finally {
        if (generation === loadGeneration) {
            view.removeAttribute("aria-busy");
        }
    }
}

function selectDay(date, focusHeading = false) {
    anchor = date;
    mode = "day";
    preference("view", mode);
    loadCalendar();
    if (focusHeading) {
        periodHeading.focus();
    }
}

const editor = setupEditor({
    app, api, token, scopes: () => scopeOptions, zone: () => zone, day: () => anchor, onSaved: announceMutation,
});

function announceMutation(message) {
    get("feedback").textContent = message;
    loadCalendar();
    periodHeading.focus();
}

const detail = get("event-detail");
const detailModal = setupDialog(detail);
const detailButtons = ["edit", "cancel", "archive"].map((action) => get(`event-${action}`));
let detailGeneration = 0;
let activeEvent = null;
let activeTrigger = null;
let detailBusy = false;

function eventFact(label, value) {
    if (!value) {
        return;
    }
    const fact = element("div", "");
    const definition = element("dd", "");
    definition.append(value);
    fact.append(element("dt", "", label), definition);
    get("detail-facts").append(fact);
}

function formatInstant(instant, eventZone) {
    return `${civilLabel(localDay(instant, eventZone), {dateStyle: "medium"})}, ${localTime(instant, eventZone)} (${offsetLabel(instant, eventZone)})`;
}

function displayEvent(event) {
    get("event-detail-heading").textContent = event.title;
    eventFact("Тип", sourceLabel(event.source));
    eventFact("Статус", event.status_label);
    if (event.all_day) {
        const first = localDay(event.starts_at, event.timezone);
        const last = addDays(localDay(event.ends_at, event.timezone), -1);
        eventFact("Весь день", first === last ? civilLabel(first) : `${civilLabel(first)} — ${civilLabel(last)}`);
    } else {
        eventFact("Начало", formatInstant(event.starts_at, zone));
        eventFact("Окончание", formatInstant(event.ends_at, zone));
        if (zone !== event.timezone) {
            eventFact("Время события", `${formatInstant(event.starts_at, event.timezone)} — ${formatInstant(event.ends_at, event.timezone)}`);
        }
    }
    eventFact("Часовой пояс события", event.timezone);
    eventFact("Проект", scopeOptions.projects.find(({id}) => id === event.project_id)?.name);
    eventFact("Подразделение", scopeOptions.departments.find(({id}) => id === event.department_id)?.name);
    eventFact("Место", event.location);
    const link = httpLink(event.online_url);
    if (link) {
        const anchor = element("a", "", "Открыть ссылку");
        anchor.href = link;
        anchor.target = "_blank";
        anchor.rel = "noopener noreferrer";
        eventFact("Онлайн-встреча", anchor);
    }
    get("detail-description").textContent = event.description;
    detailButtons.forEach((button, index) => {
        button.hidden = ![event.can_edit, event.can_cancel, event.can_archive][index];
    });
    get("detail-actions").hidden = !detailButtons.some((button) => !button.hidden);
}

async function openEvent(event, trigger) {
    const generation = ++detailGeneration;
    activeEvent = null;
    activeTrigger = trigger;
    get("event-detail-heading").textContent = event.title;
    get("detail-facts").replaceChildren();
    get("detail-description").textContent = "";
    get("detail-actions").hidden = true;
    get("detail-error").textContent = "";
    get("detail-status").textContent = "Загрузка события…";
    detailModal.open(trigger, detail.querySelector("[data-dialog-close]"));
    try {
        const record = await request(`${api}events/${event.id}/`);
        if (detail.open && generation === detailGeneration) {
            activeEvent = record;
            displayEvent(record);
        }
    } catch (error) {
        if (detail.open && generation === detailGeneration) {
            get("detail-error").textContent = errorMessage(error);
        }
    } finally {
        if (detail.open && generation === detailGeneration) {
            get("detail-status").textContent = "";
        }
    }
}

detailButtons[0].addEventListener("click", () => {
    const record = activeEvent;
    if (record?.can_edit) {
        detailModal.close();
        editor.open(activeTrigger, record);
    }
});

async function transition(action, button) {
    const record = activeEvent;
    if (detailBusy || !record) {
        return;
    }
    detailBusy = true;
    try {
        const cancelling = action === "cancel";
        const approved = await confirmArchive(record.title, cancelling ? {
            heading: "Отменить событие?",
            description: `«${record.title}» останется в календаре с отметкой об отмене.`,
            confirmLabel: "Отменить событие",
        } : {heading: "Архивировать событие?"});
        if (!approved) {
            button.focus();
            return;
        }
        get("detail-error").textContent = "";
        detailModal.setBusy(true);
        detailButtons.forEach((control) => { control.disabled = true; });
        await request(`${api}events/${record.id}/${action}/`, "POST", token);
        detailModal.close();
        announceMutation(cancelling ? "Событие отменено." : "Событие перемещено в архив.");
    } catch (error) {
        get("detail-error").textContent = errorMessage(error);
    } finally {
        detailModal.setBusy(false);
        detailButtons.forEach((control) => { control.disabled = false; });
        if (detail.open) {
            button.focus();
        }
        detailBusy = false;
    }
}

detailButtons[1].addEventListener("click", (event) => transition("cancel", event.currentTarget));
detailButtons[2].addEventListener("click", (event) => transition("archive", event.currentTarget));
create.addEventListener("click", () => editor.open(create));

for (const button of modeButtons) {
    button.addEventListener("click", () => {
        mode = button.dataset.calendarMode;
        preference("view", mode);
        loadCalendar();
    });
}
for (const [action, amount] of [["previous", -1], ["next", 1]]) {
    get(action).addEventListener("click", () => {
        anchor = movePeriod(mode, anchor, amount);
        loadCalendar();
    });
}
get("today").addEventListener("click", () => {
    anchor = localDay(Date.now(), zone);
    loadCalendar();
});
for (const field of Object.values(filterFields)) {
    field.addEventListener("change", loadCalendar);
}
timezoneField.addEventListener("change", () => {
    const value = timezoneField.value.trim();
    const valid = validZone(value);
    timezoneField.setAttribute("aria-invalid", String(!valid));
    get("timezone-error").textContent = valid ? "" : "Укажите часовой пояс IANA, например Asia/Almaty.";
    if (valid && value !== zone) {
        zone = value;
        preference("zone", zone);
        loadCalendar();
    }
});

async function start() {
    if (!scopesReady) {
        get("retry").hidden = true;
        get("status").textContent = "Загрузка календаря…";
        try {
            await loadScopes();
        } catch (error) {
            get("status").textContent = "";
            get("error").textContent = errorMessage(error);
            get("retry").hidden = false;
            return;
        }
    }
    get("toolbar").hidden = false;
    await loadCalendar();
}
get("retry").addEventListener("click", start);
setupDisclosures(app);
start();

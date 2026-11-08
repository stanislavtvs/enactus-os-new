import {validZone} from "../calendar_app/dates.js";
import {element, errorMessage, request} from "../departments/directory.js";

const settings = document.querySelector("#notification-settings");
const get = (id) => settings.querySelector(`#notification-settings-${id}`);
const personal = settings.dataset.personal === "true";
const selectOrganization = get("organization");
const form = get("form");
const status = get("status");
const error = get("error");
const retry = get("retry");
const feedback = get("feedback");
const fields = form.elements;
const save = get("save");
const token = fields.csrfmiddlewaretoken.value;
const offsetLabels = {
    0: "В момент наступления срока",
    15: "За 15 минут",
    60: "За час",
    180: "За 3 часа",
    1440: "За сутки",
};
let generation = 0;
let loading = false;
let saving = false;

function endpoint() {
    const organization = personal ? selectOrganization?.value : settings.dataset.organizationId;
    if (!organization) {
        return "";
    }
    const resource = personal ? "notification-preferences" : "notification-policy";
    return `${settings.dataset.apiUrl}${organization}/${resource}/`;
}

function checkbox(name, value, label, checked, disabled = false) {
    const wrap = element("label", "notification-settings-check");
    const input = element("input", "");
    input.type = "checkbox";
    input.name = name;
    input.value = value;
    input.checked = checked;
    input.disabled = disabled;
    wrap.append(input, label);
    return wrap;
}

function renderCategories(data, enabled) {
    const disabledKinds = data.policy?.disabled_kinds ?? [];
    const openGroups = new Set([...get("categories").querySelectorAll("details[open]")].map((details) => details.dataset.category));
    const rows = data.categories.map(({code, label}) => {
        const section = element("div", "notification-settings-category");
        const blocked = personal && !data.policy_enabled_categories.includes(code);
        const text = blocked ? `${label} · Отключено организацией` : label;
        section.append(checkbox("enabled_categories", code, text, enabled.includes(code), blocked));
        const kinds = data.kinds?.filter(({category}) => category === code) ?? [];
        if (kinds.length) {
            const details = element("details", "");
            details.dataset.category = code;
            details.open = openGroups.has(code);
            const summary = element("summary", "", "Типы уведомлений");
            summary.setAttribute("aria-label", `Типы уведомлений: ${label}`);
            details.append(summary, ...kinds.map(({code: kind, label: name}) =>
                checkbox("allowed_kinds", kind, name, !disabledKinds.includes(kind))));
            section.append(details);
        }
        return section;
    });
    get("categories").replaceChildren(...rows);
}

function renderOffsets(id, name, options, selected) {
    get(id).replaceChildren(...options.map((value) =>
        checkbox(name, value, offsetLabels[value] ?? `За ${value} мин.`, selected.includes(value))));
}

function syncQuietHours() {
    const enabled = get("quiet").checked;
    get("quiet-times").hidden = !enabled;
    for (const field of [fields.quiet_start, fields.quiet_end]) {
        field.disabled = !enabled;
        field.required = enabled;
    }
}

function render(data) {
    const value = personal ? data.preferences : data.policy;
    renderCategories(data, value.enabled_categories);
    if (personal) {
        fields.task_reminders.checked = value.task_reminders;
        fields.calendar_reminders.checked = value.calendar_reminders;
        fields.timezone.value = value.timezone;
        fields.quiet_start.value = value.quiet_start?.slice(0, 5) ?? "";
        fields.quiet_end.value = value.quiet_end?.slice(0, 5) ?? "";
        get("quiet").checked = Boolean(value.quiet_start && value.quiet_end);
        syncQuietHours();
    } else {
        renderOffsets("task-offsets", "task_offsets_minutes", data.task_offset_options, value.task_offsets_minutes);
        renderOffsets("calendar-offsets", "calendar_offsets_minutes", data.calendar_offset_options, value.calendar_offsets_minutes);
        for (const name of ["task_overdue", "include_collaborators", "calendar_informational"]) {
            fields[name].checked = value[name];
        }
        fields.all_day_reminder_time.value = value.all_day_reminder_time.slice(0, 5);
    }
}

function values(name, checked = true) {
    return [...form.querySelectorAll(`[name="${name}"]`)]
        .filter((input) => input.checked === checked).map((input) => input.value);
}

function payload() {
    const enabled_categories = values("enabled_categories");
    if (personal) {
        const timezone = fields.timezone.value.trim();
        const quiet = get("quiet").checked;
        const quiet_start = quiet ? fields.quiet_start.value : null;
        const quiet_end = quiet ? fields.quiet_end.value : null;
        if (!validZone(timezone)) {
            throw new Error("Введите часовой пояс IANA, например Asia/Almaty.");
        }
        if (quiet && quiet_start === quiet_end) {
            throw new Error("Начало и конец тихих часов должны различаться.");
        }
        return {
            enabled_categories, timezone, quiet_start, quiet_end,
            task_reminders: fields.task_reminders.checked,
            calendar_reminders: fields.calendar_reminders.checked,
        };
    }
    return {
        enabled_categories,
        disabled_kinds: values("allowed_kinds", false),
        task_offsets_minutes: values("task_offsets_minutes").map(Number),
        calendar_offsets_minutes: values("calendar_offsets_minutes").map(Number),
        task_overdue: fields.task_overdue.checked,
        include_collaborators: fields.include_collaborators.checked,
        calendar_informational: fields.calendar_informational.checked,
        all_day_reminder_time: fields.all_day_reminder_time.value,
    };
}

async function load() {
    const url = endpoint();
    if (!url) {
        return;
    }
    const revision = ++generation;
    loading = true;
    form.hidden = true;
    retry.hidden = true;
    error.textContent = "";
    feedback.textContent = "";
    get("categories").replaceChildren();
    status.textContent = "Загрузка настроек…";
    try {
        const result = await request(url);
        if (revision === generation) {
            render(result);
            form.hidden = false;
        }
    } catch (requestError) {
        if (revision === generation) {
            error.textContent = errorMessage(requestError);
            retry.hidden = false;
        }
    } finally {
        if (revision === generation) {
            loading = false;
            status.textContent = "";
        }
    }
}

form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (loading || saving || form.hidden) {
        return;
    }
    error.textContent = "";
    feedback.textContent = "";
    let data;
    try {
        data = payload();
    } catch (validationError) {
        error.textContent = validationError.message;
        error.focus();
        return;
    }
    saving = true;
    form.inert = true;
    save.disabled = true;
    if (selectOrganization) {
        selectOrganization.disabled = true;
    }
    try {
        const updated = await request(endpoint(), "PATCH", token, data);
        render(updated);
        feedback.textContent = "Настройки сохранены.";
    } catch (requestError) {
        error.textContent = errorMessage(requestError);
        error.focus();
    } finally {
        saving = false;
        form.inert = false;
        save.disabled = false;
        if (selectOrganization) {
            selectOrganization.disabled = false;
        }
        if (document.activeElement === document.body) {
            save.focus();
        }
    }
});
form.addEventListener("invalid", (event) => {
    const details = event.target.closest("details");
    if (details) {
        details.open = true;
    }
}, true);

if (personal) {
    get("quiet").addEventListener("change", syncQuietHours);
    const localZone = Intl.DateTimeFormat().resolvedOptions().timeZone;
    const zones = new Set(["UTC", "Asia/Almaty", localZone, ...(Intl.supportedValuesOf?.("timeZone") ?? [])]);
    get("timezones").replaceChildren(...[...zones].filter(Boolean).sort().map((zone) => new Option(zone, zone)));
}
selectOrganization?.addEventListener("change", load);
retry.addEventListener("click", load);
load();

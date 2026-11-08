import {errorMessage, request} from "../departments/directory.js";
import {setupDialog} from "../ui/collection.js";
import {addDays, civilBoundary, localCandidates, localDateTime, localDay, offsetLabel, validZone} from "./dates.js";

export function httpLink(value) {
    try {
        const url = new URL(value);
        return ["https:", "http:"].includes(url.protocol) ? url.href : null;
    } catch {
        return null;
    }
}

function fillScope(select, records, empty, current, editable) {
    select.replaceChildren(new Option(empty, ""));
    for (const record of records) {
        if (record.can_create || record.id === current || editable) {
            select.add(new Option(record.name, record.id));
        }
    }
    if (current && !records.some(({id}) => id === current)) {
        select.add(new Option("Текущая привязка", current));
    }
    select.value = current ?? "";
    select.disabled = !editable;
}

export function setupEditor({app, api, token, scopes, zone, day, onSaved}) {
    const get = (id) => app.querySelector(`#calendar-${id}`);
    const dialog = get("editor");
    const modal = setupDialog(dialog);
    const form = get("event-form");
    const fields = form.elements;
    const error = get("form-error");
    const folds = ["start", "end"].map((part) => ({
        input: get(`event-${part}`),
        wrap: get(`${part}-fold-field`),
        select: get(`${part}-fold`),
    }));
    let editing = null;
    let saving = false;

    function showFold({input, wrap, select}, initial = select.value) {
        const candidates = localCandidates(input.value, fields.timezone.value.trim());
        wrap.hidden = fields.all_day.checked || candidates.length < 2;
        select.replaceChildren(...candidates.map((instant) =>
            new Option(offsetLabel(instant, fields.timezone.value.trim()), instant)));
        if (candidates.includes(initial)) {
            select.value = initial;
        }
    }

    function updateDateFields() {
        const allDay = fields.all_day.checked;
        get("timed-fields").hidden = allDay;
        get("all-day-fields").hidden = !allDay;
        for (const field of [fields.timed_start, fields.timed_end]) {
            field.disabled = allDay;
            field.required = !allDay;
        }
        for (const field of [fields.first_day, fields.last_day]) {
            field.disabled = !allDay;
            field.required = allDay;
        }
        folds.forEach((fold) => showFold(fold));
    }

    fields.all_day.addEventListener("change", updateDateFields);
    fields.timezone.addEventListener("change", updateDateFields);
    for (const fold of folds) {
        fold.input.addEventListener("change", () => showFold(fold));
    }

    function open(trigger, event = null) {
        editing = event;
        form.reset();
        error.textContent = "";
        get("editor-heading").textContent = event ? "Редактировать событие" : "Новое событие";
        const eventZone = event?.timezone ?? zone();
        const activeDay = day();
        fields.timezone.value = eventZone;
        fields.title.value = event?.title ?? "";
        fields.type.value = event?.type ?? "meeting";
        fields.description.value = event?.description ?? "";
        fields.location.value = event?.location ?? "";
        fields.online_url.value = event?.online_url ?? "";
        fields.all_day.checked = event?.all_day ?? false;
        fields.timed_start.value = event ? localDateTime(event.starts_at, eventZone) : `${activeDay}T09:00`;
        fields.timed_end.value = event ? localDateTime(event.ends_at, eventZone) : `${activeDay}T10:00`;
        fields.first_day.value = event ? localDay(event.starts_at, eventZone) : activeDay;
        fields.last_day.value = event ? addDays(localDay(event.ends_at, eventZone), event.all_day ? -1 : 0) : activeDay;
        const options = scopes();
        const editable = !event || event.can_manage_scope;
        fillScope(fields.project_id, options.projects.filter((row) => event || row.can_create), "Без проекта", event?.project_id, editable);
        fillScope(fields.department_id, options.departments.filter((row) => event || row.can_create), "Без подразделения", event?.department_id, editable);
        updateDateFields();
        if (event) {
            showFold(folds[0], new Date(event.starts_at).toISOString());
            showFold(folds[1], new Date(event.ends_at).toISOString());
        }
        modal.open(trigger, fields.title);
    }

    function eventDates(eventZone) {
        if (fields.all_day.checked) {
            const first = fields.first_day.value;
            const last = fields.last_day.value;
            if (last < first) {
                throw new Error("Последний день не может быть раньше первого.");
            }
            const start = civilBoundary(first, eventZone);
            const end = civilBoundary(addDays(last, 1), eventZone);
            if (!start || !end || !civilBoundary(last, eventZone)) {
                throw new Error("Одна из дат отсутствует в выбранном часовом поясе. Измените даты события.");
            }
            return [start, end];
        }
        return folds.map((fold, index) => {
            showFold(fold);
            if (!fold.select.value) {
                fold.input.focus();
                throw new Error(index === 0
                    ? "Такого времени начала в выбранном часовом поясе нет."
                    : "Такого времени окончания в выбранном часовом поясе нет.");
            }
            return fold.select.value;
        });
    }

    form.addEventListener("submit", async (event) => {
        event.preventDefault();
        if (saving) {
            return;
        }
        error.textContent = "";
        let payload;
        try {
            const title = fields.title.value.trim();
            const eventZone = fields.timezone.value.trim();
            const link = fields.online_url.value.trim();
            const project = Number(fields.project_id.value) || null;
            const department = Number(fields.department_id.value) || null;
            if (!title || !validZone(eventZone) || (link && !httpLink(link))) {
                throw new Error(!title ? "Укажите название."
                    : !validZone(eventZone) ? "Укажите часовой пояс IANA, например Asia/Almaty."
                        : "Для онлайн-встречи укажите ссылку HTTP или HTTPS.");
            }
            if (!editing && !scopes().can_create_organization && !project && !department) {
                throw new Error("Выберите проект или подразделение для события.");
            }
            const [start, end] = eventDates(eventZone);
            if (Date.parse(end) <= Date.parse(start)) {
                throw new Error("Окончание должно быть позже начала.");
            }
            payload = {
                title, description: fields.description.value, type: fields.type.value,
                starts_at: start, ends_at: end, timezone: eventZone,
                all_day: fields.all_day.checked, location: fields.location.value.trim(), online_url: link,
            };
            if (!editing || editing.can_manage_scope) {
                if (!editing || project !== editing.project_id) {
                    payload.project_id = project;
                }
                if (!editing || department !== editing.department_id) {
                    payload.department_id = department;
                }
            }
        } catch (validationError) {
            error.textContent = validationError.message;
            return;
        }
        saving = true;
        modal.setBusy(true);
        try {
            const url = editing ? `${api}events/${editing.id}/` : `${api}events/`;
            await request(url, editing ? "PATCH" : "POST", token, payload);
            modal.close();
            onSaved(editing ? "Изменения сохранены." : "Событие создано.");
        } catch (requestError) {
            error.textContent = errorMessage(requestError);
        } finally {
            saving = false;
            modal.setBusy(false);
        }
    });
    return {open};
}

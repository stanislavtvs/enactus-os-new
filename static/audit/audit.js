import {element, errorMessage, request} from "../departments/directory.js";
import {setupDialog} from "../ui/collection.js";

const app = document.querySelector("#audit-app");
const get = (id) => app.querySelector(`#audit-${id}`);
const api = app.dataset.apiUrl;
const filters = get("filters");
const list = get("list");
const status = get("status");
const error = get("error");
const retry = get("retry");
const pagination = get("pagination");
const previous = get("previous");
const next = get("next");
const dialog = get("detail");
const modal = setupDialog(dialog);
const utcDateTime = new Intl.DateTimeFormat("ru", {dateStyle: "medium", timeStyle: "short", timeZone: "UTC"});
const utcDate = new Intl.DateTimeFormat("ru", {dateStyle: "medium", timeZone: "UTC"});
const filterOptions = [
    ["actor", "actors", "id", "name"],
    ["action", "actions", "code", "label"],
    ["object-type", "object_types", "code", "label"],
    ["project", "projects", "id", "name"],
    ["department", "departments", "id", "name"],
];
const fieldLabels = {
    name: "Название", title: "Название", description: "Описание", due_at: "Срок выполнения",
    deadline: "Срок выполнения", priority: "Приоритет", priority_id: "Приоритет", priority_level_id: "Приоритет",
    status: "Статус", workflow_status_id: "Статус", is_archived: "В архиве", is_active: "Активность",
    parent_id: "Родительский объект", department_id: "Подразделение", project_id: "Проект",
    responsible_department_id: "Ответственное подразделение", assignee_id: "Исполнитель",
    lead_id: "Руководитель", permissions: "Разрешения", include_descendants: "Включая дочерние подразделения",
    starts_at: "Начало", ends_at: "Окончание", timezone: "Часовой пояс", all_day: "Весь день",
    location: "Место", online_url: "Онлайн-ссылка", occurrence_key: "Исходная дата повторения",
    stage: "Этап", comment: "Комментарий", note: "Примечание", reason: "Причина",
    review_comment: "Комментарий к проверке", specialization: "Специализация",
    join_policy: "Правило вступления", department_directory_visibility: "Видимость каталога подразделений",
    task_assignment_mode: "Способ назначения задач", project_assignment_mode: "Способ включения в проект",
    review_policy: "Маршрут проверки", role_id: "Роль", lead_membership_id: "Руководитель",
    owner_id: "Владелец", reviewer_id: "Проверяющий", recipient_id: "Получатель",
    weekdays: "Дни недели", planned_start_at: "Запланированное начало", completed_at: "Завершено",
    start_date: "Дата начала", planned_end_date: "Плановая дата окончания",
    is_cancelled: "Отменено", occurrence_revision: "Версия повторения",
    required_permissions: "Требуемые разрешения", requested_count: "Запрошено специалистов",
    rsvp_mode: "Режим RSVP", response: "Ответ", frequency: "Частота повторения", interval: "Интервал",
    stopped_from: "Повторения остановлены с", until: "Окончание повторений", count: "Количество",
    enabled_categories: "Включённые категории", disabled_kinds: "Отключённые типы уведомлений",
    task_offsets_minutes: "Интервалы напоминаний о задачах", calendar_offsets_minutes: "Интервалы напоминаний о встречах",
    task_overdue: "Напоминания о просрочке", include_collaborators: "Напоминания соисполнителям",
    calendar_informational: "Напоминания об информационных событиях",
    all_day_reminder_time: "Время напоминания о событиях на весь день", policy: "Правило",
};
const detailMetadata = {
    occurrence_key: "Исходная дата повторения",
    stage: "Этап проверки",
    stage_position: "Этап проверки",
    history_id: "Запись истории RSVP",
    submission_version: "Версия результата",
    result_version: "Версия результата",
    version: "Версия",
    revision: "Версия",
    affected_count: "Затронуто записей",
    added_count: "Добавлено",
    removed_count: "Исключено",
    updated_count: "Обновлено",
    invitation_count: "Приглашено",
    occurrence_version: "Версия повторения",
    count: "Количество",
    mode: "Режим",
};
const stateLabels = {
    active: "Активен", archived: "В архиве", draft: "Черновик", scheduled: "Запланировано",
    cancelled: "Отменено", pending: "Ожидает", completed: "Завершено", in_progress: "В работе",
    review: "На проверке", approved: "Одобрено", returned: "Возвращено", rejected: "Отклонено",
    informational: "Информационное", optional: "Необязательный ответ", required: "Обязательный ответ",
    daily: "Ежедневно", weekly: "Еженедельно", monthly: "Ежемесячно", yearly: "Ежегодно",
};
const codedFields = new Set(["status", "mode", "rsvp_mode", "frequency"]);
let applied = new URLSearchParams();
let page = 1;
let pages = 1;
let loading = false;
let listVersion = 0;
let detailVersion = 0;
let optionsRequest;

function pathFor(value) {
    if (typeof value !== "string" || !value.startsWith("/") || value.startsWith("//") || value.includes("\\")) {
        return null;
    }
    const url = new URL(value, window.location.origin);
    return url.origin === window.location.origin && !url.pathname.startsWith("//") ? url.pathname + url.search + url.hash : null;
}

function fact(label, value) {
    const row = element("div", "");
    row.append(element("dt", "", label), element("dd", "", value));
    return row;
}

function formatted(value, field) {
    if (value === null || value === undefined || value === "") {
        return "Не указано";
    }
    if (typeof value === "boolean") {
        return value ? "Да" : "Нет";
    }
    if (Array.isArray(value)) {
        return value.length ? value.map((item) => formatted(item, field)).join(", ") : "Нет";
    }
    if (typeof value === "object") {
        const text = value.label ?? value.name ?? value.title ?? value.code;
        if (typeof text !== "string") return "Значение скрыто";
        if (typeof value.include_descendants === "boolean") {
            const scope = value.include_descendants ? "с дочерними подразделениями" : "без дочерних подразделений";
            return `${text} (${scope})`;
        }
        return text;
    }
    if (typeof value === "string") {
        if (codedFields.has(field) && Object.hasOwn(stateLabels, value)) {
            return stateLabels[value];
        }
        if (/^\d{4}-\d{2}-\d{2}(?:T|$)/.test(value) && Number.isFinite(Date.parse(value))) {
            return (value.length === 10 ? utcDate : utcDateTime).format(new Date(value));
        }
    }
    return String(value);
}

function actor(record) {
    return record.actor_name || "Не указан";
}

function renderRow(record) {
    const row = element("li", "audit-row");
    row.dataset.auditId = record.id;
    const time = element("time", "", utcDateTime.format(new Date(record.created_at)));
    time.dateTime = record.created_at;
    const copy = element("div", "audit-row-copy");
    const title = element("h2", "", record.action_label);
    const object = element("p", "", record.object_type_label);
    if (record.object_title) {
        object.append(" · ");
        const path = pathFor(record.object_url);
        if (path) {
            const link = element("a", "", record.object_title);
            link.href = path;
            object.append(link);
        } else {
            object.append(record.object_title);
        }
    }
    const context = element("p", "audit-row-context");
    context.append(element("span", "", `Инициатор: ${actor(record)}`));
    const scopes = [record.department_name, record.project_name].filter(Boolean);
    for (const label of scopes.length ? scopes : [app.dataset.organizationName]) {
        context.append(element("span", "", label));
    }
    copy.append(title, object, context);
    row.append(time, copy);
    if (record.can_view_details) {
        const details = element("button", "button collection-secondary", "Подробности");
        details.type = "button";
        details.dataset.auditDetails = record.id;
        details.setAttribute("aria-haspopup", "dialog");
        details.setAttribute("aria-label", `Подробности: ${record.action_label}`);
        details.addEventListener("click", () => openDetail(record, details));
        row.append(details);
    }
    return row;
}

function presentDetail(record) {
    get("detail-title").textContent = record.action_label;
    const facts = [
        fact("Дата и время · UTC", utcDateTime.format(new Date(record.created_at))),
        fact("Инициатор", actor(record)),
        fact("Организация", app.dataset.organizationName),
        fact("Категория объекта", record.object_type_label),
    ];
    for (const [label, value] of [
        ["Объект", record.object_title], ["Подразделение", record.department_name], ["Проект", record.project_name],
        ["Идентификатор операции", record.operation_id],
    ]) {
        if (value) {
            facts.push(fact(label, value));
        }
    }
    for (const [key, label] of Object.entries(detailMetadata)) {
        if (record.metadata?.[key] !== null && record.metadata?.[key] !== undefined) {
            facts.push(fact(label, formatted(record.metadata[key], key)));
        }
    }
    get("detail-facts").replaceChildren(...facts);
    const changes = Object.entries(record.changes ?? {}).map(([field, value]) => {
        const row = element("li", "audit-change");
        row.append(element("strong", "", fieldLabels[field] ?? field.replaceAll("_", " ")));
        if (value && Object.hasOwn(value, "old") && Object.hasOwn(value, "new")
            && (value.old !== null || value.new !== null)) {
            const diff = element("dl", "audit-change-values");
            diff.append(fact("Было", formatted(value.old, field)), fact("Стало", formatted(value.new, field)));
            row.append(diff);
        } else {
            row.append(element("p", "muted", "Значение изменено."));
        }
        return row;
    });
    get("changes-list").replaceChildren(...changes);
    get("detail-changes").hidden = !changes.length;
    const link = get("object-link");
    const path = pathFor(record.object_url);
    link.hidden = !path;
    if (path) {
        link.href = path;
    }
    if (!record.can_view_details) {
        get("detail-status").textContent = "Подробности этой записи ограничены.";
    }
}

async function openDetail(record, trigger) {
    const version = ++detailVersion;
    get("detail-title").textContent = record.action_label;
    get("detail-facts").replaceChildren();
    get("detail-changes").hidden = true;
    get("object-link").hidden = true;
    get("object-link").removeAttribute("href");
    get("detail-error").textContent = "";
    get("detail-status").textContent = "Загрузка записи…";
    modal.open(trigger, dialog.querySelector("[data-dialog-close]"));
    try {
        const detail = await request(`${api}${record.id}/`);
        if (dialog.open && version === detailVersion) {
            get("detail-status").textContent = "";
            presentDetail(detail);
        }
    } catch (requestError) {
        if (dialog.open && version === detailVersion) {
            get("detail-status").textContent = "";
            get("detail-error").textContent = errorMessage(requestError);
        }
    }
}

async function loadOptions() {
    for (const [id] of filterOptions) {
        get(id).disabled = true;
    }
    const options = await request(`${api}options/`);
    for (const [id, key, value, label] of filterOptions) {
        const control = get(id);
        control.replaceChildren(control.options[0], ...options[key].map((item) => new Option(item[label], item[value])));
        control.disabled = false;
    }
}

function ensureOptions() {
    optionsRequest ??= loadOptions().catch(() => {
        optionsRequest = undefined;
        return "Часть фильтров недоступна. Попробуйте обновить журнал.";
    });
    return optionsRequest;
}

async function load() {
    const version = ++listVersion;
    const query = new URLSearchParams(applied);
    query.set("page", page);
    const focused = document.activeElement;
    loading = true;
    list.setAttribute("aria-busy", "true");
    list.replaceChildren();
    pagination.hidden = true;
    status.textContent = "Загрузка журнала…";
    error.textContent = "";
    retry.hidden = true;
    try {
        const [data, optionsResult] = await Promise.all([
            request(`${api}?${query}`),
            ensureOptions(),
        ]);
        if (version !== listVersion) {
            return;
        }
        pages = Math.max(1, Math.ceil(data.total / data.page_size));
        if (page > pages) {
            page = pages;
            load();
            return;
        }
        list.replaceChildren(...data.items.map(renderRow));
        status.textContent = data.total ? `Найдено записей: ${data.total}` : "В этой выборке нет записей.";
        pagination.hidden = pages < 2;
        get("page").textContent = `Страница ${data.page} из ${pages}`;
        previous.disabled = data.page <= 1;
        next.disabled = data.page >= pages;
        if (pagination.contains(focused) && document.activeElement === document.body) {
            (pagination.hidden ? get("heading") : focused.disabled ? (focused === previous ? next : previous) : focused).focus();
        }
        if (optionsResult) {
            error.textContent = optionsResult;
            retry.hidden = false;
        }
    } catch (requestError) {
        if (version === listVersion) {
            status.textContent = "";
            error.textContent = errorMessage(requestError);
            retry.hidden = false;
        }
    } finally {
        if (version === listVersion) {
            loading = false;
            list.removeAttribute("aria-busy");
        }
    }
}

filters.addEventListener("submit", (event) => {
    event.preventDefault();
    const from = get("date-from");
    const to = get("date-to");
    if (from.value && to.value && from.value > to.value) {
        to.closest("details").open = true;
        to.setCustomValidity("Конец диапазона должен быть не раньше начала.");
        to.reportValidity();
        return;
    }
    applied = new URLSearchParams();
    for (const [key, value] of new FormData(filters)) {
        if (value.trim()) {
            applied.set(key, value.trim());
        }
    }
    page = 1;
    load();
});
for (const id of ["date-from", "date-to"]) {
    get(id).addEventListener("input", () => get("date-to").setCustomValidity(""));
}
filters.addEventListener("reset", () => {
    get("date-to").setCustomValidity("");
    applied = new URLSearchParams();
    page = 1;
    load();
});
for (const [control, delta] of [[previous, -1], [next, 1]]) {
    control.addEventListener("click", () => {
        if (!loading) {
            page += delta;
            load();
        }
    });
}
retry.addEventListener("click", load);
filters.hidden = false;
load();

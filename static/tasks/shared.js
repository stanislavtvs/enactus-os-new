export {element, errorMessage, request} from "../departments/directory.js";
export {selectOptions} from "../projects/shared.js";

export const statuses = {
    pending: "Ожидает",
    in_progress: "В работе",
    review: "На проверке",
    completed: "Завершена",
    cancelled: "Отменена",
};

export const priorities = {low: "Низкий", normal: "Обычный", high: "Высокий"};

const displayTime = new Intl.DateTimeFormat("ru", {dateStyle: "medium", timeStyle: "short"});
const upcomingWindow = 7 * 24 * 60 * 60 * 1000;

export function localValue(iso) {
    if (!iso) {
        return "";
    }
    const date = new Date(iso);
    const pad = (value, length = 2) => String(value).padStart(length, "0");
    const day = `${pad(date.getFullYear(), 4)}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
    const time = `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}.${pad(date.getMilliseconds(), 3)}`;
    return `${day}T${time}`;
}

export function dateLabel(iso) {
    return iso ? displayTime.format(new Date(iso)) : "Не указан";
}

export function readTaskFields(form, existing = null) {
    const field = (name) => form.elements.namedItem(name);
    const name = field("name").value.trim();
    if (!name || Array.from(name).length > 200) {
        field("name").focus();
        throw new Error("Введите название длиной от 1 до 200 символов.");
    }
    const payload = {name, description: field("description").value, priority: field("priority").value};
    for (const key of ["planned_start_at", "due_at"]) {
        const input = field(key);
        if (existing && input.value === input.dataset.originalValue) {
            continue;
        }
        if (!input.value) {
            payload[key] = null;
            continue;
        }
        const date = new Date(input.value);
        if (!Number.isFinite(date.getTime()) || localValue(date).slice(0, 16) !== input.value.slice(0, 16)) {
            input.focus();
            throw new Error("Выберите допустимое время в часовом поясе устройства.");
        }
        payload[key] = date.toISOString();
    }
    const start = Object.hasOwn(payload, "planned_start_at") ? payload.planned_start_at : existing?.planned_start_at;
    const due = Object.hasOwn(payload, "due_at") ? payload.due_at : existing?.due_at;
    if (start && due && Date.parse(due) < Date.parse(start)) {
        field("due_at").focus();
        throw new Error("Срок выполнения не может быть раньше начала.");
    }
    return payload;
}

export function deadlineState(task, now = Date.now()) {
    if (!task.due_at || task.is_archived || ["completed", "cancelled"].includes(task.status)) {
        return "";
    }
    const until = Date.parse(task.due_at) - now;
    if (until < 0) {
        return "overdue";
    }
    return until <= upcomingWindow ? "upcoming" : "";
}

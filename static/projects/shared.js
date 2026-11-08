export {element, errorMessage, request} from "../departments/directory.js";

export const statuses = {
    draft: "Черновик",
    active: "Активен",
    paused: "На паузе",
    completed: "Завершён",
};

export function selectOptions(select, label, records, selected = "") {
    select.replaceChildren(new Option(label, ""));
    for (const [id, name] of records) {
        select.add(new Option(name, String(id)));
    }
    select.value = String(selected ?? "");
}

export function formPayload(form) {
    const value = (name) => form.elements.namedItem(name).value;
    const name = value("name").trim();
    const start = value("start_date");
    const end = value("planned_end_date");
    if (!name || Array.from(name).length > 200) {
        form.elements.namedItem("name").focus();
        throw new Error("Введите название длиной от 1 до 200 символов.");
    }
    if (start && end && end < start) {
        form.elements.namedItem("planned_end_date").focus();
        throw new Error("Окончание проекта не может быть раньше даты начала.");
    }
    return {
        name,
        description: value("description"),
        status: value("status"),
        start_date: start || null,
        planned_end_date: end || null,
    };
}

export function nullableId(form, name) {
    const value = form.elements.namedItem(name).value;
    return value ? Number(value) : null;
}

export function departmentLabel(department) {
    return department.is_archived ? `${department.name} (архив)` : department.name;
}

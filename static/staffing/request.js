import {allocationStatuses, button, dateTime, element, formPayload, listRow, pageState, request, requestStatuses} from "./shared.js";

const app = document.querySelector("#staffing-request-app");
const api = `${app.dataset.orgUrl}staffing-requests/${app.dataset.requestId}/`;
const page = pageState(app);
const heading = document.querySelector("#staffing-request-heading");
const info = document.querySelector("#staffing-request-info");
const facts = document.querySelector("#staffing-request-facts");
const description = document.querySelector("#staffing-request-description");
const submit = document.querySelector("#staffing-submit");
const cancel = document.querySelector("#staffing-cancel");
const editPanel = document.querySelector("#staffing-edit-panel");
const edit = document.querySelector("#staffing-edit-form");
const allocationPanel = document.querySelector("#staffing-allocation-panel");
const allocationHint = document.querySelector("#staffing-allocation-hint");
const allocateForm = document.querySelector("#staffing-allocate-form");
const allocateButton = document.querySelector("#staffing-allocate");
const candidates = document.querySelector("#staffing-candidates");
const rejectForm = document.querySelector("#staffing-reject-form");
const historyPanel = document.querySelector("#staffing-history-panel");
const history = document.querySelector("#staffing-history");
const historyEmpty = document.querySelector("#staffing-history-empty");

let item;
let availableSlots = 0;
const batchLimit = 100;

function renderFacts() {
    heading.textContent = `Запрос №${item.id}`;
    const labels = [
        ["Проект", item.project_name],
        ["Подразделение", item.department_name],
        ["Статус", requestStatuses[item.status] ?? "Статус неизвестен"],
        ["Нужно специалистов", item.requested_count],
        ["Подтверждено", item.accepted_count],
        ["Ожидают ответа", item.pending_count],
        ["Специализация", item.specialization || "Не указана"],
        ["Желаемая дата начала", item.desired_start || "Не указана"],
        ["Длительность, дни", item.duration_days ?? "Не указана"],
        ["Создан", dateTime(item.created_at)],
    ];
    if (item.awaiting_handler) {
        labels.push(["Рассмотрение", "Ожидает ответа подразделения"]);
    }
    if (item.comment) {
        labels.push(["Комментарий подразделения", item.comment]);
    }
    const handledBy = typeof item.handled_by === "string" ? item.handled_by : item.handled_by?.username;
    if (handledBy) {
        labels.push(["Рассмотрел", handledBy]);
    }
    if (item.handled_at) {
        labels.push(["Дата рассмотрения", dateTime(item.handled_at)]);
    }
    facts.replaceChildren();
    for (const [label, value] of labels) {
        const group = element("div", "");
        group.append(element("dt", "", label), element("dd", "", value));
        facts.append(group);
    }
    description.textContent = item.description;
    info.hidden = false;

    const draft = item.can_edit && item.status === "draft";
    submit.hidden = !draft;
    cancel.hidden = !item.can_cancel;
    editPanel.hidden = !item.can_edit;
    if (item.can_edit) {
        for (const name of ["requested_count", "description", "specialization", "desired_start", "duration_days"]) {
            edit.elements.namedItem(name).value = item[name] ?? "";
        }
        edit.elements.namedItem("requested_count").min = Math.max(1, item.accepted_count + item.pending_count);
    }
}

function renderHistory() {
    history.replaceChildren();
    for (const allocation of item.history) {
        const mode = allocation.mode === "direct" ? "Прямое назначение" : "Приглашение";
        const details = [allocation.department_name, mode, allocationStatuses[allocation.status] ?? "Статус неизвестен", dateTime(allocation.created_at)];
        const allocatedBy = typeof allocation.allocated_by === "string" ? allocation.allocated_by : allocation.allocated_by?.username;
        if (allocatedBy) {
            details.push(`выделил(а): ${allocatedBy}`);
        }
        if (allocation.resolved_at) {
            details.push(`ответ: ${dateTime(allocation.resolved_at)}`);
        }
        const row = listRow(allocation.username, details.filter(Boolean).join(" · "));
        if (allocation.can_revoke) {
            const revoke = button("Отозвать", () => revokeAllocation(allocation));
            revoke.setAttribute("aria-label", `Отозвать выделение: ${allocation.username}`);
            row.append(revoke);
        }
        history.append(row);
    }
    historyEmpty.hidden = item.history.length > 0;
    historyPanel.hidden = false;
}

function updateCandidateChoices() {
    const inputs = [...candidates.querySelectorAll("input")];
    const checkedCount = inputs.filter((input) => input.checked).length;
    const limit = Math.min(availableSlots, batchLimit);
    allocateButton.disabled = !checkedCount || checkedCount > limit;
    for (const input of inputs) {
        input.disabled = !input.checked && checkedCount >= limit;
    }
}

function renderCandidates(records) {
    candidates.replaceChildren();
    availableSlots = Math.max(0, item.requested_count - item.accepted_count - item.pending_count);
    allocationHint.textContent = availableSlots > batchLimit
        ? `Свободных мест: ${availableSlots}. За один раз можно выделить до ${batchLimit} человек.`
        : `Можно выделить ещё: ${availableSlots}.`;
    for (const person of records) {
        const label = element("label", "staffing-candidate");
        const input = document.createElement("input");
        input.type = "checkbox";
        input.name = "candidate";
        input.value = String(person.id);
        const text = element("span", "");
        text.append(document.createTextNode(`${person.username} · ${person.department_name}`));
        if (person.projects?.length) {
            text.append(element("span", "staffing-meta", `Проекты: ${person.projects.join(", ")}`));
        }
        label.append(input, text);
        candidates.append(label);
    }
    if (!records.length) {
        candidates.append(element("p", "muted", "Доступных кандидатов пока нет."));
    }
    updateCandidateChoices();
    allocationPanel.hidden = false;
}

async function load(message = "") {
    page.start(message || "Загружаем запрос…");
    try {
        item = await request(api);
        renderFacts();
        renderHistory();
        allocationPanel.hidden = true;
        if (item.can_allocate) {
            renderCandidates(await request(`${api}candidates/`));
        }
        page.finish(message);
        return true;
    } catch (error) {
        page.fail(error, message);
        return false;
    }
}

async function mutate(path, method, payload, message) {
    if (page.busy) {
        return;
    }
    page.start("Сохраняем изменение…");
    try {
        await page.mutate(api + path, method, payload);
    } catch (error) {
        page.fail(error);
        return;
    }
    if (await load(message)) {
        heading.focus();
    }
}

function revokeAllocation(allocation) {
    if (window.confirm(`Отозвать выделение «${allocation.username}» для проекта?`)) {
        return mutate(`allocations/${allocation.id}/`, "DELETE", null, "Выделение отозвано.");
    }
}

submit.addEventListener("click", () => mutate("submit/", "POST", null, "Запрос отправлен подразделению."));
cancel.addEventListener("click", () => {
    if (window.confirm("Отменить этот запрос специалистов?")) {
        return mutate("", "DELETE", null, "Запрос отменён.");
    }
});
edit.addEventListener("submit", (event) => {
    event.preventDefault();
    let payload;
    try {
        payload = formPayload(edit);
    } catch (error) {
        page.validation(error);
        return;
    }
    return mutate("", "PATCH", payload, "Изменения сохранены.");
});
candidates.addEventListener("change", updateCandidateChoices);
allocateForm.addEventListener("submit", (event) => {
    event.preventDefault();
    const selected = new FormData(allocateForm).getAll("candidate").map(Number);
    if (!selected.length || selected.length > Math.min(availableSlots, batchLimit)) {
        page.validation(new Error("Выберите подходящее количество специалистов."));
        return;
    }
    return mutate("allocate/", "POST", {department_membership_ids: selected}, "Специалисты выделены.");
});
rejectForm.addEventListener("submit", (event) => {
    event.preventDefault();
    const commentField = rejectForm.elements.namedItem("comment");
    const comment = commentField.value.trim();
    if (!comment) {
        page.validation(new Error("Укажите причину отказа."));
        commentField.focus();
        return;
    }
    if (window.confirm("Отклонить этот запрос специалистов?")) {
        return mutate("reject/", "POST", {comment}, "Запрос отклонён.");
    }
});
page.retry.addEventListener("click", () => load());
load();

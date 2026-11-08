import {dateLabel, deadlineState, element, errorMessage, localValue, priorities, priorityChip, readTaskFields, request, selectOptions, setupPrioritySelect, statuses, workflowStatusChip} from "./shared.js";
import {taskWorkflow} from "./workflow.js";
import {taskDecomposition} from "./decomposition.js";
import {taskDependencies} from "./dependencies.js";

const app = document.querySelector("#task-app");
const base = app.dataset.orgUrl;
const api = `${base}tasks/${app.dataset.taskId}/`;
const token = app.querySelector('[name="csrfmiddlewaretoken"]').value;
const heading = document.querySelector("#task-heading");
const status = document.querySelector("#task-load-status");
const errorBox = document.querySelector("#task-error");
const retry = document.querySelector("#task-retry");
const info = document.querySelector("#task-info");
const facts = document.querySelector("#task-facts");
const statusForm = document.querySelector("#task-status-form");
const statusSelect = document.querySelector("#task-change-status");
const archive = document.querySelector("#task-archive");
const people = document.querySelector("#task-people");
const assigneeForm = document.querySelector("#task-assignee-form");
const assignee = document.querySelector("#task-assignee");
const collaborators = document.querySelector("#task-collaborators");
const collaboratorForm = document.querySelector("#task-collaborator-form");
const collaborator = document.querySelector("#task-collaborator");
const editPanel = document.querySelector("#task-edit-panel");
const editForm = document.querySelector("#task-edit-form");
const departmentField = document.querySelector("#task-edit-department-field");
const department = document.querySelector("#task-edit-department");
const editError = document.querySelector("#task-edit-error");

let task;
let busy = false;
const workflow = taskWorkflow(app, {
    api,
    token,
    async refresh(message) {
        if (await load(message)) {
            heading.focus();
        }
    },
});
const decomposition = taskDecomposition(app, {
    api,
    base,
    token,
    refresh: () => load(),
});
const dependencies = taskDependencies(app, {
    api,
    base,
    token,
    refresh: () => load(),
});

function renderInfo() {
    heading.textContent = task.name;
    document.title = `${task.name} — ${app.dataset.orgName} — Enactus OS`;
    const parentContext = document.querySelector("#task-parent-context");
    parentContext.hidden = !task.parent;
    parentContext.replaceChildren();
    if (task.parent) {
        const parentLink = element("a", "", task.parent.name);
        parentLink.href = `${app.dataset.taskListUrl}${task.parent.id}/`;
        parentContext.append("Родительская задача: ", parentLink);
    }
    const deadline = deadlineState(task);
    const due = dateLabel(task.due_at);
    const data = [
        ["Статус", workflowStatusChip(task.workflow_status, statuses[task.status] ?? task.status)],
        ["Системный этап", `${statuses[task.status] ?? task.status}${task.is_archived ? " · Архив" : ""}`],
        ["Приоритет", priorityChip(task.priority_level, priorities[task.priority] ?? task.priority)],
        ["Автор", task.author.username],
        ["Проект", task.project?.name ?? "Без проекта"],
        ["Подразделение", task.department?.name ?? "Не указано"],
        ["Плановое начало", dateLabel(task.planned_start_at)],
        ["Срок", deadline === "overdue" ? `${due} · просрочена` : due],
        ["Дата завершения", dateLabel(task.completed_at)],
    ];
    facts.replaceChildren();
    for (const [label, value] of data) {
        const group = element("div", "");
        const definition = element("dd", "");
        definition.append(value);
        group.append(element("dt", "", label), definition);
        facts.append(group);
    }
    document.querySelector("#task-info-description").textContent = task.description;
    const custom = Boolean(task.workflow_status);
    const transitions = custom
        ? task.allowed_workflow_statuses.map((item) => [item.id, `${item.name} · ${statuses[item.stage] ?? item.stage}`])
        : task.allowed_statuses.filter((choice) => choice !== task.status).map((code) => [code, statuses[code] ?? code]);
    statusForm.hidden = !transitions.length || (!custom && !task.permissions.change_status);
    selectOptions(statusSelect, "Выберите статус", transitions);
    const reasons = [];
    if (!task.is_archived && !["completed", "cancelled"].includes(task.status)) {
        if (task.has_blocking_dependencies) {
            reasons.push("Начать работу нельзя, пока не завершены строгие предшественники.");
        }
        if (task.status === "review") {
            reasons.push("Результат принимают на текущем этапе согласования.");
        } else if (task.status === "in_progress") {
            reasons.push("Для проверки или завершения отправьте результат на согласование.");
        } else if (!task.assignee && !task.distribution_pending) {
            reasons.push("Для начала работы нужен основной исполнитель.");
        }
        if (!transitions.length && !reasons.length) {
            reasons.push("Нет доступных ручных переходов для этой задачи.");
        }
    }
    document.querySelector("#task-status-reason").textContent = reasons.join(" ");
    archive.hidden = !task.permissions.archive;
    info.hidden = false;
}

function renderPeople(canAssign = false, assignees = []) {
    document.querySelector("#task-current-assignee").textContent = task.assignee
        ? `Основной исполнитель: ${task.assignee.username}` : "Основной исполнитель не назначен.";
    collaborators.replaceChildren();
    for (const person of task.collaborators) {
        const row = element("li", "task-row");
        row.append(element("span", "", person.username));
        if (canAssign) {
            const remove = element("button", "button task-secondary", "Убрать");
            remove.type = "button";
            remove.setAttribute("aria-label", `Убрать соисполнителя ${person.username}`);
            remove.addEventListener("click", () => mutate(`collaborators/${person.id}/`, "DELETE", null, "Соисполнитель убран."));
            row.append(remove);
        }
        collaborators.append(row);
    }
    document.querySelector("#task-collaborators-empty").hidden = task.collaborators.length > 0;
    const collaboratorIds = new Set(task.collaborators.map(({id}) => id));
    const mainChoices = assignees.filter(({id}) => !collaboratorIds.has(id)).map(({id, username}) => [id, username]);
    if (task.assignee && !mainChoices.some(([id]) => id === task.assignee.id)) {
        mainChoices.unshift([task.assignee.id, `${task.assignee.username} (текущий)`]);
    }
    selectOptions(assignee, "Без исполнителя", mainChoices, task.assignee?.id);
    const requiresAssignee = ["in_progress", "review", "completed"].includes(task.status) && Boolean(task.assignee);
    assignee.options[0].disabled = requiresAssignee;
    assignee.required = requiresAssignee;
    assigneeForm.hidden = !canAssign;
    const collaboratorsChoices = assignees.filter(({id}) => !collaboratorIds.has(id) && id !== task.assignee?.id);
    selectOptions(collaborator, "Выберите участника", collaboratorsChoices.map(({id, username}) => [id, username]));
    collaboratorForm.hidden = !canAssign || !collaboratorsChoices.length;
    people.hidden = false;
}

function setupEdit(options) {
    for (const field of ["name", "description"]) {
        editForm.elements.namedItem(field).value = task[field];
    }
    setupPrioritySelect(editForm.elements.namedItem("priority_id"), options.priorities, task.priority_level);
    for (const field of ["planned_start_at", "due_at"]) {
        const input = editForm.elements.namedItem(field);
        input.value = localValue(task[field]);
        input.dataset.originalValue = input.value;
    }
    const choices = options.departments.filter(({can_update}) => can_update).map(({id, name}) => [id, name]);
    const otherChoices = choices.filter(([id]) => id !== task.department?.id);
    if (task.department && !choices.some(([id]) => id === task.department.id)) {
        choices.unshift([task.department.id, `${task.department.name} (текущее)`]);
    }
    selectOptions(department, options.can_clear_department || !task.department ? "Без подразделения" : "Выберите подразделение",
        choices, task.department?.id);
    department.options[0].disabled = Boolean(task.department && !options.can_clear_department);
    departmentField.hidden = !otherChoices.length && !(task.department && options.can_clear_department);
    editError.textContent = "";
    editPanel.hidden = false;
}

function setBusy(value) {
    busy = value;
    for (const panel of [info, people, editPanel, workflow.panel, decomposition.panel, dependencies.panel]) {
        panel.inert = value;
    }
}

async function load(message = "") {
    setBusy(true);
    retry.hidden = true;
    errorBox.textContent = "";
    status.textContent = message || "Загружаем задачу…";
    try {
        task = await request(api);
        renderInfo();
        renderPeople();
        editPanel.hidden = true;
        workflow.panel.hidden = true;
        decomposition.panel.hidden = true;
        dependencies.panel.hidden = true;
        const [workflowResult, optionsResult, decompositionResult, dependenciesResult] = await Promise.allSettled([
            request(`${api}workflow/`),
            task.permissions.update || task.permissions.assign
                ? request(`${base}task-options/?task_id=${encodeURIComponent(app.dataset.taskId)}`) : null,
            task.can_view_decomposition ? decomposition.load(task) : null,
            task.can_view_decomposition ? dependencies.load(task) : null,
        ]);
        if (workflowResult.status === "fulfilled") {
            workflow.render(task, workflowResult.value);
        }
        if (optionsResult.status === "fulfilled" && optionsResult.value) {
            const options = optionsResult.value;
            if (task.permissions.update) {
                setupEdit(options);
            }
            if (task.permissions.assign && options.assignment_mode === "direct") {
                renderPeople(true, options.assignees);
            }
        }
        const failure = [workflowResult, optionsResult, decompositionResult, dependenciesResult]
            .find(({status}) => status === "rejected");
        if (failure) {
            throw failure.reason;
        }
        status.textContent = message;
        return true;
    } catch (error) {
        status.textContent = message;
        errorBox.textContent = errorMessage(error);
        retry.hidden = false;
        return false;
    } finally {
        setBusy(false);
    }
}

async function mutate(path, method, payload, message, targetError = errorBox) {
    if (busy) {
        return;
    }
    setBusy(true);
    errorBox.textContent = "";
    targetError.textContent = "";
    status.textContent = "Сохраняем изменение…";
    try {
        await request(api + path, method, token, payload);
    } catch (error) {
        targetError.textContent = errorMessage(error);
        status.textContent = "";
        retry.hidden = false;
        setBusy(false);
        return;
    }
    if (await load(message)) {
        heading.focus();
    }
}

editForm.addEventListener("submit", (event) => {
    event.preventDefault();
    editError.textContent = "";
    let payload;
    try {
        payload = readTaskFields(editForm, task);
    } catch (error) {
        editError.textContent = error.message;
        return;
    }
    if (!departmentField.hidden && department.value !== String(task.department?.id ?? "")) {
        payload.department_id = department.value ? Number(department.value) : null;
    }
    return mutate("", "PATCH", payload, "Задача обновлена.", editError);
});
assigneeForm.addEventListener("submit", (event) => {
    event.preventDefault();
    const assignee_id = assignee.value ? Number(assignee.value) : null;
    return mutate("assignee/", "PUT", {assignee_id}, "Исполнитель обновлён.");
});
collaboratorForm.addEventListener("submit", (event) => {
    event.preventDefault();
    return mutate(`collaborators/${collaborator.value}/`, "PUT", null, "Соисполнитель добавлен.");
});
statusForm.addEventListener("submit", (event) => {
    event.preventDefault();
    if (task.workflow_status) {
        return mutate("workflow-status/", "PUT", {
            status_id: Number(statusSelect.value),
            expected_status_id: task.workflow_status.id,
        }, "Статус изменён.");
    }
    return mutate("status/", "PUT", {status: statusSelect.value}, "Статус изменён.");
});
archive.addEventListener("click", () => {
    if (window.confirm(`Архивировать задачу «${task.name}»? Она останется в архивном списке при наличии доступа.`)) {
        return mutate("", "DELETE", null, "Задача архивирована.");
    }
});
retry.addEventListener("click", () => load());
load().then(() => {
    if (window.location.hash === "#edit" && !editPanel.hidden) {
        editPanel.querySelector('[name="name"]').focus();
    }
});

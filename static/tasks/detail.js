import {collectionActions, confirmArchive, setupDialog, setupDisclosures} from "../ui/collection.js";
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
const mainActions = document.querySelector("#task-main-actions");
const statusDialog = setupDialog(document.querySelector("#task-status-dialog"));
const peopleDialog = setupDialog(document.querySelector("#task-people-dialog"));
const assigneeForm = document.querySelector("#task-assignee-form");
const assignee = document.querySelector("#task-assignee");
const collaborators = document.querySelector("#task-collaborators");
const collaboratorForm = document.querySelector("#task-collaborator-form");
const collaborator = document.querySelector("#task-collaborator");
const editPanel = document.querySelector("#task-edit-panel");
const editDialog = setupDialog(editPanel);
const editForm = document.querySelector("#task-edit-form");
const departmentField = document.querySelector("#task-edit-department-field");
const department = document.querySelector("#task-edit-department");
const editError = document.querySelector("#task-edit-error");
const statusError = document.querySelector("#task-status-error");
const peopleError = document.querySelector("#task-people-error");
const peopleNotice = document.querySelector("#task-people-notice");

setupDisclosures(app);

let task;
let busy = false;
let editOptions = null;
let canAssign = false;
let canChangeStatus = false;
let menuKey = "";
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
    const preview = [
        ["Статус", workflowStatusChip(task.workflow_status, statuses[task.status] ?? task.status)],
        ["Приоритет", priorityChip(task.priority_level, priorities[task.priority] ?? task.priority)],
        ["Дедлайн", due],
        ["Ответственный", task.assignee?.username ?? "Не назначен"],
    ];
    if (task.project) {
        preview.push(["Проект", task.project.name]);
    }
    if (task.department) {
        preview.push(["Подразделение", task.department.name]);
    }
    renderFacts(facts, preview);
    renderFacts(document.querySelector("#task-extra-facts"), [
        ["Системный этап", `${statuses[task.status] ?? task.status}${task.is_archived ? " · Архив" : ""}`],
        ["Автор", task.author.username],
        ["Плановое начало", dateLabel(task.planned_start_at)],
        ...(task.completed_at ? [["Дата завершения", dateLabel(task.completed_at)]] : []),
    ]);
    document.querySelector("#task-info-description").textContent = task.description || "Описание пока не добавлено.";
    const custom = Boolean(task.workflow_status);
    const transitions = custom
        ? task.allowed_workflow_statuses.map((item) => [item.id, `${item.name} · ${statuses[item.stage] ?? item.stage}`])
        : task.allowed_statuses.filter((choice) => choice !== task.status).map((code) => [code, statuses[code] ?? code]);
    canChangeStatus = transitions.length > 0 && (custom || task.permissions.change_status);
    selectOptions(statusSelect, "Выберите статус", transitions);
    const reasons = [];
    if (deadline === "overdue") {
        reasons.push("Срок задачи истёк.");
    }
    if (task.is_archived) {
        reasons.push("Задача в архиве.");
    }
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
        if (!transitions.length && !task.has_blocking_dependencies && task.status !== "review") {
            reasons.push("Нет доступных ручных переходов с учётом ваших полномочий и настроек задачи.");
        }
    }
    const warning = document.querySelector("#task-status-reason");
    warning.textContent = reasons.join(" ");
    warning.classList.toggle("task-warning-critical", deadline === "overdue" || task.has_blocking_dependencies);
    info.hidden = false;
}

function renderFacts(list, rows) {
    list.replaceChildren();
    for (const [label, value] of rows) {
        const group = element("div", "");
        const definition = element("dd", "");
        definition.append(value);
        group.append(element("dt", "", label), definition);
        list.append(group);
    }
}

function renderPeople(assignable = false, assignees = []) {
    canAssign = assignable;
    document.querySelector("#task-current-assignee").textContent = task.assignee
        ? `Основной исполнитель: ${task.assignee.username}` : "Основной исполнитель не назначен.";
    collaborators.replaceChildren();
    const editList = document.querySelector("#task-collaborator-edit-list");
    editList.replaceChildren();
    for (const person of task.collaborators) {
        collaborators.append(element("li", "task-badge", person.username));
        const row = element("li", "task-row");
        row.append(element("span", "", person.username));
        if (assignable) {
            const remove = element("button", "button task-secondary", "Убрать");
            remove.type = "button";
            remove.setAttribute("aria-label", `Убрать соисполнителя ${person.username}`);
            remove.addEventListener("click", () => mutate(`collaborators/${person.id}/`, "DELETE", null, "Соисполнитель убран.", {
                targetError: peopleError, dialog: peopleDialog, keepOpen: true,
            }));
            row.append(remove);
        }
        editList.append(row);
    }
    if (!task.collaborators.length) {
        editList.append(element("li", "muted", "Соисполнителей пока нет."));
    }
    document.querySelector("#task-people-heading").textContent = `Соисполнители · ${task.collaborators.length}`;
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
}

function menuTrigger() {
    return mainActions.querySelector("summary");
}

function openEdit(trigger) {
    if (busy || !editOptions) {
        return;
    }
    setupEdit(editOptions);
    editDialog.open(trigger, editForm.elements.namedItem("name"));
}

function renderActions() {
    const key = JSON.stringify([task.name, Boolean(editOptions), canChangeStatus, canAssign, task.permissions.archive]);
    if (key === menuKey) {
        return;
    }
    menuKey = key;
    const actions = [];
    if (editOptions) {
        actions.push({label: "Редактировать", onSelect: openEdit});
    }
    if (canChangeStatus) {
        actions.push({label: "Изменить статус", onSelect: (trigger) => {
            statusSelect.value = "";
            statusError.textContent = "";
            statusDialog.open(trigger, statusSelect);
        }});
    }
    if (canAssign) {
        actions.push({label: "Исполнитель и соисполнители", onSelect: (trigger) => {
            peopleError.textContent = "";
            peopleNotice.textContent = "";
            peopleDialog.open(trigger, assignee);
        }});
    }
    if (task.permissions.archive) {
        actions.push({label: "Архивировать", danger: true, onSelect: archiveTask});
    }
    mainActions.replaceChildren(...(actions.length ? [collectionActions(task.name, actions)] : []));
    const archive = mainActions.querySelector(".collection-archive-action");
    if (archive) {
        archive.id = "task-archive";
    }
}

async function archiveTask() {
    if (busy || !await confirmArchive(task.name, {
        heading: "Архивировать задачу?",
        description: `«${task.name}» останется в архивном списке при наличии доступа.`,
    })) {
        return;
    }
    return mutate("", "DELETE", null, "Задача архивирована.");
}

function setBusy(value) {
    busy = value;
    for (const panel of [info, mainActions, workflow.panel, decomposition.panel, dependencies.panel]) {
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
        editOptions = null;
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
                editOptions = options;
            }
            if (task.permissions.assign && options.assignment_mode === "direct") {
                renderPeople(true, options.assignees);
            }
        }
        renderActions();
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

async function mutate(path, method, payload, message, {
    targetError = errorBox, dialog, keepOpen = false,
} = {}) {
    if (busy) {
        return;
    }
    setBusy(true);
    dialog?.setBusy(true);
    errorBox.textContent = "";
    targetError.textContent = "";
    status.textContent = "Сохраняем изменение…";
    if (keepOpen) {
        peopleNotice.textContent = "";
    }
    try {
        await request(api + path, method, token, payload);
    } catch (error) {
        targetError.textContent = errorMessage(error);
        status.textContent = "";
        retry.hidden = false;
        setBusy(false);
        dialog?.setBusy(false);
        return;
    }

    if (!keepOpen) {
        dialog?.close();
    }
    await load(message);
    dialog?.setBusy(false);
    if (keepOpen && canAssign) {
        peopleNotice.textContent = message;
        assignee.focus();
    } else {
        if (keepOpen) {
            dialog?.close();
        }
        (menuTrigger() ?? heading).focus();
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
    return mutate("", "PATCH", payload, "Задача обновлена.", {targetError: editError, dialog: editDialog});
});
assigneeForm.addEventListener("submit", (event) => {
    event.preventDefault();
    const assignee_id = assignee.value ? Number(assignee.value) : null;
    return mutate("assignee/", "PUT", {assignee_id}, "Исполнитель обновлён.", {
        targetError: peopleError, dialog: peopleDialog, keepOpen: true,
    });
});
collaboratorForm.addEventListener("submit", (event) => {
    event.preventDefault();
    return mutate(`collaborators/${collaborator.value}/`, "PUT", null, "Соисполнитель добавлен.", {
        targetError: peopleError, dialog: peopleDialog, keepOpen: true,
    });
});
statusForm.addEventListener("submit", (event) => {
    event.preventDefault();
    if (task.workflow_status) {
        return mutate("workflow-status/", "PUT", {
            status_id: Number(statusSelect.value),
            expected_status_id: task.workflow_status.id,
        }, "Статус изменён.", {targetError: statusError, dialog: statusDialog});
    }
    return mutate("status/", "PUT", {status: statusSelect.value}, "Статус изменён.", {targetError: statusError, dialog: statusDialog});
});
retry.addEventListener("click", () => load());
load().then(() => {
    if (window.location.hash === "#edit" && editOptions) {
        openEdit(menuTrigger() ?? heading);
    } else if (window.location.hash === "#move") {
        decomposition.openMove();
    }
});

import {collectionRow, confirmArchive, setupCollection, setupDialog} from "../ui/collection.js";
import {dateLabel, deadlineState, errorMessage, priorities, readTaskFields, request, selectOptions, statuses} from "./shared.js";

const app = document.querySelector("#tasks-app");
const api = app.dataset.orgUrl;
const token = app.querySelector('[name="csrfmiddlewaretoken"]').value;
const fixedProjectId = app.dataset.projectId;
const mine = app.dataset.mine === "true";
const search = document.querySelector("#tasks-search");
const toolbar = document.querySelector("#tasks-toolbar");
const statusFilter = document.querySelector("#tasks-status-filter");
const projectFilter = document.querySelector("#tasks-project-filter");
const departmentFilter = document.querySelector("#tasks-department-filter");
const archiveFilter = document.querySelector("#tasks-archive-filter");
const results = document.querySelector("#tasks-list");
const status = document.querySelector("#tasks-status");
const errorBox = document.querySelector("#tasks-error");
const retry = document.querySelector("#tasks-retry");
const pagination = document.querySelector("#tasks-pagination");
const previous = document.querySelector("#tasks-previous");
const next = document.querySelector("#tasks-next");
const createPanel = document.querySelector("#task-create-panel");
const createOpen = document.querySelector("#task-create-open");
const notice = document.querySelector("#tasks-notice");
const form = document.querySelector("#task-create-form");
const createProject = document.querySelector("#task-create-project");
const createDepartment = document.querySelector("#task-create-department");
const createError = document.querySelector("#task-create-error");

setupCollection(app, results, "enactus-tasks-view");
const dialog = setupDialog(createPanel, createOpen);

let options;
let page = 1;
let generation = 0;
let searchTimer;

function taskUrl(task) {
    return `${app.dataset.detailUrl}${task.id}/`;
}

function taskRow(task) {
    const badges = [];
    if (mine) {
        const me = options?.current_membership_id;
        if (task.assignee?.id === me) {
            badges.push({text: "Исполнитель"});
        }
        if (task.collaborators.some(({id}) => id === me)) {
            badges.push({text: "Соисполнитель"});
        }
        const deadline = deadlineState(task);
        if (deadline) {
            badges.push(deadline === "overdue"
                ? {text: "Просрочена", kind: "danger"}
                : {text: "Срок в ближайшие 7 дней"});
        }
    }
    return collectionRow({
        title: task.name,
        href: taskUrl(task),
        status: `${statuses[task.status] ?? task.status}${task.is_archived ? " · Архив" : ""}`,
        details: [
            {label: "Исполнитель", value: task.assignee?.username ?? "Не назначен"},
            {label: "Приоритет", value: priorities[task.priority] ?? task.priority},
            {label: "Срок", value: task.due_at ? dateLabel(task.due_at) : "—"},
            {label: "Проект", value: task.project?.name},
            {label: "Подразделение", value: task.department?.name},
        ],
        badges,
        canEdit: task.permissions.update,
        canArchive: task.permissions.archive,
        onArchive: (trigger) => archiveTask(task, trigger),
    });
}

async function archiveTask(task, trigger) {
    if (!await confirmArchive(task.name)) {
        return;
    }
    errorBox.textContent = "";
    try {
        await request(`${api}tasks/${task.id}/`, "DELETE", token);
        notice.textContent = "Задача архивирована.";
        if (trigger?.isConnected && trigger.contains(document.activeElement)) {
            search.focus();
        }
        await load();
    } catch (error) {
        errorBox.textContent = errorMessage(error);
    }
}

function render(data) {
    results.replaceChildren();
    for (const task of data.items) {
        results.append(taskRow(task));
    }
    page = data.page;
    const pages = Math.max(1, Math.ceil(data.total / data.page_size));
    pagination.hidden = pages <= 1;
    document.querySelector("#tasks-page-number").textContent = `Страница ${page} из ${pages}`;
    previous.disabled = page <= 1;
    next.disabled = page >= pages;
    status.textContent = data.total ? `Найдено: ${data.total}.` : "По этим условиям задач нет.";
}

async function load() {
    const current = ++generation;
    const query = new URLSearchParams({page, page_size: 20});
    const values = {
        q: search.value.trim(),
        status: statusFilter.value,
        project_id: fixedProjectId || projectFilter?.value,
        department_id: departmentFilter.value,
        mine: mine ? "true" : "",
        include_archived: archiveFilter.checked ? "true" : "",
    };
    for (const [key, value] of Object.entries(values)) {
        if (value) {
            query.set(key, value);
        }
    }
    results.setAttribute("aria-busy", "true");
    status.textContent = "Загружаем задачи…";
    errorBox.textContent = "";
    retry.hidden = true;
    previous.disabled = true;
    next.disabled = true;
    try {
        const data = await request(`${api}tasks/?${query}`);
        if (current !== generation) {
            return;
        }
        const lastPage = Math.max(1, Math.ceil(data.total / data.page_size));
        if (data.page > lastPage) {
            page = lastPage;
            return load();
        }
        render(data);
        toolbar.hidden = false;
    } catch (error) {
        if (current !== generation) {
            return;
        }
        status.textContent = "";
        errorBox.textContent = errorMessage(error);
        retry.hidden = false;
    } finally {
        if (current === generation) {
            results.setAttribute("aria-busy", "false");
        }
    }
}

function availableProjects() {
    return options.projects.filter((project) => project.can_create || project.department_ids.length);
}

function updateCreateDepartments() {
    const project = options.projects.find(({id}) => String(id) === createProject.value);
    const candidates = options.departments.filter(({id, can_create}) =>
        project ? project.department_ids.includes(id) : can_create);
    const optional = project ? project.can_create : options.can_create_standalone;
    const selected = createDepartment.value;
    selectOptions(createDepartment, optional ? "Без подразделения" : "Выберите подразделение",
        candidates.map(({id, name}) => [id, name]), selected);
    if (createDepartment.selectedIndex < 0) {
        createDepartment.selectedIndex = 0;
    }
    createDepartment.required = !optional;
    createDepartment.options[0].disabled = !optional;
    if (!optional && !createDepartment.value && candidates.length === 1) {
        createDepartment.value = String(candidates[0].id);
    }
}

function setupCreate() {
    const projects = availableProjects();
    const standalone = options.can_create_standalone || options.departments.some(({can_create}) => can_create);
    createOpen.hidden = fixedProjectId
        ? !projects.some(({id}) => String(id) === fixedProjectId)
        : !standalone && !projects.length;
    if (createOpen.hidden) {
        return;
    }
    selectOptions(createProject, standalone ? "Без проекта" : "Выберите проект",
        projects.map(({id, name}) => [id, name]), fixedProjectId);
    createProject.required = !standalone;
    createProject.options[0].disabled = !standalone;
    if (!standalone && !fixedProjectId && projects.length === 1) {
        createProject.value = String(projects[0].id);
    }
    createProject.disabled = Boolean(fixedProjectId);
    updateCreateDepartments();
}

async function initialize() {
    let optionsError;
    try {
        options = await request(`${api}task-options/`);
        const records = (items) => items.map(({id, name}) => [id, name]);
        if (projectFilter) {
            selectOptions(projectFilter, "Все проекты", records(options.projects));
        }
        selectOptions(departmentFilter, "Все подразделения", records(options.departments));
        setupCreate();
    } catch (error) {
        optionsError = error;
    }
    await load();
    if (optionsError && !errorBox.textContent) {
        errorBox.textContent = `Не удалось загрузить параметры задач. ${errorMessage(optionsError)}`;
        retry.hidden = false;
    }
}

form.addEventListener("submit", async (event) => {
    event.preventDefault();
    createError.textContent = "";
    let payload;
    try {
        payload = readTaskFields(form);
    } catch (error) {
        createError.textContent = error.message;
        return;
    }
    payload.project_id = createProject.value ? Number(createProject.value) : null;
    payload.department_id = createDepartment.value ? Number(createDepartment.value) : null;
    dialog.setBusy(true);
    try {
        const task = await request(`${api}tasks/`, "POST", token, payload);
        dialog.setBusy(false);
        dialog.close();
        form.reset();
        setupCreate();
        const link = document.createElement("a");
        link.href = taskUrl(task);
        link.textContent = "Открыть задачу";
        notice.replaceChildren("Задача создана. ", link);
        page = 1;
        await load();
    } catch (error) {
        createError.textContent = errorMessage(error);
        dialog.setBusy(false);
    }
});

for (const [value, label] of Object.entries(statuses)) {
    statusFilter.add(new Option(label, value));
}
search.maxLength = 200;
search.addEventListener("input", () => {
    clearTimeout(searchTimer);
    generation++;
    page = 1;
    searchTimer = setTimeout(load, 250);
});
for (const filter of [statusFilter, projectFilter, departmentFilter, archiveFilter].filter(Boolean)) {
    filter.addEventListener("change", () => {
        clearTimeout(searchTimer);
        page = 1;
        load();
    });
}
previous.addEventListener("click", () => { page--; load(); });
next.addEventListener("click", () => { page++; load(); });
createProject.addEventListener("change", updateCreateDepartments);
retry.addEventListener("click", () => options ? load() : initialize());
initialize();

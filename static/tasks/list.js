import {dateLabel, deadlineState, element, errorMessage, priorities, readTaskFields, request, selectOptions, statuses} from "./shared.js";

const app = document.querySelector("#tasks-app");
const api = app.dataset.orgUrl;
const token = app.querySelector('[name="csrfmiddlewaretoken"]').value;
const fixedProjectId = app.dataset.projectId;
const mine = app.dataset.mine === "true";
const filters = document.querySelector("#tasks-filters");
const search = document.querySelector("#tasks-search");
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
const form = document.querySelector("#task-create-form");
const createProject = document.querySelector("#task-create-project");
const createDepartment = document.querySelector("#task-create-department");
const createError = document.querySelector("#task-create-error");

let options;
let page = 1;
let generation = 0;
let searchTimer;

function taskRow(task) {
    const row = element("li", "task-row");
    const summary = element("div", "");
    const title = element("a", "", task.name);
    title.href = `${app.dataset.detailUrl}${task.id}/`;
    summary.append(title);
    const facts = [task.project?.name, task.department?.name, task.assignee ? `Исполнитель: ${task.assignee.username}` : "Без исполнителя"];
    if (task.due_at) {
        facts.push(`Срок: ${dateLabel(task.due_at)}`);
    }
    summary.append(element("p", "task-meta", facts.filter(Boolean).join(" · ")));
    const badges = element("p", "");
    badges.append(element("span", "task-badge", statuses[task.status] ?? "Статус неизвестен"));
    badges.append(element("span", "task-badge", `Приоритет: ${priorities[task.priority] ?? task.priority}`));
    if (task.is_archived) {
        badges.append(element("span", "task-badge", "Архив"));
    }
    if (mine) {
        const me = options?.current_membership_id;
        if (task.assignee?.id === me) {
            badges.append(element("span", "task-badge", "Исполнитель"));
        }
        if (task.collaborators.some(({id}) => id === me)) {
            badges.append(element("span", "task-badge", "Соисполнитель"));
        }
        if (task.author?.id === me) {
            badges.append(element("span", "task-badge", "Автор"));
        }
        const deadline = deadlineState(task);
        if (deadline) {
            badges.append(element("span", `task-badge${deadline === "overdue" ? " task-badge-overdue" : ""}`,
                deadline === "overdue" ? "Просрочена" : "Срок в ближайшие 7 дней"));
        }
    }
    summary.append(badges);
    row.append(summary);
    return row;
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
    createPanel.hidden = fixedProjectId
        ? !projects.some(({id}) => String(id) === fixedProjectId)
        : !standalone && !projects.length;
    if (createPanel.hidden) {
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
    try {
        options = await request(`${api}task-options/`);
        const records = (items) => items.map(({id, name}) => [id, name]);
        if (projectFilter) {
            selectOptions(projectFilter, "Все проекты", records(options.projects));
        }
        selectOptions(departmentFilter, "Все подразделения", records(options.departments));
        setupCreate();
    } catch (error) {
        createError.textContent = errorMessage(error);
    }
    filters.hidden = false;
    await load();
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
    form.inert = true;
    try {
        const task = await request(`${api}tasks/`, "POST", token, payload);
        window.location.assign(`${app.dataset.detailUrl}${task.id}/`);
    } catch (error) {
        createError.textContent = errorMessage(error);
        form.inert = false;
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
retry.addEventListener("click", load);
initialize();

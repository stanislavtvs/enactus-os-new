import {departmentLabel, element, errorMessage, formPayload, nullableId, request, selectOptions, statuses} from "./shared.js";

const app = document.querySelector("#projects-app");
const api = app.dataset.apiUrl;
const token = app.querySelector('[name="csrfmiddlewaretoken"]').value;
const list = document.querySelector("#projects-list");
const status = document.querySelector("#projects-status");
const errorBox = document.querySelector("#projects-error");
const retry = document.querySelector("#projects-retry");
const filters = document.querySelector("#projects-filters");
const search = document.querySelector("#projects-search");
const statusFilter = document.querySelector("#projects-status-filter");
const departmentFilter = document.querySelector("#projects-department-filter");
const archivedFilter = document.querySelector("#projects-show-archived");
const results = document.querySelector("#projects-results");
const createPanel = document.querySelector("#project-create-panel");
const form = document.querySelector("#project-create-form");
const responsible = document.querySelector("#project-create-responsible");
const leadField = document.querySelector("#project-create-lead-field");
const lead = document.querySelector("#project-create-lead");

let projects = [];
let options = null;

function relatedDepartments(project) {
    return [project.responsible_department, ...project.participating_departments].filter(Boolean);
}

function projectRow(project) {
    const row = element("li", "project-row");
    const summary = element("div", "");
    const link = element("a", "", project.name);
    link.href = `${app.dataset.pageUrl}${project.id}/`;
    summary.append(link);
    const responsible = project.responsible_department;
    summary.append(element("p", "muted", responsible ? departmentLabel(responsible) : "Без ответственного подразделения"));
    row.append(summary, element("span", "project-badge",
        `${statuses[project.status] ?? project.status}${project.is_archived ? " · Архив" : ""}`));
    return row;
}

function render() {
    const query = search.value.trim().toLocaleLowerCase();
    const rows = document.createDocumentFragment();
    let count = 0;
    for (const project of projects) {
        if ((!archivedFilter.checked && project.is_archived)
            || (statusFilter.value && project.status !== statusFilter.value)
            || (departmentFilter.value && !relatedDepartments(project).some(({id}) => String(id) === departmentFilter.value))
            || !project.name.toLocaleLowerCase().includes(query)) {
            continue;
        }
        count++;
        rows.append(projectRow(project));
    }
    list.replaceChildren(rows);
    results.textContent = count ? `Найдено: ${count}.` : "По этим условиям проектов нет.";
}

function renderDepartmentFilter() {
    const departments = new Map();
    for (const project of projects) {
        for (const department of relatedDepartments(project)) {
            departments.set(department.id, departmentLabel(department));
        }
    }
    const previous = departmentFilter.value;
    selectOptions(departmentFilter, "Все подразделения", departments);
    if (departments.has(Number(previous))) {
        departmentFilter.value = previous;
    }
}

function updateLeadField() {
    const departmentId = nullableId(form, "responsible_department_id");
    const canSetLead = departmentId === null
        ? options.can_manage_independent_lead
        : options.departments.some((department) => department.id === departmentId && department.can_manage_lead);
    leadField.hidden = !canSetLead || !options.leaders.length;
    if (leadField.hidden) {
        lead.value = "";
    }
}

function setupCreate() {
    const departments = options.departments.filter((department) => department.can_create && department.can_manage_departments);
    createPanel.hidden = !options.can_create_independent && !departments.length;
    if (createPanel.hidden) {
        return;
    }
    selectOptions(responsible,
        options.can_create_independent ? "Независимый проект" : "Выберите подразделение",
        departments.map(({id, name}) => [id, name]));
    if (!options.can_create_independent) {
        responsible.options[0].disabled = true;
        responsible.required = true;
        if (departments.length === 1) {
            responsible.value = String(departments[0].id);
        }
    }
    selectOptions(lead, "Без руководителя", options.leaders.map(({id, username}) => [id, username]));
    updateLeadField();
}

async function load() {
    status.textContent = "Загружаем проекты…";
    errorBox.textContent = "";
    retry.hidden = true;
    try {
        projects = await request(api);
        renderDepartmentFilter();
        render();
        filters.hidden = false;
        status.textContent = "";
        options = await request(app.dataset.optionsUrl);
        setupCreate();
    } catch (error) {
        status.textContent = "";
        errorBox.textContent = errorMessage(error);
        retry.hidden = false;
    }
}

form.addEventListener("submit", async (event) => {
    event.preventDefault();
    errorBox.textContent = "";
    let payload;
    try {
        payload = formPayload(form);
    } catch (error) {
        errorBox.textContent = error.message;
        return;
    }
    payload.responsible_department_id = nullableId(form, "responsible_department_id");
    if (!leadField.hidden) {
        payload.lead_membership_id = nullableId(form, "lead_membership_id");
    }
    form.inert = true;
    status.textContent = "Создаём проект…";
    try {
        const project = await request(api, "POST", token, payload);
        window.location.assign(`${app.dataset.pageUrl}${project.id}/`);
    } catch (error) {
        status.textContent = "";
        errorBox.textContent = errorMessage(error);
        form.inert = false;
    }
});

responsible.addEventListener("change", updateLeadField);
search.addEventListener("input", render);
for (const filter of [statusFilter, departmentFilter, archivedFilter]) {
    filter.addEventListener("change", render);
}
retry.addEventListener("click", load);
load();

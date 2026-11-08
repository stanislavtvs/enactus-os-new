import {collectionRow, confirmArchive, setupCollection, setupDialog} from "../ui/collection.js";
import {departmentLabel, element, errorMessage, formPayload, nullableId, request, selectOptions, statuses} from "./shared.js";

const app = document.querySelector("#projects-app");
const api = app.dataset.apiUrl;
const token = app.querySelector('[name="csrfmiddlewaretoken"]').value;
const list = document.querySelector("#projects-list");
const status = document.querySelector("#projects-status");
const feedback = document.querySelector("#projects-feedback");
const errorBox = document.querySelector("#projects-error");
const retry = document.querySelector("#projects-retry");
const toolbar = document.querySelector("#projects-toolbar");
const search = document.querySelector("#projects-search");
const statusFilter = document.querySelector("#projects-status-filter");
const departmentFilter = document.querySelector("#projects-department-filter");
const archivedFilter = document.querySelector("#projects-show-archived");
const results = document.querySelector("#projects-results");
const createButton = document.querySelector("#project-create-open");
const createPanel = document.querySelector("#project-create-panel");
const form = document.querySelector("#project-create-form");
const createError = document.querySelector("#project-create-error");
const responsible = document.querySelector("#project-create-responsible");
const leadField = document.querySelector("#project-create-lead-field");
const lead = document.querySelector("#project-create-lead");
const createDialog = setupDialog(createPanel, createButton);

setupCollection(app, list, "enactus-os-projects-mode");

let projects = [];
let options = null;
let loadGeneration = 0;
let archiving = false;

function projectUrl(project) {
    return `${app.dataset.pageUrl}${project.id}/`;
}

function relatedDepartments(project) {
    return [project.responsible_department, ...project.participating_departments].filter(Boolean);
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
        rows.append(collectionRow({
            title: project.name,
            href: projectUrl(project),
            status: statuses[project.status] ?? project.status,
            details: [
                {label: "Подразделение", value: project.responsible_department
                    ? departmentLabel(project.responsible_department) : "Не назначено"},
                {label: "Руководитель", value: project.lead?.username},
            ],
            badges: project.is_archived ? [{text: "Архив"}] : [],
            canEdit: !project.is_archived && project.permissions.update,
            canArchive: !project.is_archived && project.permissions.archive,
            onArchive: (trigger) => archiveProject(project, trigger),
        }));
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
    createButton.hidden = !options.can_create_independent && !departments.length;
    if (createButton.hidden) {
        return;
    }
    selectOptions(responsible,
        options.can_create_independent ? "Независимый проект" : "Выберите подразделение",
        departments.map(({id, name}) => [id, name]));
    responsible.options[0].disabled = !options.can_create_independent;
    responsible.required = !options.can_create_independent;
    if (responsible.required && departments.length === 1) {
        responsible.value = String(departments[0].id);
    }
    selectOptions(lead, "Без руководителя", options.leaders.map(({id, username}) => [id, username]));
    updateLeadField();
}

async function load() {
    const generation = ++loadGeneration;
    status.textContent = "Загружаем проекты…";
    errorBox.textContent = "";
    retry.hidden = true;
    list.setAttribute("aria-busy", "true");
    try {
        const records = await request(api);
        if (generation !== loadGeneration) {
            return;
        }
        projects = records;
        renderDepartmentFilter();
        render();
        toolbar.hidden = false;
        if (!options) {
            options = await request(app.dataset.optionsUrl);
            setupCreate();
        }
    } catch (error) {
        if (generation === loadGeneration) {
            errorBox.textContent = errorMessage(error);
            retry.hidden = false;
        }
    } finally {
        if (generation === loadGeneration) {
            status.textContent = "";
            list.setAttribute("aria-busy", "false");
        }
    }
}

async function archiveProject(project, trigger) {
    if (archiving) {
        return;
    }
    archiving = true;
    try {
        if (!await confirmArchive(project.name)) {
            return;
        }
        errorBox.textContent = "";
        await request(`${api}${project.id}/`, "DELETE", token);
        if (trigger.contains(document.activeElement)) {
            search.focus();
        }
        project.is_archived = true;
        render();
        feedback.textContent = "Проект архивирован.";
        await load();
    } catch (error) {
        errorBox.textContent = errorMessage(error);
        retry.hidden = false;
    } finally {
        archiving = false;
    }
}

form.addEventListener("submit", async (event) => {
    event.preventDefault();
    createError.textContent = "";
    let payload;
    try {
        payload = formPayload(form);
    } catch (error) {
        createError.textContent = error.message;
        return;
    }
    payload.responsible_department_id = nullableId(form, "responsible_department_id");
    if (!leadField.hidden) {
        payload.lead_membership_id = nullableId(form, "lead_membership_id");
    }
    createDialog.setBusy(true);
    try {
        const project = await request(api, "POST", token, payload);
        createDialog.close();
        form.reset();
        setupCreate();
        projects.push(project);
        renderDepartmentFilter();
        render();
        const link = element("a", "", project.name);
        link.href = projectUrl(project);
        feedback.replaceChildren("Проект создан. Открыть: ", link);
        await load();
    } catch (error) {
        createError.textContent = errorMessage(error);
    } finally {
        createDialog.setBusy(false);
    }
});

responsible.addEventListener("change", updateLeadField);
search.addEventListener("input", render);
for (const filter of [statusFilter, departmentFilter, archivedFilter]) {
    filter.addEventListener("change", render);
}
retry.addEventListener("click", load);
load();

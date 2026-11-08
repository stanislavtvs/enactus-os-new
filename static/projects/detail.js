import {departmentLabel, element, errorMessage, formPayload, nullableId, request, selectOptions, statuses} from "./shared.js";

const app = document.querySelector("#project-app");
const api = `${app.dataset.apiUrl}${app.dataset.projectId}/`;
const token = app.querySelector('[name="csrfmiddlewaretoken"]').value;
const heading = document.querySelector("#project-heading");
const status = document.querySelector("#project-load-status");
const errorBox = document.querySelector("#project-error");
const retry = document.querySelector("#project-retry");
const info = document.querySelector("#project-info");
const editPanel = document.querySelector("#project-edit-panel");
const editForm = document.querySelector("#project-edit-form");
const archive = document.querySelector("#project-archive");
const settings = document.querySelector("#project-settings-panel");
const responsibleForm = document.querySelector("#project-responsible-form");
const leadForm = document.querySelector("#project-lead-form");
const departmentPanel = document.querySelector("#project-departments-panel");
const departments = document.querySelector("#project-departments-list");
const departmentsEmpty = document.querySelector("#project-departments-empty");
const departmentForm = document.querySelector("#project-department-form");

let project;

function showText(selector, text) {
    document.querySelector(selector).textContent = text;
}

function calendarDate(value) {
    return value
        ? new Intl.DateTimeFormat("ru", {dateStyle: "long", timeZone: "UTC"}).format(new Date(`${value}T00:00:00Z`))
        : "Не указана";
}

function renderInfo() {
    heading.textContent = project.name;
    document.title = `${project.name} — ${app.dataset.orgName} — Enactus OS`;
    showText("#project-info-status", `${statuses[project.status] ?? project.status}${project.is_archived ? " · Архив" : ""}`);
    showText("#project-info-start", calendarDate(project.start_date));
    showText("#project-info-end", calendarDate(project.planned_end_date));
    showText("#project-info-lead", project.lead?.username ?? "Не назначен");
    showText("#project-info-responsible", project.responsible_department
        ? departmentLabel(project.responsible_department) : "Независимый проект");
    showText("#project-info-description", project.description);
    info.hidden = false;
    archive.hidden = project.is_archived || !project.permissions.archive;
    editPanel.hidden = project.is_archived || !project.permissions.update;
    if (!editPanel.hidden) {
        for (const field of ["name", "description", "status", "start_date", "planned_end_date"]) {
            editForm.elements.namedItem(field).value = project[field] ?? "";
        }
    }
}

function includeCurrent(choices, current, name) {
    if (current && !choices.some(([id]) => id === current.id)) {
        choices.unshift([current.id, name]);
    }
    return choices;
}

function setupSettings(options) {
    const canDepartments = !project.is_archived && project.permissions.manage_departments;
    const canLead = !project.is_archived && project.permissions.manage_lead;
    responsibleForm.hidden = !canDepartments;
    leadForm.hidden = !canLead;
    settings.hidden = !canDepartments && !canLead;
    if (canDepartments) {
        const current = project.responsible_department;
        const choices = includeCurrent(options.departments
            .filter((department) => department.can_set_responsible)
            .map(({id, name}) => [id, name]), current, current && departmentLabel(current));
        const select = responsibleForm.elements.namedItem("responsible_department_id");
        selectOptions(select, options.can_set_independent ? "Независимый проект" : "Выберите подразделение", choices, current?.id);
        select.options[0].disabled = !options.can_set_independent;
        select.required = !options.can_set_independent;
        responsibleForm.querySelector("button").disabled = true;
    }
    if (canLead) {
        const choices = includeCurrent(options.leaders.map(({id, username}) => [id, username]), project.lead, project.lead?.username);
        selectOptions(leadForm.elements.namedItem("lead_membership_id"), "Без руководителя", choices, project.lead?.id);
    }
    renderDepartments(options, canDepartments);
}

function renderDepartments(options, canDepartments) {
    const managed = new Set(options.departments.filter((department) => department.can_manage_departments).map(({id}) => id));
    const included = new Set(project.participating_departments.map(({id}) => id));
    departments.replaceChildren();
    for (const department of project.participating_departments) {
        const row = element("li", "project-row");
        row.append(element("span", "", departmentLabel(department)));
        if (canDepartments && (managed.has(department.id) || options.can_manage_independent)) {
            const button = element("button", "button project-secondary", "Убрать");
            button.type = "button";
            button.setAttribute("aria-label", `Убрать подразделение ${department.name}`);
            button.addEventListener("click", () => mutate(`departments/${department.id}/`, "DELETE", null, "Подразделение убрано."));
            row.append(button);
        }
        departments.append(row);
    }
    const candidates = options.departments.filter((department) =>
        department.can_manage_departments && !included.has(department.id));
    selectOptions(document.querySelector("#project-department"), "Выберите подразделение",
        candidates.map(({id, name}) => [id, name]));
    departmentForm.hidden = !canDepartments || !candidates.length;
    departmentsEmpty.hidden = included.size !== 0;
    departmentPanel.hidden = false;
}

function setBusy(busy) {
    for (const panel of [info, editPanel, settings, departmentPanel]) {
        panel.inert = busy;
    }
}

async function load(message = "") {
    setBusy(true);
    retry.hidden = true;
    errorBox.textContent = "";
    status.textContent = message || "Загружаем проект…";
    try {
        project = await request(api);
        renderInfo();
        renderDepartments({departments: []}, false);
        settings.hidden = true;
        if (!project.is_archived && (project.permissions.manage_departments || project.permissions.manage_lead)) {
            const options = await request(`${app.dataset.optionsUrl}?project_id=${encodeURIComponent(app.dataset.projectId)}`);
            setupSettings(options);
        } else {
            settings.hidden = true;
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

async function mutate(path, method, payload, message) {
    setBusy(true);
    errorBox.textContent = "";
    status.textContent = "Сохраняем изменение…";
    try {
        await request(api + path, method, token, payload);
    } catch (error) {
        errorBox.textContent = errorMessage(error);
        retry.hidden = false;
        status.textContent = "";
        setBusy(false);
        return;
    }
    if (await load(message)) {
        heading.focus();
    }
}

editForm.addEventListener("submit", (event) => {
    event.preventDefault();
    errorBox.textContent = "";
    let payload;
    try {
        payload = formPayload(editForm);
    } catch (error) {
        errorBox.textContent = error.message;
        return;
    }
    return mutate("", "PATCH", payload, "Сведения о проекте сохранены.");
});
responsibleForm.addEventListener("submit", (event) => {
    event.preventDefault();
    const responsible_department_id = nullableId(responsibleForm, "responsible_department_id");
    return mutate("responsible-department/", "PUT", {responsible_department_id}, "Ответственное подразделение обновлено.");
});
responsibleForm.elements.namedItem("responsible_department_id").addEventListener("change", (event) => {
    event.target.form.querySelector("button").disabled = event.target.value === String(project.responsible_department?.id ?? "");
});
leadForm.addEventListener("submit", (event) => {
    event.preventDefault();
    const lead_membership_id = nullableId(leadForm, "lead_membership_id");
    return mutate("lead/", "PUT", {lead_membership_id}, "Руководитель обновлён.");
});
departmentForm.addEventListener("submit", (event) => {
    event.preventDefault();
    const id = document.querySelector("#project-department").value;
    return mutate(`departments/${id}/`, "PUT", null, "Подразделение добавлено.");
});
archive.addEventListener("click", () => {
    if (window.confirm(`Архивировать проект «${project.name}»? Он останется доступен в архивном списке при наличии прав.`)) {
        return mutate("", "DELETE", null, "Проект архивирован.");
    }
});
retry.addEventListener("click", () => load());
load().then(() => {
    if (window.location.hash === "#edit" && !editPanel.hidden) {
        editPanel.querySelector('[name="name"]').focus();
    }
});

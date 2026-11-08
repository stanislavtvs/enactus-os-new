import {action, element, errorMessage, request} from "../departments/directory.js";

const app = document.querySelector("#organization-access-app");
const api = app.dataset.apiUrl;
const isOwner = app.dataset.owner === "true";
const viewerUsername = app.dataset.username;
const token = app.querySelector('[name="csrfmiddlewaretoken"]')?.value;
const admin = document.querySelector("#access-admin");
const roleForm = document.querySelector("#access-role-form");
const assignmentForm = document.querySelector("#access-assignment-form");
const positionForm = document.querySelector("#access-position-form");
const permissionOptions = document.querySelector("#access-permission-options");
const context = document.querySelector("#access-effective-department");
const contextPanel = document.querySelector("#access-effective-context");
const effectiveList = document.querySelector("#access-effective-list");
const effectiveStatus = document.querySelector("#access-effective-status");
const effectiveError = document.querySelector("#access-effective-error");
const effectiveRetry = document.querySelector("#access-effective-retry");
const adminStatus = document.querySelector("#access-admin-status");
const adminError = document.querySelector("#access-admin-error");
const adminRetry = document.querySelector("#access-admin-retry");
const rolesList = document.querySelector("#access-role-list");
const assignmentsList = document.querySelector("#access-assignment-list");
const positionsList = document.querySelector("#access-position-list");

let catalog = [];
let roleById = new Map();
let subjectById = new Map();
let editingRole = null;
let adminBusy = false;
let effectiveVersion = 0;

const roleTemplates = {
    coordinator: {
        name: "Координатор подразделения",
        codes: ["departments.directory.read", "departments.invitations.manage", "departments.members.manage"],
    },
    observer: {name: "Наблюдатель ролей", codes: ["roles.read"]},
};

function selectOptions(select, placeholder, records) {
    select.replaceChildren(new Option(placeholder, ""));
    for (const [id, label] of records) {
        select.add(new Option(label, String(id)));
    }
}

function permissionLabels() {
    return new Map(catalog.map((permission) => [permission.code, permission.label]));
}

async function loadEffective() {
    const version = ++effectiveVersion;
    context.disabled = true;
    effectiveRetry.hidden = true;
    effectiveList.replaceChildren();
    effectiveStatus.textContent = "Проверяем разрешения…";
    effectiveError.textContent = "";
    try {
        const query = context.value ? `?department_id=${encodeURIComponent(context.value)}` : "";
        const {permissions} = await request(`${api}permissions/effective/${query}`);
        if (version !== effectiveVersion) {
            return;
        }
        const labels = permissionLabels();
        for (const code of permissions) {
            effectiveList.append(element("li", "", labels.get(code) ?? code));
        }
        effectiveStatus.textContent = permissions.length ? "" : "В выбранной области дополнительных разрешений нет.";
    } catch (error) {
        if (version !== effectiveVersion) {
            return;
        }
        effectiveStatus.textContent = "";
        effectiveError.textContent = errorMessage(error);
        effectiveRetry.hidden = false;
    } finally {
        if (version === effectiveVersion) {
            context.disabled = false;
        }
    }
}

async function initializeEffective() {
    const permissions = request(`${api}permissions/`).then((items) => { catalog = items; }).catch(() => {});
    const contexts = request(`${api}departments/`).then((departments) => {
        const active = departments.filter((department) => !department.is_archived);
        const counts = new Map();
        for (const department of active) {
            counts.set(department.name, (counts.get(department.name) ?? 0) + 1);
        }
        const selected = context.value;
        selectOptions(context, "Организация", active.map(({id, name}) =>
            [id, counts.get(name) > 1 ? `${name} · №${id}` : name]));
        if (active.some(({id}) => String(id) === selected)) {
            context.value = selected;
        }
        contextPanel.hidden = false;
    }).catch(() => {
        contextPanel.hidden = true;
    });
    await Promise.all([permissions, contexts]);
    await loadEffective();
}

function setAdminBusy(busy) {
    adminBusy = busy;
    adminRetry.disabled = busy;
    for (const list of [rolesList, assignmentsList, positionsList]) {
        list.setAttribute("aria-busy", String(busy));
        for (const button of list.querySelectorAll("button")) {
            button.disabled = busy;
        }
    }
    if (roleForm) {
        for (const form of [roleForm, assignmentForm, positionForm]) {
            form.inert = busy;
        }
    }
}

function record(title, details) {
    const row = element("li", "access-record");
    const summary = element("div", "");
    summary.append(element("p", "access-record-title", title), element("p", "access-record-details", details));
    row.append(summary);
    return row;
}

function renderPermissionEditor(selected = []) {
    permissionOptions.replaceChildren();
    const previous = new Map(selected.map((permission) => [permission.code, permission.include_descendants]));
    for (const permission of catalog) {
        const line = element("div", "access-option");
        const label = element("label", "");
        const enabled = document.createElement("input");
        enabled.type = "checkbox";
        enabled.name = "permission";
        enabled.value = permission.code;
        enabled.checked = previous.has(permission.code);
        enabled.disabled = !isOwner && permission.code === "roles.manage";
        label.append(enabled, document.createTextNode(permission.label));
        line.append(label);
        if (permission.department_scoped) {
            const inheritLabel = element("label", "");
            const inherit = document.createElement("input");
            inherit.type = "checkbox";
            inherit.name = "inheritance";
            inherit.value = permission.code;
            inherit.checked = previous.get(permission.code) === true;
            inherit.disabled = !enabled.checked;
            inherit.setAttribute("aria-label", `Включая дочерние: ${permission.label}`);
            enabled.addEventListener("change", () => {
                inherit.disabled = !enabled.checked;
                if (!enabled.checked) {
                    inherit.checked = false;
                }
            });
            inheritLabel.append(inherit, document.createTextNode("Включая дочерние"));
            line.append(inheritLabel);
        }
        permissionOptions.append(line);
    }
}

function editRole(role = null, focus = false) {
    editingRole = role?.id ?? null;
    document.querySelector("#access-role-form-heading").textContent = role ? "Редактирование роли" : "Новая роль";
    const name = roleForm.elements.namedItem("name");
    name.value = role?.name ?? "";
    renderPermissionEditor(role?.permissions ?? []);
    document.querySelector("#access-role-save").textContent = role ? "Сохранить роль" : "Создать роль";
    document.querySelector("#access-role-cancel").hidden = !role;
    document.querySelector("#access-role-preset").hidden = Boolean(role);
    if (focus) {
        name.focus();
    }
}

function archiveRole(role) {
    if (window.confirm(`Архивировать роль «${role.name}»? Её назначения перестанут действовать.`)) {
        return mutate(`${api}roles/${role.id}/`, "DELETE", null, "Роль архивирована.");
    }
}

function renderRoles(roles, assignments) {
    const labels = permissionLabels();
    const assignedRoleIds = new Set(assignments.filter((item) => !item.revoked_at).map((item) => item.role_id));
    rolesList.replaceChildren();
    for (const role of roles) {
        const details = role.permissions.map(({code, include_descendants}) =>
            `${labels.get(code) ?? code}${include_descendants ? " (включая дочерние)" : ""}`);
        const row = record(role.name, `${role.is_archived ? "В архиве · " : ""}${details.join(", ") || "Без разрешений"}`);
        const canEdit = isOwner || (!assignedRoleIds.has(role.id)
            && !role.permissions.some(({code}) => code === "roles.manage"));
        if (roleForm && !role.is_archived && canEdit) {
            const actions = element("div", "access-actions");
            actions.append(action("Редактировать", `Редактировать роль ${role.name}`, () => editRole(role, true)),
                action("Архивировать", `Архивировать роль ${role.name}`, () => archiveRole(role)));
            row.append(actions);
        }
        rolesList.append(row);
    }
    if (!roles.length) {
        rolesList.append(element("li", "muted", "Ролей пока нет."));
    }
}

function scopeLabel(item) {
    if (item.department_membership_id === null) {
        return "Организация";
    }
    const subject = subjectById.get(item.organization_membership_id);
    return subject?.department_memberships.find(({id}) => id === item.department_membership_id)?.name
        ?? "Прежнее членство в подразделении";
}

function renderRecords(list, records, url, titleFor, empty, revokeLabel, success) {
    list.replaceChildren();
    for (const item of records) {
        const title = titleFor(item);
        const username = subjectById.get(item.organization_membership_id)?.username ?? "Прежний участник";
        const row = record(title, `${username} · ${scopeLabel(item)}${item.revoked_at ? " · Отозвано" : ""}`);
        if (roleForm && !item.revoked_at && (isOwner || username !== viewerUsername)) {
            row.append(action(revokeLabel, `${revokeLabel}: ${title}, ${username}`, () =>
                mutate(`${api}${url}/${item.id}/`, "DELETE", null, success)));
        }
        list.append(row);
    }
    if (!records.length) {
        list.append(element("li", "muted", empty));
    }
}

function updateScope(form) {
    const subjectId = Number(form.elements.namedItem("organization_membership_id").value);
    const memberships = subjectById.get(subjectId)?.department_memberships ?? [];
    const role = form === assignmentForm
        ? roleById.get(Number(form.elements.namedItem("role_id").value))
        : null;
    const orgOnly = role?.permissions.some(({code}) =>
        catalog.some((permission) => permission.code === code && !permission.department_scoped));
    const select = form.elements.namedItem("department_membership_id");
    const selected = select.value;
    selectOptions(select, "Организация", orgOnly ? [] : memberships.map(({id, name}) => [id, name]));
    if (!orgOnly && memberships.some(({id}) => String(id) === selected)) {
        select.value = selected;
    }
}

function prepareForms(roles, subjects) {
    if (!roleForm) {
        return;
    }
    editRole();
    selectOptions(assignmentForm.elements.namedItem("role_id"), "Выберите роль",
        roles.filter((role) => !role.is_archived
            && (isOwner || !role.permissions.some(({code}) => code === "roles.manage")))
            .map(({id, name}) => [id, name]));
    for (const form of [assignmentForm, positionForm]) {
        selectOptions(form.elements.namedItem("organization_membership_id"), "Выберите участника",
            subjects.filter((subject) => isOwner || subject.username !== viewerUsername)
                .map(({id, username}) => [id, username]));
        updateScope(form);
        form.hidden = false;
    }
    roleForm.hidden = false;
}

async function loadAdmin(message = "") {
    setAdminBusy(true);
    adminRetry.hidden = true;
    adminStatus.textContent = message || "Загружаем роли и назначения…";
    adminError.textContent = "";
    try {
        const [permissions, roles, subjects, assignments, positions] = await Promise.all([
            request(`${api}permissions/`),
            request(`${api}roles/`),
            request(`${api}role-subjects/`),
            request(`${api}role-assignments/`),
            request(`${api}positions/`),
        ]);
        catalog = permissions;
        roleById = new Map(roles.map((role) => [role.id, role]));
        subjectById = new Map(subjects.map((subject) => [subject.id, subject]));
        renderRoles(roles, assignments);
        renderRecords(assignmentsList, assignments, "role-assignments",
            (item) => roleById.get(item.role_id)?.name ?? "Прежняя роль", "Назначений пока нет.", "Отозвать назначение", "Назначение отозвано.");
        renderRecords(positionsList, positions, "positions", (item) => item.title, "Должностей пока нет.", "Снять должность", "Должность снята.");
        prepareForms(roles, subjects);
        adminStatus.textContent = message;
        return true;
    } catch (error) {
        adminStatus.textContent = message;
        adminError.textContent = errorMessage(error);
        adminRetry.hidden = false;
        return false;
    } finally {
        setAdminBusy(false);
    }
}

async function mutate(url, method, payload, message) {
    if (adminBusy) {
        return;
    }
    setAdminBusy(true);
    adminError.textContent = "";
    adminStatus.textContent = "";
    try {
        await request(url, method, token, payload);
    } catch (error) {
        adminError.textContent = errorMessage(error);
        adminRetry.hidden = false;
        setAdminBusy(false);
        return;
    }
    const loaded = await loadAdmin(message);
    await loadEffective();
    if (loaded) {
        document.querySelector("#access-roles-heading").focus();
    }
}

function textValue(form, field) {
    const input = form.elements.namedItem(field);
    const value = input.value.trim();
    if (!value || Array.from(value).length > 120) {
        adminError.textContent = "Введите название длиной от 1 до 120 символов.";
        input.focus();
        return null;
    }
    return value;
}

function subjectPayload(form) {
    const departmentMembership = form.elements.namedItem("department_membership_id").value;
    return {
        organization_membership_id: Number(form.elements.namedItem("organization_membership_id").value),
        department_membership_id: departmentMembership ? Number(departmentMembership) : null,
    };
}

if (roleForm) {
    document.querySelector("#access-role-cancel").addEventListener("click", () => editRole(null, true));
    document.querySelector("#access-fill-template").addEventListener("click", () => {
        const preset = roleTemplates[document.querySelector("#access-role-template").value];
        roleForm.elements.namedItem("name").value = preset.name;
        renderPermissionEditor(preset.codes.map((code) => ({code, include_descendants: false})));
        roleForm.elements.namedItem("name").focus();
    });
    roleForm.addEventListener("submit", (event) => {
        event.preventDefault();
        const name = textValue(roleForm, "name");
        if (name === null) {
            return;
        }
        const values = new FormData(roleForm);
        const inherited = new Set(values.getAll("inheritance"));
        const permissions = values.getAll("permission").map((code) => ({code, include_descendants: inherited.has(code)}));
        if (editingRole === null) {
            return mutate(`${api}roles/`, "POST", {name, permissions}, "Роль создана.");
        }
        return mutate(`${api}roles/${editingRole}/`, "PATCH", {name, permissions}, "Роль обновлена.");
    });
    for (const form of [assignmentForm, positionForm]) {
        form.elements.namedItem("organization_membership_id").addEventListener("change", () => updateScope(form));
    }
    assignmentForm.elements.namedItem("role_id").addEventListener("change", () => updateScope(assignmentForm));
    assignmentForm.addEventListener("submit", (event) => {
        event.preventDefault();
        const role_id = Number(assignmentForm.elements.namedItem("role_id").value);
        return mutate(`${api}role-assignments/`, "POST", {role_id, ...subjectPayload(assignmentForm)}, "Роль назначена.");
    });
    positionForm.addEventListener("submit", (event) => {
        event.preventDefault();
        const title = textValue(positionForm, "title");
        if (title !== null) {
            return mutate(`${api}positions/`, "POST", {title, ...subjectPayload(positionForm)}, "Должность добавлена.");
        }
    });
}

context.addEventListener("change", loadEffective);
effectiveRetry.addEventListener("click", initializeEffective);
initializeEffective();
if (admin) {
    adminRetry.addEventListener("click", () => loadAdmin());
    loadAdmin();
}

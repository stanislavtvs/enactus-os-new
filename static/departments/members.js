import {action, element, errorMessage, invitationStatus, request} from "./directory.js";

const app = document.querySelector("#department-members-app");
const departmentUrl = app.dataset.departmentUrl;
const organizationUrl = app.dataset.organizationUrl;
const token = app.querySelector('[name="csrfmiddlewaretoken"]')?.value;
const canManage = app.dataset.canManage === "true";
const list = document.querySelector("#department-members-list");
const history = document.querySelector("#department-invitation-history");
const historyEmpty = document.querySelector("#department-history-empty");
const filters = document.querySelector("#department-members-filters");
const search = document.querySelector("#department-members-search");
const scope = document.querySelector("#department-members-scope");
const result = document.querySelector("#department-members-results");
const status = document.querySelector("#department-members-status");
const errorBox = document.querySelector("#department-members-error");
const retry = document.querySelector("#department-members-retry");
const heading = document.querySelector("#department-members-heading");
const policyPanel = document.querySelector("#department-policy-panel");
const policyForm = document.querySelector("#department-directory-policy");

let branch = [];
let others = [];
let invitations = [];
let pendingByMembership = new Set();

function setBusy(busy) {
    for (const control of app.querySelectorAll("button, input, select")) {
        control.disabled = busy;
    }
    list.setAttribute("aria-busy", String(busy));
}

function invite(person) {
    return mutate(`${departmentUrl}invitations/`, "POST", {
        organization_membership_id: person.organization_membership_id,
    }, "Приглашение отправлено.");
}

function removeMembership(person, membership) {
    const path = membership.path.join(" → ");
    if (!window.confirm(`Исключить «${person.username}» из подразделения «${path}»? Другие членства сохранятся.`)) {
        return;
    }
    return mutate(`${organizationUrl}departments/${membership.department_id}/members/${membership.id}/`,
        "DELETE", null, "Членство удалено.");
}

function personRow(person, outside) {
    const row = element("li", outside ? "department-directory-row is-outside" : "department-directory-row");
    const summary = element("div", "");
    summary.append(element("p", "department-name", person.username));

    if (outside) {
        summary.append(element("p", "department-description", "Вне этой ветки"));
    }

    const paths = element("ul", "department-membership-paths");
    for (const membership of person.memberships) {
        const path = membership.path.join(" → ");
        const label = membership.is_direct ? "Прямое членство" : "Во вложенном подразделении";
        const entry = element("li", "", `${label}: ${path}`);
        if (canManage && !outside) {
            entry.append(action("Исключить", `Исключить ${person.username} из ${path}`,
                () => removeMembership(person, membership)));
        }
        paths.append(entry);
    }
    if (paths.childElementCount) {
        summary.append(paths);
    }
    row.append(summary);

    const isDirect = person.memberships.some((membership) => membership.is_direct);
    if (canManage && !isDirect) {
        if (pendingByMembership.has(person.organization_membership_id)) {
            row.append(element("span", "department-description", "Приглашение отправлено"));
        } else {
            row.append(action("Пригласить", `Пригласить ${person.username}`, () => invite(person), true));
        }
    }
    return row;
}

function renderMembers() {
    const query = search.value.trim().toLocaleLowerCase();
    const outside = scope.value === "others";
    const people = outside ? others : branch;
    const rows = [];
    for (const person of people) {
        if (scope.value === "direct" && !person.memberships.some((membership) => membership.is_direct)) {
            continue;
        }
        if (scope.value === "descendants" && !person.memberships.some((membership) => !membership.is_direct)) {
            continue;
        }
        const searchText = [person.username, ...person.memberships.flatMap((membership) => membership.path)].join(" ").toLocaleLowerCase();
        if (searchText.includes(query)) {
            rows.push(personRow(person, outside));
        }
    }
    list.replaceChildren(...rows);
    result.textContent = rows.length ? `Найдено: ${rows.length}.` : "По этим условиям никого не найдено.";
}

function renderHistory() {
    if (!history) {
        return;
    }
    const rows = invitations.map((invitation) => {
        const row = element("li", "department-directory-row");
        const summary = element("div", "");
        summary.append(element("p", "department-name", invitation.username));
        const timestamp = new Date(invitation.created_at).toLocaleString();
        summary.append(element("p", "department-description", `${invitationStatus(invitation.status)} · ${timestamp}`));
        row.append(summary);
        if (invitation.status === "pending") {
            row.append(action("Отозвать", `Отозвать приглашение для ${invitation.username}`, () =>
                mutate(`${departmentUrl}invitations/${invitation.id}/`, "DELETE", null, "Приглашение отозвано.")));
        }
        return row;
    });
    history.replaceChildren(...rows);
    historyEmpty.hidden = rows.length !== 0;
}

async function load(message = "") {
    setBusy(true);
    status.textContent = message || "Загружаем каталог…";
    errorBox.textContent = "";
    retry.hidden = true;
    try {
        const showOthers = canManage || app.dataset.visibility === "organization";
        [branch, others, invitations] = await Promise.all([
            request(`${departmentUrl}members/?scope=branch`),
            showOthers ? request(`${departmentUrl}members/?scope=others`) : [],
            canManage ? request(`${departmentUrl}invitations/`) : [],
        ]);
        pendingByMembership = new Set(invitations.filter((item) => item.status === "pending")
            .map((item) => item.organization_membership_id));
        renderMembers();
        renderHistory();
        filters.hidden = false;
        if (policyPanel) {
            policyPanel.hidden = false;
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

async function mutate(url, method, payload, message) {
    setBusy(true);
    errorBox.textContent = "";
    status.textContent = "";
    try {
        await request(url, method, token, payload);
    } catch (error) {
        errorBox.textContent = errorMessage(error);
        retry.hidden = false;
        setBusy(false);
        return;
    }
    if (await load(message)) {
        heading.focus();
    }
}

search.addEventListener("input", renderMembers);
scope.addEventListener("change", renderMembers);
retry.addEventListener("click", () => load());

policyForm?.addEventListener("submit", (event) => {
    event.preventDefault();
    const visibility = policyForm.elements.namedItem("visibility").value;
    return mutate(`${organizationUrl}department-directory-policy/`, "PATCH", {visibility}, "Видимость каталога обновлена.");
});

load();

import {action, element, errorMessage, invitationStatus, request} from "./directory.js";

const app = document.querySelector("#department-invitation-app");
const apiUrl = app.dataset.apiUrl;
const token = app.querySelector('[name="csrfmiddlewaretoken"]').value;
const list = document.querySelector("#department-invitations-list");
const status = document.querySelector("#department-invitation-status");
const errorBox = document.querySelector("#department-invitation-error");
const retry = document.querySelector("#department-invitation-retry");
const heading = document.querySelector("#department-invitation-heading");

function setBusy(busy) {
    list.setAttribute("aria-busy", String(busy));
    for (const button of app.querySelectorAll("button")) {
        button.disabled = busy;
    }
}

function invitationRow(invitation) {
    const row = element("li", "department-directory-row");
    const summary = element("div", "");
    summary.append(
        element("p", "department-name", `${invitation.organization_name} · ${invitation.department_name}`),
        element("p", "department-description", invitationStatus(invitation.status)),
    );
    row.append(summary);
    if (invitation.status === "pending") {
        const actions = element("div", "department-actions");
        const target = `${invitation.organization_name} · ${invitation.department_name}`;
        actions.append(
            action("Принять", `Принять приглашение: ${target}`, () => respond(invitation, "accept"), true),
            action("Отклонить", `Отклонить приглашение: ${target}`, () => respond(invitation, "decline")),
        );
        row.append(actions);
    }
    return row;
}

async function load(message = "") {
    setBusy(true);
    status.textContent = message || "Загружаем приглашения…";
    errorBox.textContent = "";
    retry.hidden = true;
    try {
        const invitations = await request(apiUrl);
        list.replaceChildren(...invitations.map(invitationRow));
        status.textContent = message || (invitations.length ? "" : "У вас пока нет приглашений в подразделения.");
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

async function respond(invitation, decision) {
    setBusy(true);
    status.textContent = "";
    errorBox.textContent = "";
    try {
        await request(`${apiUrl}${invitation.id}/respond/`, "POST", token, {decision});
    } catch (error) {
        errorBox.textContent = errorMessage(error);
        retry.hidden = false;
        setBusy(false);
        return;
    }
    const message = decision === "accept" ? "Приглашение принято." : "Приглашение отклонено.";
    if (await load(message)) {
        heading.focus();
    }
}

retry.addEventListener("click", () => load());
load();

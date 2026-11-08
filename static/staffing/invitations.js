import {allocationStatuses, button, dateTime, element, listRow, pageState, request} from "./shared.js";

const app = document.querySelector("#project-invitations-app");
const api = app.dataset.apiUrl;
const page = pageState(app);
const list = document.querySelector("#project-invitations-list");
const heading = document.querySelector("#project-invitations-heading");

function render(invitations) {
    list.replaceChildren();
    for (const invitation of invitations) {
        const title = `${invitation.organization_name} · ${invitation.project_name}`;
        const status = allocationStatuses[invitation.status] ?? "Статус неизвестен";
        const row = listRow(title, [invitation.department_name, status, dateTime(invitation.created_at)].filter(Boolean).join(" · "));
        if (invitation.status === "pending") {
            const actions = element("div", "staffing-actions");
            const accept = button("Принять", () => respond(invitation, "accept"), true);
            accept.setAttribute("aria-label", `Принять приглашение: ${title}`);
            const decline = button("Отклонить", () => respond(invitation, "decline"));
            decline.setAttribute("aria-label", `Отклонить приглашение: ${title}`);
            actions.append(accept, decline);
            row.append(actions);
        }
        list.append(row);
    }
}

async function load(message = "") {
    page.start(message || "Загружаем приглашения…");
    try {
        const invitations = await request(api);
        render(invitations);
        page.finish(message || (invitations.length ? "" : "Приглашений в проекты пока нет."));
        return true;
    } catch (error) {
        page.fail(error, message);
        return false;
    }
}

async function respond(invitation, decision) {
    if (page.busy) {
        return;
    }
    page.start("Сохраняем ответ…");
    try {
        await page.mutate(`${api}${invitation.id}/respond/`, "POST", {decision});
    } catch (error) {
        page.fail(error);
        return;
    }
    if (await load(decision === "accept" ? "Приглашение принято." : "Приглашение отклонено.")) {
        heading.focus();
    }
}

page.retry.addEventListener("click", () => load());
load();

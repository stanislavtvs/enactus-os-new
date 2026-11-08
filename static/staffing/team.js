import {button, listRow, pageState, request, requestRow, formPayload} from "./shared.js";
import {selectOptions} from "../projects/shared.js";

const app = document.querySelector("#project-team-app");
const api = app.dataset.projectUrl;
const page = pageState(app);
const members = document.querySelector("#project-team-members");
const requests = document.querySelector("#project-staffing-list");
const requestsPanel = document.querySelector("#project-staffing-panel");
const createPanel = document.querySelector("#project-staffing-create-panel");
const form = document.querySelector("#project-staffing-create-form");
const heading = document.querySelector("#team-members-heading");

function renderTeam(team) {
    document.querySelector("#project-team-panel").hidden = !team.can_view_team;
    document.querySelector("#project-team-lead").textContent =
        team.lead ? `Руководитель: ${team.lead.username}` : "Руководитель не назначен.";
    members.replaceChildren();
    const active = team.members.filter((item) => item.is_active);
    for (const member of team.members) {
        const meta = [member.source_department_name || "Подразделение не указано"];
        if (!member.is_active) {
            meta.push("Больше не в команде");
        }
        const row = listRow(member.username, meta.join(" · "));
        if (team.can_manage_team && member.is_active) {
            const remove = button("Исключить", () => removeMember(member));
            remove.setAttribute("aria-label", `Исключить ${member.username} из проекта`);
            row.append(remove);
        }
        members.append(row);
    }
    document.querySelector("#project-team-empty").hidden = active.length !== 0;
}

async function load(message = "") {
    page.start(message || "Загружаем команду…");
    try {
        const team = await request(`${api}team/`);
        renderTeam(team);
        requests.replaceChildren();
        requestsPanel.hidden = true;
        createPanel.hidden = true;
        if (team.can_view_requests) {
            const staffingRequests = await request(`${api}staffing-requests/`);
            for (const item of staffingRequests) {
                requests.append(requestRow(item, app.dataset.queueUrl));
            }
            document.querySelector("#project-staffing-empty").hidden = staffingRequests.length !== 0;
            requestsPanel.hidden = false;
        }
        if (team.can_request) {
            const allDepartments = await request(`${app.dataset.orgUrl}departments/`);
            const departments = allDepartments.filter((department) => !department.is_archived);
            selectOptions(form.elements.namedItem("department_id"), "Выберите подразделение",
                departments.map(({id, name}) => [id, name]));
            createPanel.hidden = departments.length === 0;
        }
        page.finish(message);
        return true;
    } catch (error) {
        page.fail(error, message);
        return false;
    }
}

async function removeMember(member) {
    if (page.busy || !window.confirm(`Исключить «${member.username}» из команды? Членство в подразделении сохранится.`)) {
        return;
    }
    page.start("Сохраняем изменение…");
    try {
        await page.mutate(`${api}team/${member.id}/`, "DELETE");
    } catch (error) {
        page.fail(error);
        return;
    }
    if (await load("Участник исключён из команды.")) {
        heading.focus();
    }
}

form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (page.busy) {
        return;
    }
    let payload;
    try {
        payload = {
            ...formPayload(form),
            department_id: Number(form.elements.namedItem("department_id").value),
        };
    } catch (error) {
        page.validation(error);
        return;
    }
    page.start("Сохраняем черновик…");
    try {
        const created = await page.mutate(`${api}staffing-requests/`, "POST", payload);
        window.location.assign(`${app.dataset.queueUrl}${created.id}/`);
    } catch (error) {
        page.fail(error);
    }
});
page.retry.addEventListener("click", () => load());
load();

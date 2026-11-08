import {element, errorMessage, request} from "../departments/directory.js";
import {addDays, civilLabel, localDay, validZone} from "./dates.js";
import {httpLink} from "./editor.js";
import {answerLabel, answerLabels, formatInstant, modeLabel} from "./rsvp.js";

const app = document.querySelector("#calendar-invitations-app");
const api = app.dataset.invitationApiUrl;
const token = app.querySelector("[name='csrfmiddlewaretoken']").value;
const invitationId = app.dataset.invitationId;
const filter = document.querySelector("#calendar-invitation-filter");
const list = document.querySelector("#calendar-invitation-list");
const status = document.querySelector("#calendar-invitation-status");
const error = document.querySelector("#calendar-invitation-error");
const retry = document.querySelector("#calendar-invitation-retry");
const busy = new Set();
let invitations = [];

function viewerZone() {
    const browserZone = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
    try {
        const saved = localStorage.getItem("enactus-calendar-zone");
        return validZone(saved) ? saved : browserZone;
    } catch {
        return browserZone;
    }
}

const zone = viewerZone();

function fact(label, value) {
    const row = element("div", "");
    const description = element("dd", "");
    description.append(value);
    row.append(element("dt", "", label), description);
    return row;
}

function eventWhen(event) {
    if (!event.all_day) {
        return `${formatInstant(event.starts_at, zone)} — ${formatInstant(event.ends_at, zone)} · ${zone}`;
    }
    const first = localDay(event.starts_at, event.timezone);
    const last = addDays(localDay(event.ends_at, event.timezone), -1);
    const dates = first === last ? civilLabel(first) : `${civilLabel(first)} — ${civilLabel(last)}`;
    return `${dates} · Весь день · ${event.timezone}`;
}

function unanswered(invitation) {
    return invitation.can_respond
        && (invitation.needs_reconfirmation || !Object.hasOwn(answerLabels, invitation.response));
}

function matches(invitation) {
    const {event, response} = invitation;
    switch (filter?.value) {
        case "upcoming": {
            const future = event.all_day
                ? localDay(event.ends_at, event.timezone) > localDay(Date.now(), event.timezone)
                : Date.parse(event.ends_at) > Date.now();
            return event.status === "scheduled" && future;
        }
        case "pending":
            return unanswered(invitation);
        case "yes":
        case "maybe":
        case "no":
            return response === filter.value;
        case "cancelled":
            return event.status === "cancelled";
        default:
            return true;
    }
}

function renderCard(invitation) {
    const {id, event, response, rsvp_mode: mode} = invitation;
    const card = element("article", "calendar-invitation-card");
    card.dataset.invitationId = id;
    const header = element("header", "calendar-invitation-heading");
    const title = element("h2", "", event.title);
    if (!invitationId) {
        const link = element("a", "", event.title);
        link.href = `${app.dataset.listUrl}${id}/`;
        title.replaceChildren(link);
    }
    header.append(title, element("span", "muted", event.status_label));
    card.append(header);
    const facts = element("dl", "calendar-fields calendar-facts");
    facts.append(fact("Когда", eventWhen(event)), fact("Режим RSVP", modeLabel(mode)));
    if (invitation.organizer) {
        facts.append(fact("Организатор", invitation.organizer));
    }
    if (event.location) {
        facts.append(fact("Место", event.location));
    }
    const onlineUrl = httpLink(event.online_url);
    if (onlineUrl) {
        const link = element("a", "", "Открыть ссылку");
        link.href = onlineUrl;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        facts.append(fact("Онлайн-встреча", link));
    }
    if (mode !== "informational") {
        facts.append(fact("Мой ответ", mode === "required" && unanswered(invitation)
            ? element("span", "calendar-rsvp-needed", "Ожидается ответ") : answerLabel(response)));
    }
    card.append(facts);
    if (invitation.needs_reconfirmation && event.status === "scheduled") {
        const prompt = mode === "required" ? "Ответьте с учётом нового времени." : "Проверьте новое время.";
        card.append(element("p", "calendar-rsvp-needed", `Время события изменилось. ${prompt}`));
    }
    if (invitation.can_respond && mode !== "informational") {
        const actions = element("div", "calendar-rsvp-actions");
        actions.setAttribute("role", "group");
        actions.setAttribute("aria-label", `Ответ на приглашение: ${event.title}`);
        for (const [value, label] of Object.entries(answerLabels)) {
            const button = element("button", "button collection-secondary", label);
            button.type = "button";
            button.dataset.rsvp = value;
            button.disabled = busy.has(id);
            button.setAttribute("aria-pressed", String(value === response && !invitation.needs_reconfirmation));
            button.addEventListener("click", () => respond(invitation, value, actions));
            actions.append(button);
        }
        card.append(actions);
    }
    return card;
}

function render(message = "") {
    const visible = invitations.filter(matches);
    list.replaceChildren(...visible.map(renderCard));
    const empty = invitations.length ? "Нет приглашений в этой категории." : "У вас пока нет приглашений на мероприятия.";
    status.textContent = [message, visible.length ? "" : empty].filter(Boolean).join(" ");
}

async function respond(invitation, response, actions) {
    if (busy.has(invitation.id)) {
        return;
    }
    busy.add(invitation.id);
    error.textContent = "";
    for (const button of actions.children) {
        button.disabled = true;
    }
    try {
        const updated = await request(`${api}${invitation.id}/respond/`, "POST", token, {
            response,
            expected_revision: invitation.rsvp_revision,
            expected_response_at: invitation.responded_at ?? null,
        });
        invitations = invitations.map((row) => row.id === updated.id ? updated : row);
        busy.delete(invitation.id);
        render(`Ответ сохранён: ${answerLabel(response)}.`);
        (list.querySelector(`[data-invitation-id="${invitation.id}"] [data-rsvp="${response}"]`) ?? filter)?.focus();
    } catch (requestError) {
        error.textContent = errorMessage(requestError);
        retry.hidden = false;
        for (const button of actions.children) {
            button.disabled = false;
        }
        actions.querySelector(`[data-rsvp="${response}"]`)?.focus();
    } finally {
        busy.delete(invitation.id);
    }
}

async function load() {
    error.textContent = "";
    status.textContent = "Загрузка приглашений…";
    retry.hidden = true;
    list.setAttribute("aria-busy", "true");
    try {
        const result = await request(invitationId ? `${api}${invitationId}/` : `${api}?org_id=${app.dataset.organizationId}`);
        invitations = invitationId ? [result] : result.items;
        status.textContent = "";
        render();
    } catch (requestError) {
        status.textContent = "";
        error.textContent = errorMessage(requestError);
        retry.hidden = false;
    } finally {
        list.removeAttribute("aria-busy");
    }
}

filter?.addEventListener("change", () => {
    status.textContent = "";
    render();
});
retry.addEventListener("click", load);
load();

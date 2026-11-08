import {element, errorMessage, request} from "../departments/directory.js";
import {addDays, civilLabel, localDay, localTime, validZone} from "./dates.js";
import {recurrenceLabel} from "./recurrence.js";
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
            const today = localDay(Date.now(), event.timezone);
            const rule = event.recurrence;
            const ongoingSeries = invitation.is_recurring && (!rule?.until || rule.until >= today)
                && (!rule?.stopped_from || rule.stopped_from > today);
            return event.status === "scheduled" && (future || ongoingSeries);
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
    const occurrence = invitation.occurrence_key;
    const isSeries = invitation.is_recurring && !occurrence;
    const scope = occurrence ? "на эту встречу" : isSeries ? "на серию" : "";
    const busyKey = occurrence || id;
    const card = element("article", "calendar-invitation-card");
    card.dataset.invitationId = id;
    const header = element("header", "calendar-invitation-heading");
    const title = element(occurrence ? "h3" : "h2", "", event.title);
    if (!invitationId) {
        const link = element("a", "", event.title);
        link.href = `${app.dataset.listUrl}${id}/`;
        title.replaceChildren(link);
    }
    header.append(title, element("span", "muted", event.status_label));
    card.append(header);
    const facts = element("dl", "calendar-fields calendar-facts");
    facts.append(fact(isSeries ? "Начало серии" : "Когда", eventWhen(event)), fact("Режим RSVP", modeLabel(mode)));
    if (event.recurrence && !occurrence) {
        facts.append(fact("Расписание", recurrenceLabel(event.recurrence)));
    }
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
        facts.append(fact(scope ? `Мой ответ ${scope}` : "Мой ответ", mode === "required" && unanswered(invitation)
            ? element("span", "calendar-rsvp-needed", "Ожидается ответ") : answerLabel(response)));
    }
    if (occurrence && mode !== "informational") {
        facts.append(fact("Ответ на серию", answerLabel(invitation.series_response)));
    }
    card.append(facts);
    if (invitation.needs_reconfirmation && event.status === "scheduled") {
        const prompt = mode === "required" ? "Ответьте с учётом нового времени." : "Проверьте новое время.";
        card.append(element("p", "calendar-rsvp-needed", `Время события изменилось. ${prompt}`));
    }
    if (invitation.can_respond && mode !== "informational") {
        const actions = element("div", "calendar-rsvp-actions");
        actions.setAttribute("role", "group");
        actions.setAttribute("aria-label", `Ответ ${scope || "на приглашение"}: ${event.title}`);
        for (const [value, label] of Object.entries(answerLabels)) {
            const button = element("button", "button collection-secondary", label);
            button.type = "button";
            button.dataset.rsvp = value;
            button.disabled = busy.has(busyKey);
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
    const key = invitation.occurrence_key;
    const busyKey = key || invitation.id;
    const output = key ? occurrencePanel.error : error;
    if (busy.has(busyKey)) {
        return;
    }
    busy.add(busyKey);
    output.textContent = "";
    for (const button of actions.children) {
        button.disabled = true;
    }
    try {
        const path = key ? `occurrences/${key}/respond/` : "respond/";
        const updated = await request(`${api}${invitation.id}/${path}`, "POST", token, {
            response,
            expected_revision: invitation.rsvp_revision,
            expected_response_at: invitation.responded_at ?? null,
            ...(key ? {expected_occurrence_revision: invitation.occurrence_rsvp_revision} : {}),
        });
        busy.delete(busyKey);
        const message = `Ответ сохранён: ${answerLabel(response)}.`;
        if (key) {
            if (occurrencePanel.select.value === key) {
                occurrencePanel.renderUpdated(updated, message);
                occurrencePanel.card.querySelector(`[data-rsvp="${response}"]`)?.focus();
            }
        } else {
            invitations = invitations.map((row) => row.id === updated.id ? updated : row);
            render(message);
            (list.querySelector(`[data-invitation-id="${invitation.id}"] [data-rsvp="${response}"]`) ?? filter)?.focus();
            if (occurrencePanel?.select.value) {
                occurrencePanel.loadOccurrence();
            }
        }
    } catch (requestError) {
        if (!key || occurrencePanel.select.value === key) {
            output.textContent = errorMessage(requestError);
            if (key) {
                occurrencePanel.reload.hidden = false;
            } else {
                retry.hidden = false;
            }
        }
        for (const button of actions.children) {
            button.disabled = false;
        }
        actions.querySelector(`[data-rsvp="${response}"]`)?.focus();
    } finally {
        busy.delete(busyKey);
    }
}

function setupOccurrencePanel() {
    const section = document.querySelector("#calendar-invitation-occurrences");
    const get = (name) => section.querySelector(`#calendar-occurrence-${name}`);
    const select = get("select");
    const card = get("card");
    const status = get("status");
    const error = get("error");
    const reload = get("reload");
    const rangeDays = 42;
    const deepLink = new URLSearchParams(window.location.search).get("occurrence");
    const validKey = /^\d{4}-\d{2}-\d{2}$/.test(deepLink)
        && Number.isFinite(Date.parse(deepLink))
        && new Date(deepLink).toISOString().slice(0, 10) === deepLink ? deepLink : "";
    let start = validKey || localDay(Date.now(), zone);
    let currentInvitation;
    let rangeGeneration = 0;
    let detailGeneration = 0;

    function render(invitation, message = "") {
        card.replaceChildren(renderCard(invitation));
        status.textContent = message;
    }

    async function loadOccurrence() {
        const key = select.value;
        const generation = ++detailGeneration;
        card.replaceChildren();
        error.textContent = "";
        reload.hidden = true;
        status.textContent = "";
        if (!key) {
            return;
        }
        status.textContent = "Загрузка встречи…";
        try {
            const invitation = await request(`${api}${currentInvitation.id}/occurrences/${key}/`);
            if (generation === detailGeneration) {
                render(invitation);
            }
        } catch (requestError) {
            if (generation === detailGeneration) {
                status.textContent = "";
                error.textContent = errorMessage(requestError);
                reload.hidden = false;
            }
        }
    }

    async function loadRange(preferred = "") {
        const generation = ++rangeGeneration;
        detailGeneration++;
        const end = addDays(start, rangeDays);
        get("period").textContent = `${civilLabel(start, {dateStyle: "medium"})} — ${civilLabel(addDays(end, -1), {dateStyle: "medium"})}`;
        select.disabled = true;
        card.replaceChildren();
        error.textContent = "";
        reload.hidden = true;
        status.textContent = "Загрузка дат…";
        select.replaceChildren(new Option("Выберите дату", ""));
        try {
            const params = new URLSearchParams({start, end, timezone: zone, include_cancelled: "true"});
            const {items} = await request(`${app.dataset.orgUrl}events/${currentInvitation.event_id}/occurrences/?${params}`);
            if (generation !== rangeGeneration) {
                return;
            }
            for (const event of items) {
                const startDay = localDay(event.starts_at, event.all_day ? event.timezone : zone);
                const time = event.all_day ? "весь день" : localTime(event.starts_at, zone);
                const statusLabel = event.status === "scheduled" ? "" : ` · ${event.status_label}`;
                const label = `${civilLabel(startDay, {dateStyle: "medium"})} · ${time}${statusLabel}`;
                select.add(new Option(label, event.occurrence_key));
            }
            if (preferred && !items.some((event) => event.occurrence_key === preferred)) {
                // A moved meeting may now fall outside the selected period.
                select.add(new Option(`Исходная дата: ${civilLabel(preferred)}`, preferred));
            }
            select.disabled = select.options.length <= 1;
            status.textContent = select.disabled ? "В этом периоде встреч нет." : "";
            if (preferred) {
                select.value = preferred;
                loadOccurrence();
            }
        } catch (requestError) {
            if (generation === rangeGeneration) {
                if (preferred) {
                    select.add(new Option(`Исходная дата: ${civilLabel(preferred)}`, preferred));
                    select.disabled = false;
                    select.value = preferred;
                    loadOccurrence();
                } else {
                    status.textContent = "";
                    error.textContent = errorMessage(requestError);
                    reload.hidden = false;
                }
            }
        }
    }

    select.addEventListener("change", () => {
        const url = new URL(window.location.href);
        if (select.value) {
            url.searchParams.set("occurrence", select.value);
        } else {
            url.searchParams.delete("occurrence");
        }
        history.replaceState(history.state, "", url);
        loadOccurrence();
    });
    for (const [name, amount] of [["previous", -1], ["next", 1]]) {
        get(name).addEventListener("click", () => {
            start = addDays(start, amount * rangeDays);
            const url = new URL(window.location.href);
            url.searchParams.delete("occurrence");
            history.replaceState(history.state, "", url);
            loadRange();
        });
    }
    reload.addEventListener("click", () => {
        if (select.value) {
            loadOccurrence();
        } else {
            loadRange();
        }
    });

    return {
        card, error, loadOccurrence, reload, select,
        renderUpdated(invitation, message) {
            detailGeneration++;
            render(invitation, message);
        },
        open(invitation) {
            currentInvitation = invitation;
            section.hidden = !invitation.is_recurring && !validKey;
            if (!section.hidden) {
                loadRange(select.value || validKey);
            }
        },
    };
}

const occurrencePanel = invitationId ? setupOccurrencePanel() : null;

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
        if (invitationId) {
            occurrencePanel.open(result);
        }
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

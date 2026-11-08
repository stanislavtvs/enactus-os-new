import {element, errorMessage, request} from "../departments/directory.js";
import {confirmArchive} from "../ui/collection.js";
import {setupAudience} from "./audience.js";
import {answerLabel, formatInstant, modeLabel} from "./rsvp.js";

function action(label, listener) {
    const button = element("button", "button collection-secondary", label);
    button.type = "button";
    button.addEventListener("click", listener);
    return button;
}

export function setupParticipants({app, api, token, zone, isDetailOpen}) {
    const get = (suffix) => app.querySelector(`#calendar-participants${suffix ? `-${suffix}` : ""}`);
    const section = get("");
    const invite = app.querySelector("#calendar-invite");
    const summaryFields = [
        ["invited", "Приглашено"], ["yes", "Придут"], ["no", "Не придут"],
        ["maybe", "Пока не знают"], ["pending", "Без ответа"],
        ["needs_reconfirmation", "Нужно подтвердить заново"], ["revoked", "Отозвано"],
    ];
    let current, stats;
    let generation = 0;
    let changing = false;

    function endpoint(record = current) {
        return `${api}events/${record.id}/invitations/`;
    }

    const audience = setupAudience({app, api, token, onUpdate: (event, message) => load(event, message)});

    function reset() {
        generation++;
        current = null;
        stats = null;
        section.hidden = true;
        invite.hidden = true;
        get("status").textContent = "";
        get("error").textContent = "";
        get("mode").textContent = "";
        get("totals").replaceChildren();
        get("controls").replaceChildren();
        get("disclosure").open = false;
        get("disclosure").hidden = true;
        get("list").replaceChildren();
    }

    function allowed(event) {
        return isDetailOpen() && current?.id === event.id;
    }

    function renderStats(data) {
        get("mode").textContent = modeLabel(data.rsvp_mode);
        get("totals").replaceChildren(...summaryFields.map(([field, label]) => {
            const stat = element("div", "");
            stat.append(element("dt", "", label), element("dd", "", String(data[field])));
            return stat;
        }));
        invite.hidden = !data.can_send;
        const controls = [];
        if (data.can_manage && current.status === "scheduled") {
            const editMode = action("Изменить режим RSVP", (event) => audience.open(event.currentTarget, current, {
                ...stats, can_send: false,
            }));
            editMode.setAttribute("aria-haspopup", "dialog");
            controls.push(editMode);
        }
        if (data.can_send && data.can_manage) {
            controls.push(action("Обновить аудиторию", (event) => refresh(event.currentTarget)));
        }
        get("controls").replaceChildren(...controls);
    }

    function historyDisclosure(event, row) {
        const details = element("details", "calendar-participant-history");
        const summary = element("summary", "", "История ответов");
        summary.setAttribute("aria-label", `История ответов: ${row.username}`);
        const output = element("div", "");
        details.append(summary, output);
        let loading = false;
        let loaded = false;
        details.addEventListener("toggle", async () => {
            if (!details.open || loading || loaded) {
                return;
            }
            loading = true;
            output.textContent = "Загрузка истории…";
            try {
                const {items} = await request(`${endpoint(event)}${row.id}/history/`);
                const history = element("ul", "");
                for (const record of items) {
                    const actor = record.actor ? ` · ${record.actor}` : "";
                    history.append(element("li", "", `${answerLabel(record.response)} · ${formatInstant(record.created_at, zone())}${actor}`));
                }
                output.replaceChildren(items.length ? history : element("p", "muted", "Изменений ответа пока нет."));
                loaded = true;
            } catch (requestError) {
                output.textContent = errorMessage(requestError);
            } finally {
                loading = false;
            }
        });
        return details;
    }

    function renderParticipants(event, items) {
        const rows = items.map((record) => {
            const row = element("li", "calendar-participant");
            const body = element("div", "");
            const note = [
                record.revoked_at ? "Отозвано" : "",
                record.needs_reconfirmation ? "Нужно подтвердить заново" : "",
            ].filter(Boolean).join(" · ");
            body.append(element("strong", "", record.username));
            body.append(element("p", "muted", [answerLabel(record.response), note].filter(Boolean).join(" · ")));
            row.append(body);
            if (record.can_revoke) {
                const revokeButton = action("Отозвать", (click) => revoke(record, click.currentTarget));
                revokeButton.setAttribute("aria-label", `Отозвать приглашение: ${record.username}`);
                row.append(revokeButton);
            }
            row.append(historyDisclosure(event, record));
            return row;
        });
        get("disclosure").hidden = !rows.length;
        get("list").replaceChildren(...rows);
    }

    async function load(event, message = "") {
        const version = ++generation;
        current = event;
        section.hidden = !(event.can_invite || event.can_manage_invitations);
        if (section.hidden) {
            return;
        }
        get("error").textContent = "";
        if (!event.can_manage_invitations) {
            stats = {rsvp_mode: event.rsvp_mode, can_send: event.can_invite, can_manage: false};
            get("mode").textContent = modeLabel(stats.rsvp_mode);
            get("status").textContent = message;
            invite.hidden = !stats.can_send;
            return;
        }
        get("status").textContent = "Загрузка участников…";
        try {
            const data = await request(`${endpoint(event)}stats/`);
            if (!allowed(event) || version !== generation) {
                return;
            }
            stats = data;
            renderStats(data);
            const {items} = await request(endpoint(event));
            if (allowed(event) && version === generation) {
                renderParticipants(event, items);
            }
            if (allowed(event) && version === generation) {
                get("status").textContent = message;
            }
        } catch (requestError) {
            if (allowed(event) && version === generation) {
                get("error").textContent = errorMessage(requestError);
                get("status").textContent = "";
            }
        }
    }

    async function mutate(path, button, confirmation, feedback) {
        if (changing || !current) {
            return;
        }
        const record = current;
        changing = true;
        try {
            if (!await confirmArchive(record.title, confirmation) || !allowed(record)) {
                return;
            }
            button.disabled = true;
            get("error").textContent = "";
            const result = await request(`${endpoint(record)}${path}`, "POST", token);
            if (allowed(record)) {
                await load(record, feedback(result));
                if (!button.isConnected) {
                    get("heading").focus();
                }
            }
        } catch (requestError) {
            if (allowed(record)) {
                get("error").textContent = errorMessage(requestError);
            }
        } finally {
            button.disabled = false;
            if (allowed(record) && button.isConnected) {
                button.focus();
            }
            changing = false;
        }
    }

    function refresh(button) {
        mutate("refresh/", button, {
            heading: "Обновить аудиторию?",
            description: "Система повторно проверит сохранённые группы и пригласит новых подходящих участников.",
            confirmLabel: "Обновить аудиторию",
        }, (result) => `Новых приглашений: ${result.created}.`);
    }

    function revoke(record, button) {
        mutate(`${record.id}/revoke/`, button, {
            heading: "Отозвать приглашение?",
            description: `Приглашение участника «${record.username}» будет отозвано. История ответов сохранится.`,
            confirmLabel: "Отозвать приглашение",
        }, () => "Приглашение отозвано.");
    }

    invite.addEventListener("click", () => {
        if (current && stats?.can_send) {
            audience.open(invite, current, {...stats});
        }
    });
    return {load, reset};
}

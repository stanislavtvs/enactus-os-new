import {element, errorMessage, request} from "../departments/directory.js";
import {setupDialog} from "../ui/collection.js";
import {modeLabel} from "./rsvp.js";

function checkbox(text, checked, onChange) {
    const label = element("label", "calendar-check");
    const input = element("input", "");
    input.type = "checkbox";
    input.checked = checked;
    input.addEventListener("change", () => onChange(input.checked));
    label.append(input, text);
    return label;
}

export function setupAudience({app, api, token, onUpdate}) {
    const get = (suffix) => app.querySelector(`#calendar-audience-${suffix}`);
    const dialog = app.querySelector("#calendar-invite-dialog");
    const form = app.querySelector("#calendar-invite-form");
    const modal = setupDialog(dialog);
    const search = get("search");
    const mode = get("mode");
    const modeButton = get("save-mode");
    const preview = get("preview");
    const send = get("send");
    const count = get("count");
    const error = get("error");
    const organization = get("organization");
    const reinvite = get("reinvite");
    const members = new Map();
    const departments = new Map();
    const projects = new Set();
    let activeEvent, stats, endpoint, queryTimeout;
    let generation = 0;
    let selectionVersion = 0;
    let confirmedVersion = -1;
    let page = 1;
    let busy = false;
    let scopeReady = false;
    let loadingOptions = false;
    let canReinvite = false;
    let audienceReady = false;

    function selection() {
        return {
            member_ids: [...members.keys()],
            organization: organization.checked,
            departments: [...departments].map(([id, include_descendants]) => ({id, include_descendants})),
            project_ids: [...projects],
            reinvite_revoked: canReinvite && reinvite.checked,
        };
    }

    function updateButtons() {
        const hasRecipients = members.size || departments.size || projects.size || organization.checked;
        const modeChanged = mode.value !== stats.rsvp_mode;
        preview.disabled = busy || !scopeReady || !hasRecipients || !stats.can_send;
        send.disabled = preview.disabled || !audienceReady || confirmedVersion !== selectionVersion || modeChanged;
        modeButton.disabled = busy || !modeChanged;
    }

    function changed() {
        selectionVersion++;
        count.textContent = "";
        updateButtons();
    }

    function renderSelected() {
        const rows = [...members].map(([id, username]) => {
            const row = element("li", "");
            const button = element("button", "button collection-secondary", `${username} ×`);
            button.type = "button";
            button.setAttribute("aria-label", `Убрать из выбора: ${username}`);
            button.addEventListener("click", () => {
                members.delete(id);
                for (const input of get("members").querySelectorAll("input")) {
                    if (Number(input.value) === id) {
                        input.checked = false;
                    }
                }
                renderSelected();
                search.focus();
                changed();
            });
            row.append(button);
            return row;
        });
        get("selected").replaceChildren(...rows);
    }

    function renderOptions(options) {
        get("members").replaceChildren(...options.members.map(({id, username}) => {
            const option = checkbox(username, members.has(id), (checked) => {
                if (checked) {
                    members.set(id, username);
                } else {
                    members.delete(id);
                }
                renderSelected();
                changed();
            });
            option.querySelector("input").value = id;
            return option;
        }));
        get("members-status").textContent = options.member_total
            ? `Найдено участников: ${options.member_total}. Страница ${options.member_page}.`
            : "Участники не найдены.";
        get("prev").hidden = options.member_page <= 1;
        get("next").hidden = options.member_total <= options.member_page * options.member_page_size;
        if (scopeReady) {
            return;
        }
        get("organization-field").hidden = !options.can_invite_organization;
        canReinvite = options.can_reinvite;
        get("reinvite-field").hidden = !canReinvite;
        get("departments").replaceChildren(...options.departments.map(({id, name, can_include_descendants: descendants}) => {
            const row = element("div", "calendar-audience-row");
            const label = checkbox(name, false, (checked) => {
                if (checked) {
                    departments.set(id, include.value === "branch");
                } else {
                    departments.delete(id);
                }
                include.disabled = !checked || !descendants;
                changed();
            });
            const include = element("select", "");
            include.setAttribute("aria-label", `Охват: ${name}`);
            include.add(new Option("Только прямые участники", "direct"));
            if (descendants) {
                include.add(new Option("Включая дочерние подразделения", "branch"));
            }
            include.disabled = true;
            include.addEventListener("change", () => {
                departments.set(id, include.value === "branch");
                changed();
            });
            row.append(label, include);
            return row;
        }));
        get("projects").replaceChildren(...options.projects.map(({id, name}) => checkbox(name, false, (checked) => {
            if (checked) {
                projects.add(id);
            } else {
                projects.delete(id);
            }
            changed();
        })));
        if (!options.departments.length) {
            get("departments").textContent = "Нет доступных подразделений.";
        }
        if (!options.projects.length) {
            get("projects").textContent = "Нет доступных проектных команд.";
        }
        scopeReady = true;
        updateButtons();
    }

    async function loadOptions(focusPagination = false) {
        loadingOptions = true;
        const version = ++generation;
        get("members-status").textContent = "Поиск участников…";
        get("prev").hidden = true;
        get("next").hidden = true;
        try {
            const query = new URLSearchParams({q: search.value.trim(), page});
            const options = await request(`${endpoint}options/?${query}`);
            if (dialog.open && generation === version) {
                renderOptions(options);
                if (focusPagination) {
                    const target = [get("next"), get("prev"), search].find((control) => !control.hidden);
                    target.focus();
                }
            }
        } catch (requestError) {
            if (dialog.open && generation === version) {
                get("members-status").textContent = errorMessage(requestError);
                if (focusPagination) {
                    search.focus();
                }
            }
        } finally {
            if (generation === version) {
                loadingOptions = false;
            }
        }
    }

    function open(trigger, event, summary) {
        activeEvent = event;
        stats = summary;
        endpoint = `${api}events/${event.id}/invitations/`;
        generation++;
        clearTimeout(queryTimeout);
        form.reset();
        members.clear();
        departments.clear();
        projects.clear();
        page = 1;
        scopeReady = false;
        audienceReady = false;
        renderSelected();
        get("members").replaceChildren();
        get("departments").replaceChildren();
        get("projects").replaceChildren();
        get("organization-field").hidden = true;
        get("reinvite-field").hidden = true;
        search.closest(".calendar-audience-group").hidden = !stats.can_send;
        get("departments").closest(".calendar-audience-group").hidden = !stats.can_send;
        get("projects").closest(".calendar-audience-group").hidden = !stats.can_send;
        preview.hidden = !stats.can_send;
        send.hidden = !stats.can_send;
        mode.value = stats.rsvp_mode;
        mode.disabled = !stats.can_manage;
        modeButton.hidden = !stats.can_manage;
        error.textContent = "";
        changed();
        modal.open(trigger, stats.can_send ? search : mode);
        if (stats.can_send) {
            loadOptions();
        }
    }

    search.addEventListener("input", () => {
        generation++;
        page = 1;
        clearTimeout(queryTimeout);
        queryTimeout = setTimeout(loadOptions, 200);
    });
    for (const [name, amount] of [["prev", -1], ["next", 1]]) {
        get(name).addEventListener("click", () => {
            if (!loadingOptions) {
                page += amount;
                loadOptions(true);
            }
        });
    }
    organization.addEventListener("change", changed);
    reinvite.addEventListener("change", changed);
    mode.addEventListener("change", updateButtons);
    dialog.addEventListener("close", () => {
        generation++;
        clearTimeout(queryTimeout);
    });

    preview.addEventListener("click", async () => {
        if (preview.disabled) {
            return;
        }
        const version = selectionVersion;
        const eventId = activeEvent.id;
        error.textContent = "";
        preview.disabled = true;
        send.disabled = true;
        count.textContent = "Подсчитываем уникальных получателей…";
        try {
            const summary = await request(`${endpoint}preview/`, "POST", token, selection());
            if (dialog.open && eventId === activeEvent.id && version === selectionVersion) {
                confirmedVersion = version;
                audienceReady = summary.new > 0 || organization.checked || departments.size > 0 || projects.size > 0;
                count.textContent = `Уникальных получателей: ${summary.total} · Новых приглашений: ${summary.new} · Уже приглашены: ${summary.already_invited} · Отозваны: ${summary.revoked}`;
            }
        } catch (requestError) {
            if (dialog.open && eventId === activeEvent.id && version === selectionVersion) {
                count.textContent = "";
                error.textContent = errorMessage(requestError);
            }
        } finally {
            if (dialog.open) {
                updateButtons();
            }
        }
    });

    modeButton.addEventListener("click", async () => {
        if (modeButton.disabled) {
            return;
        }
        const selected = mode.value;
        error.textContent = "";
        busy = true;
        mode.disabled = true;
        modal.setBusy(true);
        updateButtons();
        try {
            const record = await request(`${api}events/${activeEvent.id}/`, "PATCH", token, {rsvp_mode: selected});
            stats.rsvp_mode = record.rsvp_mode;
            mode.value = stats.rsvp_mode;
            if (stats.can_send) {
                count.textContent = `Режим сохранён: ${modeLabel(stats.rsvp_mode)}. Рассчитайте аудиторию перед отправкой.`;
                selectionVersion++;
                onUpdate(record);
            } else {
                modal.close();
                onUpdate(record, `Режим сохранён: ${modeLabel(stats.rsvp_mode)}.`);
            }
        } catch (requestError) {
            error.textContent = errorMessage(requestError);
        } finally {
            busy = false;
            mode.disabled = !stats.can_manage;
            modal.setBusy(false);
            updateButtons();
        }
    });

    form.addEventListener("submit", async (event) => {
        event.preventDefault();
        if (send.disabled) {
            return;
        }
        error.textContent = "";
        busy = true;
        modal.setBusy(true);
        updateButtons();
        try {
            const summary = await request(endpoint, "POST", token, selection());
            modal.close();
            onUpdate(activeEvent, `Отправлено приглашений: ${summary.created}.`);
        } catch (requestError) {
            error.textContent = errorMessage(requestError);
        } finally {
            busy = false;
            modal.setBusy(false);
            updateButtons();
        }
    });
    return {open};
}

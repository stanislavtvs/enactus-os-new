import {element, errorMessage, request} from "../departments/directory.js";
import {confirmArchive, setupDialog} from "../ui/collection.js";

export const requestStatus = {
    pending: "Ожидает рассмотрения", approved: "Одобрена", rejected: "Отклонена", withdrawn: "Отозвана",
};
const policyModes = {
    permission_only: "По разрешению", open: "Свободное создание", request_approval: "По заявке",
};
const dateFormat = new Intl.DateTimeFormat("ru", {dateStyle: "medium", timeStyle: "short"});

export function formatDate(date) {
    return dateFormat.format(new Date(date));
}

export function actionButton(label, action, primary = false) {
    const button = element("button", primary ? "button button-primary" : "button collection-secondary", label);
    button.type = "button";
    button.dataset.platformAction = action;
    return button;
}

export function setupPlatformList({get, prefix, url, renderRow, empty, query = () => new URLSearchParams()}) {
    const control = (id) => get(`${prefix}-${id}`);
    const previous = control("previous");
    const next = control("next");
    const nav = control("pages");
    const list = control("list");
    const status = control("status");
    const error = control("error");
    const retry = control("retry") ?? control("refresh");
    let page = 1;
    let generation = 0;
    let loading = false;

    async function load({reset = false, message = ""} = {}) {
        if (reset) {
            page = 1;
        }
        const version = ++generation;
        const params = query();
        params.set("page", page);
        loading = true;
        error.textContent = "";
        status.textContent = "Загрузка…";
        list.setAttribute("aria-busy", "true");
        if (retry.id.endsWith("-retry")) {
            retry.hidden = true;
        }
        try {
            const result = await request(`${url}?${params}`);
            if (version !== generation) {
                return;
            }
            const pages = Math.max(1, Math.ceil(result.total / result.page_size));
            if (page > pages) {
                page = pages;
                return load({message});
            }
            list.replaceChildren(...result.items.map(renderRow));
            status.textContent = message || (result.items.length ? "" : empty);
            nav.hidden = pages <= 1;
            control("page").textContent = `Страница ${page} из ${pages}`;
            previous.disabled = page <= 1;
            next.disabled = page >= pages;
        } catch (requestError) {
            if (version === generation) {
                status.textContent = "";
                error.textContent = errorMessage(requestError);
                list.replaceChildren();
                nav.hidden = true;
                retry.hidden = false;
            }
        } finally {
            if (version === generation) {
                loading = false;
                list.removeAttribute("aria-busy");
            }
        }
    }

    retry.addEventListener("click", () => load());
    for (const [button, offset] of [[previous, -1], [next, 1]]) {
        button.addEventListener("click", () => {
            if (!loading) {
                page += offset;
                load();
            }
        });
    }
    return {load};
}

function rowWithAction(title, summary, action, handler) {
    const row = element("li", "platform-row");
    const copy = element("div", "platform-row-copy");
    copy.append(element("h3", "", title), element("p", "muted", summary));
    row.append(copy);
    if (action) {
        action.addEventListener("click", () => handler(action));
        row.append(action);
    }
    return row;
}

function setupAdmin(app) {
    const get = (id) => app.querySelector(`#platform-${id}`);
    const api = app.dataset.platformUrl;
    const token = app.querySelector("[name='csrfmiddlewaretoken']").value;
    const mode = get("mode");
    const modeSave = get("mode-save");
    const policyForm = get("policy-form");
    const policyCurrent = get("policy-current");
    const policyError = get("policy-error");
    let currentMode;
    let changingMode = false;

    function renderPolicy(data) {
        currentMode = data.mode;
        mode.value = currentMode;
        modeSave.disabled = true;
        policyCurrent.textContent = `Действующий режим: ${policyModes[currentMode]}.`;
        policyForm.hidden = !data.can_administer;
    }

    async function loadPolicy() {
        get("policy-retry").hidden = true;
        policyError.textContent = "";
        try {
            renderPolicy(await request(`${api}creation-policy/`));
        } catch (requestError) {
            policyCurrent.textContent = "";
            policyError.textContent = errorMessage(requestError);
            get("policy-retry").hidden = false;
        }
    }

    mode.addEventListener("change", () => {
        modeSave.disabled = changingMode || mode.value === currentMode;
    });
    get("policy-retry").addEventListener("click", loadPolicy);
    policyForm.addEventListener("submit", async (event) => {
        event.preventDefault();
        if (changingMode || mode.value === currentMode) {
            return;
        }
        changingMode = true;
        policyError.textContent = "";
        const nextMode = mode.value;
        try {
            if (!await confirmArchive(policyModes[nextMode], {
                heading: "Изменить правила создания?",
                description: `Новый режим «${policyModes[nextMode]}» будет действовать для всей платформы.`,
                confirmLabel: "Изменить режим",
            })) {
                modeSave.focus();
                return;
            }
            modeSave.disabled = true;
            mode.disabled = true;
            renderPolicy(await request(`${api}creation-policy/`, "PATCH", token, {mode: nextMode}));
            mode.disabled = false;
            mode.focus();
        } catch (requestError) {
            policyError.textContent = errorMessage(requestError);
            get("policy-retry").hidden = false;
        } finally {
            changingMode = false;
            mode.disabled = false;
            modeSave.disabled = mode.value === currentMode;
        }
    });
    loadPolicy();

    const grantEndpoint = `${api}creation-grants/`;
    let grantBusy = false;
    let grantFilter = "";
    const grants = setupPlatformList({
        get, prefix: "grants", url: grantEndpoint, empty: "В этой выборке нет разрешений.",
        query: () => new URLSearchParams(grantFilter ? {q: grantFilter} : undefined),
        renderRow: (grant) => rowWithAction(
            grant.user.username,
            [
                `Выдано ${formatDate(grant.granted_at)} · ${grant.granted_by?.username ?? "Система"}`,
                grant.revoked_at ? `Отозвано ${formatDate(grant.revoked_at)}` : "Действует",
            ].join(" · "),
            grant.revoked_at ? null : actionButton("Отозвать", "revoke-grant"),
            (trigger) => revokeGrant(grant, trigger),
        ),
    });
    get("grants-filter").addEventListener("submit", (event) => {
        event.preventDefault();
        grantFilter = get("grants-query").value.trim();
        grants.load({reset: true});
    });

    async function revokeGrant(grant, trigger) {
        if (grantBusy) {
            return;
        }
        grantBusy = true;
        try {
            if (!await confirmArchive(grant.user.username, {
                heading: "Отозвать разрешение?",
                description: `У пользователя «${grant.user.username}» будет отозвано индивидуальное разрешение на создание организаций.`,
                confirmLabel: "Отозвать разрешение",
            })) {
                return;
            }
            trigger.disabled = true;
            await request(`${grantEndpoint}${grant.user.id}/`, "DELETE", token);
            await grants.load({message: "Разрешение отозвано."});
            get("grants-heading").focus();
        } catch (requestError) {
            get("grants-error").textContent = errorMessage(requestError);
            get("grants-retry").hidden = false;
        } finally {
            grantBusy = false;
            if (trigger.isConnected) {
                trigger.disabled = false;
                trigger.focus();
            }
        }
    }

    const grantDialog = get("grant-dialog");
    const grantModal = setupDialog(grantDialog);
    const grantQuery = get("grant-query");
    const grantCandidate = get("grant-candidate");
    const grantStatus = get("grant-status");
    const grantError = get("grant-error");
    const grantSubmit = get("grant-submit");
    let searchVersion = 0;

    function clearCandidates() {
        grantCandidate.replaceChildren();
        grantCandidate.disabled = true;
        grantSubmit.disabled = true;
        get("grant-candidates-field").hidden = true;
    }

    get("grant-open").addEventListener("click", (event) => {
        searchVersion++;
        get("grant-search").reset();
        clearCandidates();
        grantStatus.textContent = "";
        grantError.textContent = "";
        grantModal.open(event.currentTarget, grantQuery);
    });
    grantQuery.addEventListener("input", () => {
        searchVersion++;
        clearCandidates();
        grantStatus.textContent = "";
        grantError.textContent = "";
    });
    get("grant-search").addEventListener("submit", async (event) => {
        event.preventDefault();
        const q = grantQuery.value.trim();
        if (!q || grantBusy) {
            return;
        }
        const version = ++searchVersion;
        clearCandidates();
        grantStatus.textContent = "Поиск пользователей…";
        grantError.textContent = "";
        try {
            const {items} = await request(`${api}creation-grant-candidates/?${new URLSearchParams({q})}`);
            if (grantDialog.open && version === searchVersion) {
                grantStatus.textContent = items.length ? "" : "Подходящих пользователей нет.";
                if (items.length) {
                    grantCandidate.replaceChildren(new Option("Выберите пользователя", ""),
                        ...items.map(({username}) => new Option(username, username)));
                    grantCandidate.disabled = false;
                    get("grant-candidates-field").hidden = false;
                    grantCandidate.focus();
                }
            }
        } catch (requestError) {
            if (grantDialog.open && version === searchVersion) {
                grantStatus.textContent = "";
                grantError.textContent = errorMessage(requestError);
            }
        }
    });
    grantCandidate.addEventListener("change", () => { grantSubmit.disabled = !grantCandidate.value; });
    get("grant-form").addEventListener("submit", async (event) => {
        event.preventDefault();
        if (grantBusy || !grantCandidate.value) {
            return;
        }
        grantBusy = true;
        grantModal.setBusy(true);
        grantQuery.disabled = true;
        grantCandidate.disabled = true;
        grantError.textContent = "";
        try {
            await request(grantEndpoint, "POST", token, {username: grantCandidate.value});
            grantModal.close();
            grants.load({reset: true, message: "Разрешение выдано."});
        } catch (requestError) {
            grantError.textContent = errorMessage(requestError);
        } finally {
            grantBusy = false;
            grantModal.setBusy(false);
            grantQuery.disabled = false;
            grantCandidate.disabled = false;
            grantSubmit.disabled = !grantCandidate.value;
        }
    });
    grants.load();

    const requestEndpoint = `${api}admin/creation-requests/`;
    const filters = get("request-filters");
    let activeFilters = new URLSearchParams({status: "pending"});
    const requests = setupPlatformList({
        get, prefix: "requests", url: requestEndpoint, empty: "В этой выборке нет заявок.",
        query: () => new URLSearchParams(activeFilters),
        renderRow: (item) => rowWithAction(
            item.name, `${requestStatus[item.status]} · ${item.applicant.username} · ${formatDate(item.created_at)}`,
            actionButton("Подробности", "review-request"), (trigger) => openRequest(item, trigger),
        ),
    });
    filters.addEventListener("submit", (event) => {
        event.preventDefault();
        activeFilters = new URLSearchParams();
        for (const [key, value] of new FormData(filters)) {
            if (value.trim()) {
                activeFilters.set(key, value.trim());
            }
        }
        requests.load({reset: true});
    });

    const requestDialog = get("request-dialog");
    const requestModal = setupDialog(requestDialog);
    const reviewForm = get("review-form");
    const reviewStatus = get("review-status");
    const reviewError = get("review-error");
    const reviewComment = get("review-comment");
    let detailVersion = 0;
    let activeRequest;
    let reviewTrigger;
    requestDialog.addEventListener("close", () => {
        if (!reviewTrigger?.isConnected) {
            get("requests-heading").focus();
        }
    });
    let decisionPending = false;

    function fact(label, value) {
        const field = element("div", "");
        field.append(element("dt", "", label), element("dd", "", value));
        return field;
    }

    function renderRequest(record) {
        activeRequest = record;
        get("review-title").textContent = record.name;
        const fields = [
            ["Заявитель", record.applicant.username],
            ["Статус", requestStatus[record.status]],
            ["Создана", formatDate(record.created_at)],
            ["Рассмотрел", record.reviewer?.username],
            ["Дата решения", record.decided_at ? formatDate(record.decided_at) : ""],
            ["Комментарий к решению", record.decision_comment],
        ];
        get("review-facts").replaceChildren(...fields.filter(([, value]) => value).map(([label, value]) => fact(label, value)));
        get("review-description").textContent = record.description;
        reviewForm.hidden = record.status !== "pending";
    }

    async function openRequest(record, trigger) {
        const version = ++detailVersion;
        reviewTrigger = trigger;
        activeRequest = null;
        get("review-title").textContent = record.name;
        get("review-facts").replaceChildren();
        get("review-description").textContent = "";
        reviewForm.reset();
        reviewForm.hidden = true;
        reviewStatus.textContent = "Загрузка заявки…";
        reviewError.textContent = "";
        requestModal.open(trigger, requestDialog.querySelector("[data-dialog-close]"));
        try {
            const detail = await request(`${requestEndpoint}${record.id}/`);
            if (requestDialog.open && detailVersion === version) {
                reviewStatus.textContent = "";
                renderRequest(detail);
            }
        } catch (requestError) {
            if (requestDialog.open && detailVersion === version) {
                reviewStatus.textContent = "";
                reviewError.textContent = errorMessage(requestError);
            }
        }
    }

    reviewForm.addEventListener("submit", async (event) => {
        event.preventDefault();
        const decision = event.submitter?.value;
        const comment = reviewComment.value.trim();
        if (decisionPending || activeRequest?.status !== "pending" || !["approve", "reject"].includes(decision)) {
            return;
        }
        if ((decision === "reject" && !comment) || Array.from(comment).length > 1000) {
            reviewError.textContent = !comment ? "Укажите причину отклонения." : "Введите комментарий длиной до 1000 символов.";
            reviewComment.focus();
            return;
        }
        decisionPending = true;
        reviewError.textContent = "";
        try {
            if (decision === "approve" && !await confirmArchive(activeRequest.name, {
                heading: "Одобрить заявку?",
                description: `Будет создана организация «${activeRequest.name}». Заявитель станет её владельцем.`,
                confirmLabel: "Одобрить заявку",
            })) {
                event.submitter.focus();
                return;
            }
            requestModal.setBusy(true);
            reviewComment.disabled = true;
            const updated = await request(`${requestEndpoint}${activeRequest.id}/decision/`, "POST", token, {decision, comment});
            renderRequest(updated);
            reviewStatus.textContent = decision === "approve" ? "Заявка одобрена. Организация создана." : "Заявка отклонена.";
            requestDialog.querySelector("[data-dialog-close]").disabled = false;
            requestDialog.querySelector("[data-dialog-close]").focus();
            requests.load();
        } catch (requestError) {
            reviewError.textContent = errorMessage(requestError);
        } finally {
            decisionPending = false;
            reviewComment.disabled = false;
            requestModal.setBusy(false);
        }
    });
    requests.load();
}

const platform = document.querySelector("#platform-app");
if (platform?.dataset.platformUrl) {
    setupAdmin(platform);
}

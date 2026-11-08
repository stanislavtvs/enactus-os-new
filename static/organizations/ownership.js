import {element, errorMessage, request} from "../departments/directory.js";
import {confirmArchive, setupDialog} from "../ui/collection.js";

const app = document.querySelector("#ownership-app");
const get = (id) => app.querySelector(`#ownership-${id}`);
const api = app.dataset.apiUrl;
const token = app.querySelector("[name='csrfmiddlewaretoken']").value;
const status = get("status");
const error = get("error");
const retry = get("retry");
const formatDate = new Intl.DateTimeFormat("ru", {dateStyle: "medium", timeStyle: "short"});
const labels = {
    pending: "Ожидает ответа",
    accepted: "Владение передано",
    declined: "Отклонено",
    revoked: "Отозвано",
    expired: "Срок истёк",
};

function expiry(offer) {
    const text = element("p", "muted", "Срок действия: ");
    const date = element("time", "", formatDate.format(new Date(offer.expires_at)));
    date.dateTime = offer.expires_at;
    text.append(date);
    return text;
}

function button(text, id, primary = false) {
    const node = element("button", primary ? "button button-primary" : "button collection-secondary", text);
    node.type = "button";
    node.dataset.ownershipAction = id;
    return node;
}

function setupLoad(render, onError) {
    let pending = false;
    async function load() {
        if (pending) {
            return;
        }
        pending = true;
        retry.hidden = true;
        error.textContent = "";
        status.textContent = "Загрузка предложений…";
        try {
            const result = await request(api);
            status.textContent = "";
            render(result);
        } catch (requestError) {
            status.textContent = "";
            onError();
            error.textContent = errorMessage(requestError);
            retry.hidden = false;
        } finally {
            pending = false;
        }
    }
    retry.addEventListener("click", load);
    load();
}

function setupManage() {
    const create = get("create");
    const dialog = get("dialog");
    const modal = setupDialog(dialog);
    const form = get("form");
    const select = get("recipient");
    const confirm = get("confirm");
    const formError = get("form-error");
    const pending = get("pending");
    let candidates = [];
    let busy = false;

    function render(offer) {
        create.hidden = Boolean(offer) || !candidates.length;
        pending.replaceChildren();
        if (!offer) {
            status.textContent = candidates.length ? "Действующего предложения нет." : "Для передачи нужен другой активный участник.";
            return;
        }
        status.textContent = "";
        const card = element("div", "ownership-row");
        const recipient = element("p", "", "Предложение для ");
        recipient.append(element("strong", "", offer.recipient.username));
        const revoke = button("Отозвать предложение", "revoke");
        revoke.addEventListener("click", () => revokeOffer(offer, revoke));
        const actions = element("div", "ownership-actions");
        actions.append(revoke);
        card.append(recipient, element("p", "muted", labels[offer.status]), expiry(offer), actions);
        pending.append(card);
    }

    async function revokeOffer(offer, trigger) {
        if (busy) {
            return;
        }
        busy = true;
        try {
            if (!await confirmArchive(offer.recipient.username, {
                heading: "Отозвать предложение?",
                description: `После отзыва ${offer.recipient.username} не сможет принять это предложение.`,
                confirmLabel: "Отозвать предложение",
            })) {
                trigger.focus();
                return;
            }
            trigger.disabled = true;
            error.textContent = "";
            await request(`${api}${offer.id}/revoke/`, "POST", token);
            render(null);
            status.textContent = "Предложение отозвано.";
            (create.hidden ? get("heading") : create).focus();
        } catch (requestError) {
            error.textContent = errorMessage(requestError);
            retry.hidden = false;
        } finally {
            busy = false;
            if (trigger.isConnected) {
                trigger.disabled = false;
                trigger.focus();
            }
        }
    }

    create.addEventListener("click", () => {
        select.replaceChildren(new Option("Выберите участника", ""), ...candidates.map(({id, username}) => new Option(username, id)));
        form.reset();
        formError.textContent = "";
        modal.open(create, select);
    });

    form.addEventListener("submit", async (event) => {
        event.preventDefault();
        const recipient = Number(select.value);
        if (busy || !Number.isSafeInteger(recipient) || recipient < 1 || !confirm.checked) {
            return;
        }
        busy = true;
        modal.setBusy(true);
        select.disabled = true;
        confirm.disabled = true;
        formError.textContent = "";
        try {
            const offer = await request(api, "POST", token, {recipient_id: recipient, confirm: true});
            modal.close();
            render(offer);
            status.textContent = "Предложение отправлено. Вы сохраняете владение до принятия.";
            get("heading").focus();
        } catch (requestError) {
            formError.textContent = errorMessage(requestError);
            retry.hidden = false;
        } finally {
            busy = false;
            modal.setBusy(false);
            select.disabled = false;
            confirm.disabled = false;
        }
    });

    setupLoad(({owner, pending: offer, candidates: available}) => {
        get("current-owner").textContent = owner.username;
        candidates = available;
        render(offer);
    }, () => {
        create.hidden = true;
        pending.replaceChildren();
    });
}

function setupInbox() {
    const list = get("inbox-list");
    let offers = [];
    let busy = false;

    function render() {
        const rows = offers.map((offer) => {
            const row = element("li", "ownership-row");
            row.dataset.ownershipId = offer.id;
            const title = element("h2", "", offer.organization_name);
            const source = element("p", "", `Предложение от ${offer.from_owner.username}`);
            row.append(title, source, element("p", "muted", labels[offer.status] ?? "Закрыто"), expiry(offer));
            if (offer.status === "pending") {
                row.append(element("p", "", "При принятии вы получите полномочия владельца этой организации."));
                const controls = element("div", "ownership-actions");
                for (const [action, name, accept] of [["accept", "Принять", true], ["decline", "Отклонить", false]]) {
                    const control = button(name, action, accept);
                    control.setAttribute("aria-label", `${name} предложение о владении организацией «${offer.organization_name}»`);
                    control.addEventListener("click", () => respond(offer, accept, control, controls));
                    controls.append(control);
                }
                row.append(controls);
            }
            return row;
        });
        list.replaceChildren(...rows);
        if (!offers.length) {
            status.textContent = "У вас нет предложений о владении.";
        }
    }

    async function respond(offer, accept, trigger, controls) {
        if (busy) {
            return;
        }
        busy = true;
        try {
            if (!await confirmArchive(offer.organization_name, accept ? {
                heading: "Принять владение?",
                description: `Вы станете владельцем организации «${offer.organization_name}». Полномочия владельца перейдут вам.`,
                confirmLabel: "Принять владение",
            } : {
                heading: "Отклонить предложение?",
                description: `Предложение о владении организацией «${offer.organization_name}» будет закрыто.`,
                confirmLabel: "Отклонить предложение",
            })) {
                trigger.focus();
                return;
            }
            error.textContent = "";
            for (const control of controls.children) {
                control.disabled = true;
            }
            const result = await request(`${api}${offer.id}/respond/`, "POST", token, {accept});
            offers = offers.map((current) => current.id === result.id ? result : current);
            render();
            status.textContent = accept ? `Вы стали владельцем организации «${result.organization_name}».` : "Предложение отклонено.";
            get("heading").focus();
        } catch (requestError) {
            error.textContent = errorMessage(requestError);
            retry.hidden = false;
        } finally {
            busy = false;
            for (const control of controls.children) {
                control.disabled = false;
            }
            if (trigger.isConnected) {
                trigger.focus();
            }
        }
    }

    setupLoad((data) => {
        offers = data;
        render();
    }, () => {
        offers = [];
        list.replaceChildren();
    });
}

if (api) {
    if (app.dataset.ownershipMode === "manage") {
        setupManage();
    } else {
        setupInbox();
    }
}

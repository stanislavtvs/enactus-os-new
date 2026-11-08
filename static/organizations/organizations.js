import {element, errorMessage, request} from "../departments/directory.js";
import {confirmArchive, setupDialog} from "../ui/collection.js";
import {actionButton, formatDate, requestStatus, setupPlatformList} from "./platform.js";

const app = document.querySelector("#organization-list-app");
const get = (id) => app.querySelector(`#organization-${id}`);
const token = app.querySelector("[name='csrfmiddlewaretoken']").value;
const api = app.dataset.platformUrl;
const requestEndpoint = `${api}creation-requests/`;
const form = get("create") ?? get("request-form");
const organizations = new Map([...app.querySelectorAll("[data-organization-id]")].map((row) =>
    [Number(row.dataset.organizationId), row.querySelector("a").href]));
let withdrawing = false;

function renderRequest(item) {
    const row = element("li", "platform-row");
    row.dataset.requestId = item.id;
    const copy = element("div", "platform-row-copy");
    copy.append(
        element("h3", "", item.name),
        element("p", "muted", `${requestStatus[item.status]} · ${formatDate(item.created_at)}`),
    );
    if (item.decided_at) {
        copy.append(element("p", "muted", `Решение от ${formatDate(item.decided_at)}`));
    }
    if (item.decision_comment) {
        copy.append(element("p", "platform-description", item.decision_comment));
    }
    const organizationUrl = organizations.get(item.organization_id);
    if (item.status === "approved" && item.organization_id) {
        const link = element("a", "", organizationUrl ? "Открыть организацию" : "Обновить список организаций");
        link.href = organizationUrl || window.location.pathname;
        copy.append(link);
    }
    row.append(copy);
    if (item.status === "pending") {
        const withdraw = actionButton("Отозвать", "withdraw");
        withdraw.setAttribute("aria-label", `Отозвать заявку «${item.name}»`);
        withdraw.addEventListener("click", () => withdrawRequest(item, withdraw));
        row.append(withdraw);
    }
    return row;
}

let requests;
if (api) {
    get("requests").hidden = false;
    requests = setupPlatformList({
        get, prefix: "requests", url: requestEndpoint,
        empty: "Вы пока не подавали заявок на создание организации.",
        renderRow: renderRequest,
    });
    requests.load();
}

async function withdrawRequest(item, trigger) {
    if (withdrawing) {
        return;
    }
    withdrawing = true;
    try {
        if (!await confirmArchive(item.name, {
            heading: "Отозвать заявку?",
            description: `Заявка «${item.name}» будет снята с рассмотрения.`,
            confirmLabel: "Отозвать заявку",
        })) {
            return;
        }
        trigger.disabled = true;
        await request(`${requestEndpoint}${item.id}/withdraw/`, "POST", token);
        await requests.load({message: "Заявка отозвана."});
        get("requests-title").focus();
    } catch (requestError) {
        get("requests-error").textContent = errorMessage(requestError);
    } finally {
        withdrawing = false;
        if (trigger.isConnected) {
            trigger.disabled = false;
            trigger.focus();
        }
    }
}

if (form) {
    const creating = form.id === "organization-create";
    const open = get(creating ? "create-open" : "request-open");
    const nameInput = get("name");
    const descriptionInput = get("purpose");
    const error = get("error");
    const modal = setupDialog(get("create-dialog"), open);
    let saving = false;
    let lastName;
    let idempotencyKey;
    open.hidden = false;

    form.addEventListener("submit", async (event) => {
        event.preventDefault();
        if (saving) {
            return;
        }
        const name = nameInput.value.trim();
        const description = descriptionInput?.value.trim();
        error.textContent = "";
        nameInput.removeAttribute("aria-invalid");
        descriptionInput?.removeAttribute("aria-invalid");
        if (!name || Array.from(name).length > 200) {
            error.textContent = "Введите название длиной от 1 до 200 символов.";
            nameInput.setAttribute("aria-invalid", "true");
            nameInput.focus();
            return;
        }
        if (!creating && (!description || Array.from(description).length > 2000)) {
            error.textContent = "Введите описание или цель длиной от 1 до 2000 символов.";
            descriptionInput.setAttribute("aria-invalid", "true");
            descriptionInput.focus();
            return;
        }
        if (creating && name !== lastName) {
            lastName = name;
            idempotencyKey = globalThis.crypto?.randomUUID?.();
        }
        saving = true;
        modal.setBusy(true);
        try {
            if (creating) {
                await request(form.action, "POST", token, {name, idempotency_key: idempotencyKey});
                window.location.reload();
                return;
            }
            await request(requestEndpoint, "POST", token, {name, description});
            modal.close();
            form.reset();
            await requests.load({reset: true, message: "Заявка отправлена на рассмотрение."});
            get("requests-title").focus();
        } catch (requestError) {
            error.textContent = errorMessage(requestError);
        } finally {
            saving = false;
            modal.setBusy(false);
        }
    });
}

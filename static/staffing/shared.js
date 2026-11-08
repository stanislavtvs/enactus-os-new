import {element, errorMessage, request} from "../departments/directory.js";

export {element, request};

export const requestStatuses = {
    draft: "Черновик",
    sent: "Отправлен",
    partial: "Частично выполнен",
    fulfilled: "Выполнен",
    rejected: "Отклонён",
    cancelled: "Отменён",
};

export const allocationStatuses = {
    pending: "Ожидает ответа",
    accepted: "Подтверждено",
    declined: "Отклонено",
    revoked: "Отозвано",
};

export function dateTime(value) {
    return value ? new Intl.DateTimeFormat("ru", {dateStyle: "medium", timeStyle: "short"}).format(new Date(value)) : "";
}

export function button(label, handler, primary = false) {
    const control = element("button", primary ? "button button-primary" : "button staffing-secondary", label);
    control.type = "button";
    control.addEventListener("click", handler);
    return control;
}

export function listRow(title, meta) {
    const row = element("li", "staffing-row");
    const summary = element("div", "");
    const heading = typeof title === "string" ? element("p", "staffing-title", title) : title;
    summary.append(heading, element("p", "staffing-meta", meta));
    row.append(summary);
    return row;
}

export function requestRow(item, queueUrl) {
    const link = element("a", "staffing-title", `${item.project_name} → ${item.department_name}`);
    link.href = `${queueUrl}${item.id}/`;
    const status = requestStatuses[item.status] ?? "Статус неизвестен";
    const details = [status, `подтверждено ${item.accepted_count} из ${item.requested_count}`];
    if (item.awaiting_handler) {
        details.push("Ожидает рассмотрения");
    }
    if (item.specialization) {
        details.push(item.specialization);
    }
    return listRow(link, details.join(" · "));
}

export function formPayload(form) {
    const fields = form.elements;
    const get = (name) => fields.namedItem(name).value;
    const description = get("description").trim();
    if (!description) {
        fields.namedItem("description").focus();
        throw new Error("Опишите работу для специалистов.");
    }
    return {
        requested_count: Number(get("requested_count")),
        description,
        specialization: get("specialization").trim(),
        desired_start: get("desired_start") || null,
        duration_days: get("duration_days") ? Number(get("duration_days")) : null,
    };
}

export function pageState(app) {
    const status = app.querySelector("#staffing-status");
    const error = app.querySelector("#staffing-error");
    const retry = app.querySelector("#staffing-retry");
    const token = app.querySelector('[name="csrfmiddlewaretoken"]')?.value;
    let busy = false;

    function setBusy(value) {
        busy = value;
        for (const region of app.querySelectorAll("form, ul, .staffing-actions, #staffing-queue-filters")) {
            region.inert = value;
        }
    }

    function start(message) {
        setBusy(true);
        retry.hidden = true;
        status.textContent = message;
        error.textContent = "";
    }

    function finish(message = "") {
        setBusy(false);
        status.textContent = message;
    }

    function fail(cause, message = "") {
        finish(message);
        error.textContent = errorMessage(cause);
        retry.hidden = false;
    }

    return {
        start,
        finish,
        fail,
        get busy() { return busy; },
        retry,
        validation(cause) {
            error.textContent = cause.message;
        },
        mutate(url, method, payload = null) {
            return request(url, method, token, payload);
        },
    };
}

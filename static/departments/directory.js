export function element(tag, className, text) {
    const node = document.createElement(tag);
    node.className = className;
    if (text !== undefined) {
        node.textContent = text;
    }
    return node;
}

export function action(label, accessibleLabel, handler, primary = false) {
    const button = element("button", primary ? "button button-primary" : "button department-secondary", label);
    button.type = "button";
    button.setAttribute("aria-label", accessibleLabel);
    button.addEventListener("click", handler);
    return button;
}

export async function request(url, method = "GET", token = "", payload = null) {
    const headers = {Accept: "application/json"};
    if (method !== "GET") {
        headers["X-CSRFToken"] = token;
    }
    if (payload !== null) {
        headers["Content-Type"] = "application/json";
    }
    const response = await fetch(url, {
        method,
        headers,
        mode: "same-origin",
        body: payload === null ? undefined : JSON.stringify(payload),
    });
    if (!response.ok) {
        const errors = {
            401: "Сессия завершилась. Войдите снова.",
            403: "Не удалось подтвердить доступ. Обновите страницу и проверьте права.",
            404: "Запись или каталог недоступны.",
            409: "Запись уже изменилась. Обновите список и повторите попытку.",
            422: "Не удалось проверить данные запроса.",
        };
        const data = await response.json().catch(() => null);
        const detail = response.status === 409 && typeof data?.detail === "string" ? data.detail : "";
        throw new Error(detail || errors[response.status] || "Не удалось выполнить запрос.");
    }
    return response.status === 204 ? null : response.json();
}

export function errorMessage(error) {
    return error instanceof TypeError
        ? "Соединение прервалось. Обновите список перед повторной попыткой."
        : error.message;
}

export function invitationStatus(status) {
    const labels = {
        pending: "Ожидает ответа",
        accepted: "Принято",
        declined: "Отклонено",
        revoked: "Отозвано",
        expired: "Истекло",
    };
    return labels[status] ?? "Статус неизвестен";
}

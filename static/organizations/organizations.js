const form = document.querySelector("#organization-create");
const nameInput = form.elements.namedItem("name");
const csrfToken = form.elements.namedItem("csrfmiddlewaretoken").value;
const submitButton = form.querySelector('button[type="submit"]');
const error = document.querySelector("#organization-error");

const messages = {
    401: "Сессия завершилась. Войдите снова и повторите попытку.",
    403: "Не удалось подтвердить право на создание. Обновите страницу и проверьте доступ.",
    422: "Проверьте название организации.",
};

form.addEventListener("submit", async (event) => {
    event.preventDefault();
    error.textContent = "";
    nameInput.removeAttribute("aria-invalid");

    const name = nameInput.value.trim();
    if (!name || Array.from(name).length > 200) {
        error.textContent = "Введите название длиной от 1 до 200 символов.";
        nameInput.setAttribute("aria-invalid", "true");
        nameInput.focus();
        return;
    }

    submitButton.disabled = true;
    submitButton.textContent = "Создаём…";

    try {
        const response = await fetch(form.action, {
            method: "POST",
            mode: "same-origin",
            headers: {
                "Content-Type": "application/json",
                "X-CSRFToken": csrfToken,
            },
            body: JSON.stringify({ name }),
        });

        if (response.status === 201) {
            window.location.reload();
            return;
        }

        error.textContent = messages[response.status] || "Не удалось подтвердить создание. Обновите список перед повторной попыткой.";
        if (response.status === 422) {
            nameInput.setAttribute("aria-invalid", "true");
            nameInput.focus();
        }
    } catch {
        error.textContent = "Соединение прервалось. Обновите список перед повторной попыткой.";
    }

    submitButton.disabled = false;
    submitButton.textContent = "Создать организацию";
});

form.hidden = false;

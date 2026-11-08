import {collectionActions, confirmArchive, setupDialog, setupDisclosures} from "../ui/collection.js";
import {element, errorMessage, request, selectOptions, statuses} from "./shared.js";

export function taskDependencies(app, {api, base, token, refresh}) {
    const find = (selector) => app.querySelector(selector);
    const panel = find("#task-dependencies");
    const heading = find("#task-dependencies-heading");
    const list = find("#task-dependency-list");
    const add = find("#task-add-dependency");
    const notice = find("#task-dependencies-notice");
    const error = find("#task-dependencies-error");
    const dialogElement = find("#task-dependency-dialog");
    const dialog = setupDialog(dialogElement);
    const form = find("#task-dependency-form");
    const search = find("#task-dependency-search");
    const searchStatus = find("#task-dependency-search-status");
    const predecessor = find("#task-dependency-predecessor");
    const kind = find("#task-dependency-kind");
    const formError = find("#task-dependency-form-error");
    const save = find("#task-dependency-save");
    const endpoint = `${api}dependencies/`;
    let data;
    let task;
    let busy = false;
    let searchTimer;
    let searchVersion = 0;

    setupDisclosures(panel);

    function label(dependency) {
        if (dependency.kind === "strict") {
            if (!dependency.unresolved) {
                return "Строгая · выполнена";
            }
            return data.blocked ? "Блокирует выполнение" : "Строгая · предшественник не завершён";
        }
        return dependency.unresolved ? "Предупреждение" : "Информационная · выполнена";
    }

    function render() {
        list.replaceChildren();
        for (const dependency of data.items) {
            const item = element("li", "task-row");
            const summary = element("div", "");
            const target = dependency.predecessor;
            const title = target ? element("a", "", target.name) : element("p", "", "Предшествующая задача недоступна");
            if (target) {
                title.href = `${app.dataset.taskListUrl}${target.id}/`;
            }
            const meta = element("p", "");
            const badge = data.blocked && dependency.kind === "strict" && dependency.unresolved
                ? "task-badge task-badge-overdue" : "task-badge";
            meta.append(element("span", badge, label(dependency)));
            if (target) {
                meta.append(element("span", "task-meta", statuses[target.status] ?? target.status));
            }
            summary.append(title, meta);
            item.append(summary);
            if (data.can_manage) {
                item.append(collectionActions(target?.name ?? "Зависимость", [{
                    label: "Удалить связь",
                    danger: true,
                    onSelect: () => remove(dependency),
                }]));
            }
            list.append(item);
        }
        add.hidden = !data.can_manage;
        const summary = find("#task-dependency-summary");
        if (data.blocked) {
            summary.textContent = "Начало работы заблокировано до завершения строгих предшественников.";
        } else if (task.has_dependency_warnings) {
            summary.textContent = "Есть незавершённые информационные зависимости. Они не мешают начать работу.";
        } else {
            summary.textContent = "";
        }
        find("#task-dependencies-empty").hidden = data.items.length > 0;
    }

    async function load(current) {
        task = current;
        panel.hidden = true;
        data = await request(endpoint);
        render();
        panel.hidden = false;
    }

    async function searchTasks() {
        const version = ++searchVersion;
        save.disabled = true;
        predecessor.disabled = true;
        searchStatus.textContent = "Ищем задачи…";
        formError.textContent = "";
        try {
            const query = new URLSearchParams({q: search.value.trim(), page_size: 30});
            const {items} = await request(`${base}tasks/?${query}`);
            if (version !== searchVersion || !dialogElement.open) {
                return;
            }
            const excluded = new Set([task.id, ...data.items.map((item) => item.predecessor?.id)]);
            const choices = items.filter(({id}) => !excluded.has(id)).map(({id, name}) => [id, name]);
            selectOptions(predecessor, "Выберите задачу", choices);
            predecessor.disabled = !choices.length;
            save.disabled = !choices.length;
            searchStatus.textContent = choices.length ? "Уточните поиск, если нужной задачи нет в списке." : "Подходящих задач не найдено.";
        } catch (cause) {
            if (version === searchVersion && dialogElement.open) {
                searchStatus.textContent = "";
                formError.textContent = errorMessage(cause);
            }
        }
    }

    function openCreate() {
        if (busy || !data.can_manage) {
            return;
        }
        form.reset();
        selectOptions(predecessor, "Выберите задачу", []);
        dialog.open(add, search);
        searchTasks();
    }

    async function saveChange(url, method, payload, message, isCreate = false) {
        if (busy) {
            return;
        }
        busy = true;
        error.textContent = "";
        formError.textContent = "";
        panel.inert = true;
        if (isCreate) {
            dialog.setBusy(true);
        }
        let success = false;
        try {
            await request(url, method, token, payload);
            if (isCreate) {
                dialog.close();
            }
            await refresh();
            notice.textContent = message;
            success = true;
        } catch (cause) {
            (isCreate ? formError : error).textContent = errorMessage(cause);
        } finally {
            busy = false;
            panel.inert = false;
            if (isCreate) {
                dialog.setBusy(false);
            }
            if (success) {
                heading.focus();
            }
        }
    }

    async function remove(dependency) {
        if (busy) {
            return;
        }
        const confirmed = await confirmArchive(dependency.predecessor?.name ?? "", {
            heading: "Удалить зависимость?",
            description: "Будет удалена только связь между задачами.",
            confirmLabel: "Удалить связь",
        });
        if (confirmed) {
            return saveChange(`${endpoint}${dependency.id}/`, "DELETE", null, "Зависимость удалена.");
        }
    }

    add.addEventListener("click", openCreate);
    search.addEventListener("input", () => {
        clearTimeout(searchTimer);
        searchVersion++;
        save.disabled = true;
        predecessor.disabled = true;
        searchTimer = setTimeout(searchTasks, 250);
    });
    dialogElement.addEventListener("close", () => {
        clearTimeout(searchTimer);
        searchVersion++;
    });
    form.addEventListener("submit", (event) => {
        event.preventDefault();
        if (predecessor.disabled || !predecessor.value) {
            return;
        }
        return saveChange(endpoint, "POST", {
            predecessor_id: Number(predecessor.value), kind: kind.value,
        }, "Зависимость добавлена.", true);
    });

    return {panel, load};
}

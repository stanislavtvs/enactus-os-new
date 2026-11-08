import {errorMessage, request, selectOptions} from "./shared.js";

export function setupCreationAssignee(form, {api, project, department, parentId = null}) {
    const field = form.querySelector("[data-assignee-field]");
    const hint = form.querySelector("[data-assignee-hint]");
    const select = field.querySelector("select");
    let generation = 0;

    function reset() {
        generation++;
        field.hidden = true;
        select.disabled = true;
        select.replaceChildren();
        hint.textContent = "";
    }

    async function refresh() {
        reset();
        const current = generation;
        if ((project.required && !project.value) || (department.required && !department.value)) {
            hint.textContent = "Уточните проект и подразделение, чтобы проверить возможность назначения.";
            return;
        }

        const query = new URLSearchParams({
            project_id: project.value || "0",
            department_id: department.value || "0",
        });
        if (parentId !== null) {
            query.set("parent_id", parentId);
        }
        hint.textContent = "Проверяем возможность назначения…";
        try {
            const options = await request(`${api}task-assignee-options/?${query}`);
            if (current !== generation) {
                return;
            }
            if (options.assignment_mode === "department") {
                hint.textContent = "Исполнитель будет назначен руководителем подразделения после направления задачи на распределение.";
            } else if (!options.can_assign) {
                hint.textContent = "В этой области задачу можно создать без исполнителя.";
            } else if (!options.candidates.length) {
                hint.textContent = project.value
                    ? "В команде проекта пока нет доступных исполнителей."
                    : "Доступных исполнителей пока нет.";
            } else {
                selectOptions(select, "Назначить позже", options.candidates.map(({id, username}) => [id, username]));
                field.hidden = false;
                select.disabled = false;
                hint.textContent = "Назначение сохранится вместе с задачей.";
            }
        } catch (error) {
            if (current === generation) {
                hint.textContent = `Не удалось загрузить исполнителей. ${errorMessage(error)} Задачу можно создать без назначения.`;
            }
        }
    }

    return {
        refresh,
        reset,
        value: () => !select.disabled && select.value ? Number(select.value) : null,
    };
}

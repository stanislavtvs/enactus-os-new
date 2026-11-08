import {collectionActions, confirmArchive, setupDialog, setupDisclosures} from "../ui/collection.js";
import {setupCreationAssignee} from "./creation-assignee.js";
import {dateLabel, element, errorMessage, readTaskFields, request, selectOptions, setupPrioritySelect, statuses} from "./shared.js";

const treePageSize = 40;

function memberName(value) {
    return typeof value === "string" ? value : value?.username ?? "";
}

export function taskDecomposition(app, {api, base, token, refresh}) {
    const find = (selector) => app.querySelector(selector);
    const panel = find("#task-decomposition");
    const tree = find("#task-subtasks-tree");
    const more = find("#task-subtasks-more");
    const checklistList = find("#task-checklists-list");
    const notice = find("#task-decomposition-notice");
    const error = find("#task-decomposition-error");
    const childButton = find("#task-add-subtask");
    const moveButton = find("#task-move-parent");
    const addChecklist = find("#task-add-checklist");
    const demoteButton = find("#task-convert-to-item");
    const childDialogElement = find("#task-subtask-dialog");
    const childDialog = setupDialog(childDialogElement);
    const childForm = find("#task-subtask-form");
    const childError = find("#task-subtask-error");
    const childProject = find("#task-child-project");
    const childDepartment = find("#task-child-department");
    const childAssignee = setupCreationAssignee(childForm, {
        api: base, project: childProject, department: childDepartment, parentId: app.dataset.taskId,
    });
    const dialogElement = find("#task-decomposition-dialog");
    const dialog = setupDialog(dialogElement);
    const form = find("#task-decomposition-form");
    const formError = find("#task-decomposition-form-error");
    const nameField = find("#task-decomposition-name-field");
    const name = find("#task-decomposition-name");
    const targetField = find("#task-decomposition-target-field");
    const target = find("#task-decomposition-target");
    const searchField = find("#task-decomposition-search-field");
    const search = find("#task-decomposition-search");
    const save = find("#task-decomposition-save");

    const expanded = new Map();
    let task;
    let details;
    let childOptions;
    let descendants = [];
    let treeInfo;
    let action;
    let busy = false;
    let searchTimer;
    let searchVersion = 0;
    let childVersion = 0;

    setupDisclosures(panel);

    function taskUrl(id) {
        return `${app.dataset.taskListUrl}${id}/`;
    }

    function taskLink(id, label) {
        const link = element("a", "", label);
        link.href = taskUrl(id);
        return link;
    }

    function setBusy(value) {
        busy = value;
        panel.inert = value;
    }

    function renderTree() {
        const root = element("ul", "task-subtask-tree task-subtask-tree-root");
        root.setAttribute("role", "list");
        const ids = new Set(descendants.map(({id}) => id));
        const branches = new Map();
        for (const child of descendants) {
            const parent = ids.has(child.parent_id) ? child.parent_id : null;
            if (!branches.has(parent)) {
                branches.set(parent, []);
            }
            branches.get(parent).push(child);
        }
        const stack = [...(branches.get(null) ?? [])].reverse().map((child) => ({child, list: root, depth: 0}));
        const visited = new Set();
        while (stack.length) {
            const {child, list, depth} = stack.pop();
            if (visited.has(child.id)) {
                continue;
            }
            visited.add(child.id);
            const item = element("li", "");
            const row = element("div", "task-child-row");
            row.style.setProperty("--child-depth", depth);
            item.append(row);
            list.append(item);

            const children = branches.get(child.id) ?? [];
            if (children.length) {
                const branch = element("ul", "task-subtask-tree");
                branch.id = `task-subtasks-branch-${child.id}`;
                branch.setAttribute("role", "list");
                const toggle = element("button", "task-child-disclosure");
                toggle.type = "button";
                toggle.setAttribute("aria-controls", branch.id);
                const setExpanded = (open) => {
                    branch.hidden = !open;
                    toggle.textContent = open ? "▾" : "▸";
                    toggle.setAttribute("aria-expanded", String(open));
                    toggle.setAttribute("aria-label", `${open ? "Свернуть" : "Развернуть"}: ${child.name}`);
                };
                setExpanded(expanded.get(child.id) ?? depth === 0);
                toggle.addEventListener("click", () => {
                    expanded.set(child.id, branch.hidden);
                    setExpanded(branch.hidden);
                });
                row.append(toggle);
                item.append(branch);
                for (let index = children.length - 1; index >= 0; index--) {
                    stack.push({child: children[index], list: branch, depth: depth + 1});
                }
            } else {
                row.append(element("span", "task-child-spacer"));
            }

            const summary = element("div", "task-child-summary");
            const facts = [
                child.workflow_status?.name ?? statuses[child.status] ?? child.status,
                child.is_archived ? "Архив" : "",
                child.assignee ? `Исполнитель: ${memberName(child.assignee)}` : "Без исполнителя",
                child.due_at ? `Срок: ${dateLabel(child.due_at)}` : "",
            ];
            if (depth > 4) {
                facts.push(`Уровень ${depth + 1}`);
            }
            if (child.parent_id && child.parent_id !== task.id && !ids.has(child.parent_id)) {
                facts.push(`Родительская задача №${child.parent_id}`);
            }
            summary.append(taskLink(child.id, child.name), element("p", "task-meta", facts.filter(Boolean).join(" · ")));
            const href = taskUrl(child.id);
            const actions = [{label: "Открыть", href}];
            if (child.permissions?.update) {
                actions.push({label: "Редактировать", href: href + "#edit"});
            }
            if (child.can_move) {
                actions.push({label: "Переместить", href: href + "#move"});
            }
            if (child.permissions?.archive) {
                actions.push({label: "Архивировать", danger: true, onSelect: () => archiveChild(child)});
            }
            row.append(summary, collectionActions(child.name, actions));
        }

        tree.replaceChildren(root);
        find("#task-subtasks-empty").hidden = treeInfo.total !== 0;
        find("#task-subtask-progress").textContent = treeInfo.total
            ? `Завершено ${treeInfo.completed} из ${treeInfo.total} подзадач` : "";
        more.hidden = descendants.length >= treeInfo.total || treeInfo.page * treeInfo.page_size >= treeInfo.total;
    }

    function checklistPath(checklist, item = null) {
        const path = `checklists/${checklist.id}/`;
        return item ? `${path}items/${item.id}/` : path;
    }

    function checklistItem(checklist, item, index) {
        const row = element("li", "task-checklist-item");
        const label = element("label", "");
        const box = document.createElement("input");
        box.type = "checkbox";
        box.id = `task-checklist-item-${item.id}`;
        box.checked = item.is_completed;
        box.disabled = !details.can_manage;
        box.addEventListener("change", async () => {
            if (!await mutate(checklistPath(checklist, item), "PATCH", {is_completed: box.checked},
                box.checked ? "Пункт отмечен выполненным." : "Отметка выполнения снята.", {
                    focusAfter: () => find(`#task-checklist-item-${item.id}`),
                })) {
                box.checked = item.is_completed;
            }
        });
        const text = element("span", "", item.name);
        label.append(box, text);
        const main = element("div", "");
        main.append(label);
        if (item.is_completed && item.completed_at) {
            const by = memberName(item.completed_by);
            main.append(element("p", "task-meta", `Выполнено: ${dateLabel(item.completed_at)}${by ? ` · ${by}` : ""}`));
        }
        row.append(main);
        if (details.can_manage) {
            const actions = [
                {label: "Редактировать", onSelect: (trigger) => editName("Редактировать пункт", trigger, checklistPath(checklist, item), "PATCH", item.name)},
            ];
            if (index > 0) {
                actions.push({label: "Выше", onSelect: () => reorder(checklist, index, index - 1)});
            }
            if (index < checklist.items.length - 1) {
                actions.push({label: "Ниже", onSelect: () => reorder(checklist, index, index + 1)});
            }
            if (details.can_create_subtask) {
                actions.push({label: "Преобразовать в подзадачу", onSelect: (trigger) => openAction(trigger, {
                    heading: "Преобразовать в подзадачу",
                    hint: `«${item.name}» станет новой подзадачей со статусом «Ожидает». История исходного пункта сохранится.`,
                    label: "Преобразовать",
                    path: `${checklistPath(checklist, item)}convert/`,
                    method: "POST",
                    payload: () => ({confirm: true}),
                    success: "Пункт преобразован в подзадачу.",
                    linkToCreated: true,
                })});
            }
            actions.push({label: "Удалить", danger: true, onSelect: (trigger) => confirmDeletion(
                trigger, "Удалить пункт?", item.name, checklistPath(checklist, item), "Пункт удалён.",
            )});
            row.append(collectionActions(item.name, actions));
        }
        return row;
    }

    function quickItem(checklist) {
        const wrapper = element("div", "");
        const quick = element("form", "task-checklist-quick-form");
        quick.method = "post";
        const field = element("div", "form-field");
        const label = element("label", "visually-hidden", `Новый пункт для чек-листа «${checklist.name}»`);
        const input = document.createElement("input");
        input.id = `task-checklist-quick-${checklist.id}`;
        input.name = "name";
        input.placeholder = "Новый пункт";
        input.required = true;
        label.htmlFor = input.id;
        field.append(label, input);
        const submit = element("button", "button collection-secondary task-checklist-quick-submit", "+");
        submit.type = "submit";
        submit.setAttribute("aria-label", `Добавить пункт в чек-лист «${checklist.name}»`);
        submit.title = "Добавить пункт";
        const quickError = element("p", "form-errors task-checklist-quick-error");
        quickError.id = `${input.id}-error`;
        quickError.setAttribute("role", "alert");
        input.setAttribute("aria-describedby", quickError.id);
        quick.append(field, submit);
        wrapper.append(quick, quickError);
        quick.addEventListener("submit", async (event) => {
            event.preventDefault();
            const value = input.value.trim();
            if (!value || Array.from(value).length > 200) {
                quickError.textContent = "Введите название длиной от 1 до 200 символов.";
                input.focus();
                return;
            }
            const saved = await mutate(`${checklistPath(checklist)}items/`, "POST", {name: value}, "Пункт добавлен.", {
                errorTarget: quickError,
                focusAfter: () => find(`#${input.id}`),
            });
            if (!saved) {
                input.focus();
            }
        });
        return wrapper;
    }

    function renderChecklists() {
        let count = 0;
        let completed = 0;
        checklistList.replaceChildren();
        for (const checklist of details.checklists) {
            count += checklist.total;
            completed += checklist.completed;
            const row = element("li", "task-checklist");
            const header = element("div", "task-checklist-heading");
            const summary = element("div", "");
            const title = element("h3", "task-checklist-title", checklist.name);
            const progressLabel = `Выполнено ${checklist.completed} из ${checklist.total} пунктов`;
            summary.append(title, element("p", "task-meta", progressLabel));
            if (checklist.total) {
                const progress = document.createElement("progress");
                progress.max = checklist.total;
                progress.value = checklist.completed;
                progress.setAttribute("aria-label", `${checklist.name}: ${progressLabel}`);
                summary.append(progress);
            }
            header.append(summary);
            if (details.can_manage) {
                header.append(collectionActions(checklist.name, [
                    {label: "Переименовать", onSelect: (trigger) => editName("Переименовать чек-лист", trigger, checklistPath(checklist), "PATCH", checklist.name)},
                    {label: "Удалить", danger: true, onSelect: (trigger) => confirmDeletion(
                        trigger, "Удалить чек-лист?", `${checklist.name} и его пункты`, checklistPath(checklist), "Чек-лист удалён.",
                    )},
                ]));
            }
            row.append(header);
            if (checklist.items.length) {
                const items = element("ul", "task-checklist-items");
                items.setAttribute("role", "list");
                for (const [index, item] of checklist.items.entries()) {
                    items.append(checklistItem(checklist, item, index));
                }
                row.append(items);
            } else {
                row.append(element("p", "task-meta", "В чек-листе пока нет пунктов."));
            }
            if (details.can_manage) {
                row.append(quickItem(checklist));
            }
            checklistList.append(row);
        }
        find("#task-checklists-empty").hidden = details.checklists.length > 0;
        find("#task-checklist-progress").textContent = count ? `Всего выполнено ${completed} из ${count} пунктов` : "";
    }

    function renderConversions() {
        const history = find("#task-conversion-history");
        history.replaceChildren();
        for (const entry of details.conversions) {
            const toTask = entry.direction === "item_to_task";
            const row = element("li", "");
            row.append(element("p", "", `${toTask ? "Пункт → подзадача" : "Подзадача → пункт"}: ${entry.item_name}`));
            if (entry.task_id) {
                row.append(taskLink(entry.task_id, entry.task_name ?? "Открыть задачу"));
            }
            row.append(element("p", "task-meta", [memberName(entry.actor), dateLabel(entry.created_at)].filter(Boolean).join(" · ")));
            history.append(row);
        }
        find("#task-conversion-history-panel").hidden = !details.conversions.length;
    }

    async function load(current) {
        task = current;
        panel.hidden = true;
        const [data, page] = await Promise.all([
            request(`${api}decomposition/`),
            request(`${api}subtasks/?page=1&page_size=${treePageSize}`),
        ]);
        details = data;
        treeInfo = page;
        descendants = page.items;
        childButton.hidden = !details.can_create_subtask;
        moveButton.hidden = !details.can_move;
        addChecklist.hidden = !details.can_manage;
        demoteButton.hidden = !details.can_convert_to_item || !task.parent_id;
        const conversionReason = find("#task-convert-to-item-reason");
        conversionReason.hidden = !details.convert_to_item_reason;
        conversionReason.textContent = details.convert_to_item_reason
            ? `${details.convert_to_item_reason} Оставьте её полноценной задачей.` : "";
        renderTree();
        renderChecklists();
        renderConversions();
        panel.hidden = false;
    }

    async function loadMore() {
        if (busy) {
            return;
        }
        setBusy(true);
        error.textContent = "";
        try {
            const page = await request(`${api}subtasks/?page=${treeInfo.page + 1}&page_size=${treePageSize}`);
            treeInfo = page;
            const known = new Set(descendants.map(({id}) => id));
            descendants.push(...page.items.filter(({id}) => !known.has(id)));
            renderTree();
        } catch (cause) {
            error.textContent = errorMessage(cause);
        } finally {
            setBusy(false);
            (more.hidden ? find("#task-subtasks-heading") : more).focus();
        }
    }

    async function mutate(path, method, payload, message, {
        errorTarget = error, afterWrite, hrefFrom, focusAfter, endpoint = api,
    } = {}) {
        if (busy) {
            return false;
        }
        setBusy(true);
        let focus;
        error.textContent = "";
        errorTarget.textContent = "";
        try {
            const result = await request(endpoint + path, method, token, payload);
            afterWrite?.();
            await refresh();
            const href = hrefFrom?.(result);
            if (href) {
                const link = element("a", "", "Открыть подзадачу");
                link.href = href;
                notice.replaceChildren(`${message} `, link);
            } else {
                notice.textContent = message;
            }
            focus = focusAfter?.() ?? find(path === "parent/" || path === "subtasks/"
                ? "#task-subtasks-heading" : "#task-checklists-heading");
            return true;
        } catch (cause) {
            errorTarget.textContent = errorMessage(cause);
            return false;
        } finally {
            setBusy(false);
            focus?.focus();
        }
    }

    async function archiveChild(child) {
        if (busy || !await confirmArchive(child.name, {
            heading: "Архивировать подзадачу?",
            description: `«${child.name}» останется в архиве. Её дочерние задачи сохранятся.`,
        })) {
            return;
        }
        return mutate("", "DELETE", null, "Подзадача архивирована.", {
            endpoint: `${base}tasks/${child.id}/`,
            focusAfter: () => find("#task-subtasks-heading"),
        });
    }

    function reorder(checklist, from, to) {
        const ids = checklist.items.map(({id}) => id);
        [ids[from], ids[to]] = [ids[to], ids[from]];
        return mutate(`${checklistPath(checklist)}order/`, "PUT", {item_ids: ids}, "Порядок пунктов сохранён.");
    }

    function openAction(trigger, spec) {
        if (busy) {
            return;
        }
        action = spec;
        clearTimeout(searchTimer);
        searchVersion++;
        find("#task-decomposition-dialog-heading").textContent = spec.heading;
        const hint = find("#task-decomposition-dialog-hint");
        hint.textContent = spec.hint ?? "";
        hint.hidden = !spec.hint;
        nameField.hidden = !spec.nameField;
        name.disabled = !spec.nameField;
        name.required = Boolean(spec.nameField);
        name.value = spec.name ?? "";
        targetField.hidden = !spec.targetLabel;
        target.disabled = !spec.targetLabel;
        target.required = Boolean(spec.targetRequired);
        find("#task-decomposition-target-label").textContent = spec.targetLabel ?? "";
        searchField.hidden = !spec.parentSearch;
        search.value = "";
        formError.textContent = "";
        save.textContent = spec.label ?? "Сохранить";
        save.disabled = false;
        const initial = spec.nameField ? name
            : spec.parentSearch ? search
            : spec.targetLabel ? target
            : form.querySelector("[data-dialog-close]");
        dialog.open(trigger, initial);
    }

    function editName(heading, trigger, path, method, value = "") {
        openAction(trigger, {
            heading, path, method, nameField: true, name: value, success: "Изменения сохранены.",
            payload: () => {
                const text = name.value.trim();
                if (!text || Array.from(text).length > 200) {
                    name.focus();
                    throw new Error("Введите название длиной от 1 до 200 символов.");
                }
                return {name: text};
            },
        });
    }

    function confirmDeletion(trigger, heading, hint, path, success) {
        openAction(trigger, {
            heading, hint, label: "Удалить", path, method: "DELETE", payload: () => null, success,
        });
    }

    async function loadParentChoices(query = "") {
        const version = ++searchVersion;
        save.disabled = true;
        formError.textContent = "";
        try {
            const filters = new URLSearchParams({q: query, page_size: 30});
            const {items} = await request(`${base}tasks/?${filters}`);
            if (version !== searchVersion) {
                return;
            }
            const excluded = new Set([task.id, ...descendants.map(({id}) => id)]);
            const choices = items.filter(({id}) => !excluded.has(id)).map(({id, name}) => [id, name]);
            if (task.parent && !choices.some(([id]) => id === task.parent.id)) {
                choices.unshift([task.parent.id, task.parent.name]);
            }
            const selected = target.value;
            selectOptions(target, "Без родительской задачи", choices, selected);
            if (target.selectedIndex < 0) {
                target.selectedIndex = 0;
            }
        } catch (cause) {
            if (version === searchVersion) {
                formError.textContent = errorMessage(cause);
            }
        } finally {
            if (version === searchVersion) {
                save.disabled = false;
            }
        }
    }

    function openMove(trigger = moveButton) {
        if (busy || !details?.can_move) {
            return;
        }
        selectOptions(target, "Без родительской задачи", task.parent ? [[task.parent.id, task.parent.name]] : [], task.parent_id);
        openAction(trigger, {
            heading: "Изменить родительскую задачу", targetLabel: "Родительская задача", parentSearch: true,
            hint: "Найдите задачу в этой организации. Связи с самой задачей и её потомками недоступны.",
            path: "parent/", method: "PUT", success: "Родительская задача изменена.",
            payload: () => ({parent_id: target.value ? Number(target.value) : null}),
        });
        loadParentChoices();
    }

    async function openDemote(trigger) {
        if (busy) {
            return;
        }
        selectOptions(target, "Выберите чек-лист", []);
        openAction(trigger, {
            heading: "Преобразовать в пункт чек-листа",
            hint: "Выберите чек-лист родительской задачи. Система проверит, что преобразование сохранит значимые данные.",
            targetLabel: "Чек-лист родительской задачи", targetRequired: true,
            label: "Преобразовать", path: "convert-to-item/", method: "POST", success: "Задача преобразована в пункт чек-листа.",
            payload: () => ({checklist_id: Number(target.value), confirm: true}),
        });
        const version = ++searchVersion;
        save.disabled = true;
        target.disabled = true;
        try {
            const parent = await request(`${base}tasks/${task.parent_id}/decomposition/`);
            if (version !== searchVersion || !dialogElement.open) {
                return;
            }
            const choices = parent.can_manage ? parent.checklists.map(({id, name}) => [id, name]) : [];
            selectOptions(target, "Выберите чек-лист", choices);
            target.disabled = !choices.length;
            save.disabled = !choices.length;
            if (!choices.length) {
                formError.textContent = "Нет доступных чек-листов у родительской задачи.";
            }
        } catch (cause) {
            if (version === searchVersion && dialogElement.open) {
                formError.textContent = errorMessage(cause);
            }
        }
    }

    function childProjects() {
        return childOptions.projects.filter((project) => project.can_create || project.department_ids.length);
    }

    function setChildDepartments(preferred = "") {
        const project = childOptions.projects.find(({id}) => String(id) === childProject.value);
        const departments = childOptions.departments.filter(({id, can_create}) =>
            project ? project.department_ids.includes(id) : can_create);
        const optional = project ? project.can_create : childOptions.can_create_standalone;
        selectOptions(childDepartment, optional ? "Без подразделения" : "Выберите подразделение",
            departments.map(({id, name}) => [id, name]), preferred);
        if (childDepartment.selectedIndex < 0) {
            childDepartment.selectedIndex = 0;
        }
        childDepartment.required = !optional;
        childDepartment.options[0].disabled = !optional;
        if (!optional && !childDepartment.value && departments.length === 1) {
            childDepartment.value = String(departments[0].id);
        }
    }

    async function openChild(trigger) {
        if (busy) {
            return;
        }
        childForm.reset();
        childAssignee.reset();
        childError.textContent = "";
        childOptions = null;
        childProject.disabled = true;
        childDepartment.disabled = true;
        const submit = childForm.querySelector('button[type="submit"]');
        submit.disabled = true;
        const version = ++childVersion;
        childDialog.open(trigger, childForm.elements.namedItem("name"));
        try {
            const options = await request(`${base}task-options/`);
            if (version !== childVersion || !childDialogElement.open) {
                return;
            }
            childOptions = options;
            setupPrioritySelect(childForm.elements.namedItem("priority_id"), options.priorities);
            const projects = childProjects();
            const standalone = childOptions.can_create_standalone || childOptions.departments.some(({can_create}) => can_create);
            selectOptions(childProject, standalone ? "Без проекта" : "Выберите проект",
                projects.map(({id, name}) => [id, name]), task.project?.id);
            if (childProject.selectedIndex < 0) {
                childProject.selectedIndex = 0;
            }
            childProject.required = !standalone;
            childProject.options[0].disabled = !standalone;
            if (!standalone && !childProject.value && projects.length === 1) {
                childProject.value = String(projects[0].id);
            }
            setChildDepartments(task.department?.id);
            childProject.disabled = false;
            childDepartment.disabled = false;
            submit.disabled = false;
            childAssignee.refresh();
        } catch (cause) {
            if (version === childVersion && childDialogElement.open) {
                childOptions = null;
                childError.textContent = errorMessage(cause);
            }
        }
    }

    search.addEventListener("input", () => {
        clearTimeout(searchTimer);
        searchVersion++;
        save.disabled = true;
        searchTimer = setTimeout(() => loadParentChoices(search.value.trim()), 250);
    });
    dialogElement.addEventListener("close", () => {
        clearTimeout(searchTimer);
        searchVersion++;
    });
    childDialogElement.addEventListener("close", () => {
        if (!childDialogElement.open) {
            childVersion++;
            childAssignee.reset();
        }
    });
    more.addEventListener("click", loadMore);
    childButton.addEventListener("click", () => openChild(childButton));
    moveButton.addEventListener("click", () => openMove(moveButton));
    addChecklist.addEventListener("click", () => editName("Новый чек-лист", addChecklist, "checklists/", "POST"));
    demoteButton.addEventListener("click", () => openDemote(demoteButton));
    childProject.addEventListener("change", () => {
        setChildDepartments();
        childAssignee.refresh();
    });
    childDepartment.addEventListener("change", childAssignee.refresh);

    form.addEventListener("submit", async (event) => {
        event.preventDefault();
        if (busy) {
            return;
        }
        let payload;
        try {
            payload = action.payload();
        } catch (cause) {
            formError.textContent = cause.message;
            return;
        }
        dialog.setBusy(true);
        try {
            await mutate(action.path, action.method, payload, action.success, {
                errorTarget: formError,
                afterWrite: () => dialog.close(),
                hrefFrom: action.linkToCreated ? (result) => taskUrl(result.id) : null,
            });
        } finally {
            dialog.setBusy(false);
        }
    });

    childForm.addEventListener("submit", async (event) => {
        event.preventDefault();
        if (!childOptions || busy) {
            return;
        }
        let payload;
        try {
            payload = readTaskFields(childForm);
        } catch (cause) {
            childError.textContent = cause.message;
            return;
        }
        payload.project_id = childProject.value ? Number(childProject.value) : null;
        payload.department_id = childDepartment.value ? Number(childDepartment.value) : null;
        const assigneeId = childAssignee.value();
        if (assigneeId !== null) {
            payload.assignee_id = assigneeId;
        }
        childDialog.setBusy(true);
        try {
            await mutate("subtasks/", "POST", payload, "Подзадача создана.", {
                errorTarget: childError,
                afterWrite: () => childDialog.close(),
                hrefFrom: (result) => taskUrl(result.id),
            });
        } finally {
            childDialog.setBusy(false);
        }
    });

    return {panel, load, openMove};
}

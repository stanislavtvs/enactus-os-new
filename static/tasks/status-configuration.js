import {collectionActions, confirmArchive, setupDialog} from "../ui/collection.js";
import {element, errorMessage, request, selectOptions, statuses, workflowStatusChip} from "./shared.js";

export function setupStatusConfiguration(app, token) {
    const find = (selector) => app.querySelector(selector);
    function section(suffix, singular) {
        const prefix = `#task-${suffix}`;
        return {
            endpoint: `${app.dataset.orgUrl}task-${suffix}/`,
            list: find(`${prefix}-list`),
            heading: find(`${prefix}-heading`),
            notice: find(`${prefix}-notice`),
            status: find(`${prefix}-status`),
            error: find(`${prefix}-error`),
            retry: find(`${prefix}-retry`),
            add: find(`#task-add-${singular}`),
            formError: find(`#task-${singular}-form-error`),
            dialog: setupDialog(find(`#task-${singular}-dialog`)),
        };
    }

    const catalog = section("statuses", "status");
    const rules = section("transitions", "transition");
    const statusForm = find("#task-status-editor-form");
    const transitionForm = find("#task-transition-form");
    const from = find("#task-transition-from");
    const to = find("#task-transition-to");
    const permissionFields = find("#task-transition-permissions");
    let statusData;
    let transitionData;
    let statusById = new Map();
    let editingStatus;
    let editingTransition;
    let busy = false;

    const statusField = (name) => statusForm.elements.namedItem(name);
    const transitionField = (name) => transitionForm.elements.namedItem(name);

    function statusLabel(item) {
        return `${item.name} · ${statuses[item.stage] ?? item.stage}${item.is_active ? "" : " · Архив"}`;
    }

    function renderStatuses() {
        catalog.list.replaceChildren();
        for (const item of statusData.items) {
            const row = element("li", "task-row task-configuration-item");
            const body = element("div", "");
            const title = workflowStatusChip(item);
            title.classList.add("task-configuration-label");
            const details = [statuses[item.stage] ?? item.stage, `порядок ${item.sort_order}`, `код ${item.code}`];
            if (item.is_default) {
                details.push("По умолчанию");
            }
            if (!item.is_active) {
                details.push("Архив");
            }
            body.append(title, element("p", "task-meta task-configuration-meta", details.join(" · ")));
            row.append(body);
            if (statusData.can_manage && item.is_active) {
                const actions = [{label: "Редактировать", onSelect: (trigger) => openStatus(trigger, item)}];
                if (!item.is_default) {
                    actions.push({label: "Назначить по умолчанию", onSelect: () => mutate(
                        catalog, item.id, "PATCH", {is_default: true}, "Статус этапа по умолчанию обновлён.",
                    )});
                    actions.push({label: "Архивировать", danger: true, onSelect: () => archiveStatus(item)});
                }
                row.append(collectionActions(item.name, actions));
            }
            catalog.list.append(row);
        }
        catalog.add.hidden = !statusData.can_manage;
        catalog.status.textContent = statusData.items.length ? "" : "Статусов пока нет.";
    }

    function renderTransitions() {
        rules.list.replaceChildren();
        const permissionNames = new Map(transitionData.permission_options.map(({code, label}) => [code, label]));
        for (const item of transitionData.items) {
            const source = statusById.get(item.from_status_id);
            const destination = statusById.get(item.to_status_id);
            const title = `${source?.name ?? "Исходный статус"} → ${destination?.name ?? "Целевой статус"}`;
            const row = element("li", "task-row task-configuration-item");
            const body = element("div", "");
            const route = source && destination
                ? `${statuses[source.stage] ?? source.stage} → ${statuses[destination.stage] ?? destination.stage}`
                : "";
            const details = [
                route,
                item.is_enabled && destination?.is_active === false ? "Целевой статус в архиве"
                    : item.is_enabled ? "Разрешён" : "Выключен",
                item.allow_assignee ? "Доступен основному исполнителю" : "Только с правом смены статуса",
            ].filter(Boolean);
            if (item.required_permissions.length) {
                details.push(`Права: ${item.required_permissions.map((code) => permissionNames.get(code) ?? code).join(", ")}`);
            }
            body.append(element("p", "task-configuration-label", title),
                element("p", "task-meta task-configuration-meta", details.join(" · ")));
            row.append(body);
            if (transitionData.can_manage && statusData) {
                row.append(collectionActions(title, [{
                    label: "Редактировать",
                    onSelect: (trigger) => openTransition(trigger, item),
                }]));
            }
            rules.list.append(row);
        }
        rules.add.hidden = !transitionData.can_manage || !statusData?.items.some(({is_active}) => is_active)
            || statusData.items.length < 2;
        rules.status.textContent = transitionData.items.length ? "" : "Переходов пока нет.";
    }

    function setBusy(value) {
        busy = value;
        for (const panel of [catalog, rules]) {
            panel.add.disabled = value;
            panel.retry.disabled = value;
            panel.list.inert = value;
            panel.list.setAttribute("aria-busy", String(value));
        }
    }

    async function load() {
        setBusy(true);
        for (const panel of [catalog, rules]) {
            panel.status.textContent = "Загрузка…";
            panel.error.textContent = "";
            panel.retry.hidden = true;
        }
        try {
            const [statusResult, transitionResult] = await Promise.allSettled([
                request(catalog.endpoint), request(rules.endpoint),
            ]);
            if (statusResult.status === "fulfilled") {
                statusData = statusResult.value;
                statusById = new Map(statusData.items.map((item) => [item.id, item]));
                renderStatuses();
            } else {
                catalog.status.textContent = "";
                catalog.error.textContent = errorMessage(statusResult.reason);
                catalog.retry.hidden = false;
            }
            if (transitionResult.status === "fulfilled") {
                transitionData = transitionResult.value;
                renderTransitions();
            } else {
                rules.status.textContent = "";
                rules.error.textContent = errorMessage(transitionResult.reason);
                rules.retry.hidden = false;
            }
        } finally {
            setBusy(false);
        }
    }

    async function mutate(panel, id, method, payload, message, fromDialog = false) {
        if (busy) {
            return;
        }
        setBusy(true);
        panel.error.textContent = "";
        panel.notice.textContent = "";
        if (fromDialog) {
            panel.dialog.setBusy(true);
        }
        try {
            await request(id ? `${panel.endpoint}${id}/` : panel.endpoint, method, token, payload);
            if (fromDialog) {
                panel.dialog.close();
            }
            panel.notice.textContent = message;
            await load();
            panel.heading.focus();
        } catch (error) {
            (fromDialog ? panel.formError : panel.error).textContent = errorMessage(error);
        } finally {
            setBusy(false);
            if (fromDialog) {
                panel.dialog.setBusy(false);
            }
        }
    }

    function openStatus(trigger, item = null) {
        if (busy || !statusData.can_manage) {
            return;
        }
        editingStatus = item;
        statusForm.reset();
        catalog.formError.textContent = "";
        find("#task-status-dialog-heading").textContent = item ? "Редактировать статус" : "Новый статус";
        statusField("name").value = item?.name ?? "";
        statusField("code").value = item?.code ?? "";
        statusField("code").readOnly = Boolean(item);
        statusField("code").required = !item;
        const stage = statusField("stage");
        selectOptions(stage, "Выберите этап", Object.entries(statuses), item?.stage);
        stage.disabled = Boolean(item);
        const maxOrder = statusData.items.reduce((max, status) => Math.max(max, status.sort_order), 0);
        statusField("sort_order").value = item?.sort_order ?? Math.min(2147483647, maxOrder + 1);
        statusField("color").value = item?.color ?? "#6b7280";
        catalog.dialog.open(trigger, statusField("name"));
    }

    async function archiveStatus(item) {
        if (busy) {
            return;
        }
        if (await confirmArchive(item.name, {
            heading: "Архивировать статус?",
            description: `«${item.name}» исчезнет из вариантов перехода. Существующие задачи сохранят этот статус до смены на активный.`,
            confirmLabel: "Архивировать",
        })) {
            return mutate(catalog, item.id, "DELETE", null, "Статус архивирован.");
        }
    }

    function transitionTargets(selected) {
        const source = statusById.get(Number(from.value));
        const stageTargets = transitionData.stage_targets?.[source?.stage];
        const choices = statusData.items.filter((item) =>
            (editingTransition ? item.id === editingTransition.to_status_id
                : item.is_active && String(item.id) !== from.value && (!stageTargets || stageTargets.includes(item.stage))))
            .map((item) => [item.id, statusLabel(item)]);
        selectOptions(to, "Выберите целевой статус", choices, selected);
        if (to.selectedIndex < 0) {
            to.selectedIndex = 0;
        }
    }

    function openTransition(trigger, item = null) {
        if (busy || !transitionData.can_manage || !statusData) {
            return;
        }
        editingTransition = item;
        transitionForm.reset();
        rules.formError.textContent = "";
        find("#task-transition-dialog-heading").textContent = item ? "Редактировать переход" : "Новый переход";
        const sources = statusData.items.filter((status) => !item || status.id === item.from_status_id);
        selectOptions(from, "Выберите исходный статус", sources.map((status) => [status.id, statusLabel(status)]), item?.from_status_id);
        transitionTargets(item?.to_status_id);
        from.disabled = Boolean(item);
        to.disabled = Boolean(item);
        const targetArchived = item && statusById.get(item.to_status_id)?.is_active === false;
        transitionField("is_enabled").checked = !targetArchived && (item?.is_enabled ?? true);
        transitionField("is_enabled").disabled = Boolean(targetArchived);
        transitionField("allow_assignee").checked = item?.allow_assignee ?? false;
        permissionFields.replaceChildren();
        const selected = new Set(item?.required_permissions ?? []);
        for (const {code, label} of transitionData.permission_options) {
            const option = element("label", "");
            const checkbox = document.createElement("input");
            checkbox.type = "checkbox";
            checkbox.name = "required_permissions";
            checkbox.value = code;
            checkbox.checked = selected.has(code);
            option.append(checkbox, document.createTextNode(label));
            permissionFields.append(option);
        }
        permissionFields.closest("details").open = selected.size > 0;
        const initial = item ? transitionField(targetArchived ? "allow_assignee" : "is_enabled") : from;
        rules.dialog.open(trigger, initial);
    }

    statusForm.addEventListener("submit", (event) => {
        event.preventDefault();
        const name = statusField("name").value.trim();
        const code = statusField("code").value.trim();
        catalog.formError.textContent = "";
        if (!name || Array.from(name).length > 120) {
            catalog.formError.textContent = "Введите название длиной от 1 до 120 символов.";
            statusField("name").focus();
            return;
        }
        if (!editingStatus && !/^[a-z0-9]+(?:[-_][a-z0-9]+)*$/.test(code)) {
            catalog.formError.textContent = "Введите технический код строчными латинскими буквами и цифрами.";
            statusField("code").focus();
            return;
        }
        const payload = {
            name,
            color: statusField("color").value,
            sort_order: Number(statusField("sort_order").value),
        };
        if (!editingStatus) {
            payload.code = code;
            payload.stage = statusField("stage").value;
        }
        return mutate(catalog, editingStatus?.id, editingStatus ? "PATCH" : "POST", payload,
            editingStatus ? "Статус обновлён." : "Статус создан.", true);
    });

    transitionForm.addEventListener("submit", (event) => {
        event.preventDefault();
        rules.formError.textContent = "";
        const payload = {
            is_enabled: transitionField("is_enabled").checked,
            allow_assignee: transitionField("allow_assignee").checked,
            required_permissions: new FormData(transitionForm).getAll("required_permissions"),
        };
        if (!editingTransition) {
            payload.from_status_id = Number(from.value);
            payload.to_status_id = Number(to.value);
        }
        return mutate(rules, editingTransition?.id, editingTransition ? "PATCH" : "POST", payload,
            editingTransition ? "Переход обновлён." : "Переход создан.", true);
    });

    from.addEventListener("change", () => transitionTargets(to.value));
    catalog.add.addEventListener("click", () => openStatus(catalog.add));
    rules.add.addEventListener("click", () => openTransition(rules.add));
    catalog.retry.addEventListener("click", load);
    rules.retry.addEventListener("click", load);
    load();
}

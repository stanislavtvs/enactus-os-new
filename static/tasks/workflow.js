import {setupDialog} from "../ui/collection.js";
import {dateLabel, element, errorMessage, request, selectOptions} from "./shared.js";

const policies = {
    project: "Руководитель проекта",
    department: "Руководитель подразделения",
    department_project: "Руководитель подразделения, затем руководитель проекта",
    explicit: "Назначенный проверяющий",
};

const stageNames = {
    project: policies.project,
    department: policies.department,
    explicit: policies.explicit,
};

const distributionStates = {
    pending: "Ожидает распределения",
    assigned: "Исполнитель назначен",
    cleared: "Назначение снято",
    cancelled: "Маршрут отменён",
};

function objectName(value) {
    return typeof value === "string" ? value : value?.username ?? value?.name ?? "";
}

function webUrl(value) {
    try {
        const url = new URL(value);
        return ["http:", "https:"].includes(url.protocol) ? url.href : null;
    } catch {
        return null;
    }
}

function setField(wrapper, control, visible, required = false) {
    wrapper.hidden = !visible;
    control.disabled = !visible;
    control.required = visible && required;
}

function appendMaterials(container, submission) {
    if (submission.comment) {
        container.append(element("p", "task-review-comment", submission.comment));
    }
    if (submission.links.length) {
        const links = element("ul", "task-review-links");
        for (const value of submission.links) {
            const item = element("li", "");
            const href = webUrl(value);
            if (href) {
                const link = element("a", "", value);
                link.href = href;
                link.target = "_blank";
                link.rel = "noopener noreferrer";
                item.append(link);
            } else {
                item.textContent = value;
            }
            links.append(item);
        }
        container.append(links);
    }
}

function reviewHistoryEntry(submission) {
    const row = element("li", "task-review-version");
    const author = objectName(submission.submitted_by);
    row.append(element("strong", "", `Результат от ${dateLabel(submission.created_at)}${author ? ` · ${author}` : ""}`));
    appendMaterials(row, submission);
    const stages = element("ol", "task-review-stages");
    for (const stage of submission.stages) {
        const decision = stage.decision;
        const label = stageNames[stage.kind] ?? "Проверяющий";
        const actor = decision && objectName(decision.actor);
        const result = decision?.outcome === "approved" ? "Принято"
            : decision?.outcome === "returned" ? "Возвращено на доработку" : "Без решения";
        const item = element("li", "");
        item.append(element("p", "", [label, result, actor, decision && dateLabel(decision.created_at)].filter(Boolean).join(" · ")));
        if (decision?.comment) {
            item.append(element("p", "task-review-comment", decision.comment));
        }
        stages.append(item);
    }
    if (stages.childElementCount) {
        row.append(stages);
    }
    return row;
}

export function taskWorkflow(app, {api, token, refresh}) {
    const find = (selector) => app.querySelector(selector);
    const panel = find("#task-workflow");
    const dialogElement = find("#task-workflow-dialog");
    const dialog = setupDialog(dialogElement);
    const form = find("#task-workflow-form");
    const error = find("#task-workflow-error");
    const hint = find("#task-workflow-hint");
    const target = find("#task-workflow-target");
    const comment = find("#task-workflow-comment");
    const links = find("#task-workflow-links");
    const policy = find("#task-workflow-policy");
    const reviewer = find("#task-workflow-reviewer");
    const save = find("#task-workflow-save");
    const distributionHistory = find("#task-distribution-history");
    const reviewHistory = find("#task-review-history");
    const buttons = {
        route: find("#task-route"),
        allocate: find("#task-distribute"),
        submit: find("#task-review-submit"),
        approve: find("#task-review-approve"),
        return: find("#task-review-return"),
        policy: find("#task-review-policy-open"),
    };

    let current;
    let workflow;
    let action;
    let sending = false;

    function render(task, data) {
        current = task;
        workflow = data;
        const {distribution, review} = data;
        const distributionStatus = find("#task-distribution-status");
        distributionStatus.textContent = distribution.pending
            ? `Ожидает распределения${task.department?.name ? ` · ${task.department.name}` : ""}`
            : task.assignee ? `Основной исполнитель: ${task.assignee.username}` : "Основной исполнитель не назначен.";
        if (distribution.can_allocate && !distribution.candidates.length) {
            distributionStatus.textContent += task.project
                ? " Доступных кандидатов нет. Если сотрудник ещё не в проекте, сначала оформите участие в команде проекта."
                : " В доступной ветке сейчас нет подходящих исполнителей.";
        }

        distributionHistory.replaceChildren();
        for (const entry of distribution.history) {
            const requestedBy = objectName(entry.requested_by);
            const resolvedBy = objectName(entry.resolved_by);
            const details = [
                distributionStates[entry.status] ?? entry.status,
                entry.mode === "direct" ? "Прямое назначение" : "Через подразделение",
                objectName(entry.department),
                entry.assignee && `исполнитель: ${objectName(entry.assignee)}`,
                requestedBy && `направил: ${requestedBy}`,
                resolvedBy && `распределил: ${resolvedBy}`,
                entry.created_at && `направление: ${dateLabel(entry.created_at)}`,
                entry.resolved_at && `решение: ${dateLabel(entry.resolved_at)}`,
            ].filter(Boolean);
            distributionHistory.append(element("li", "", details.join(" · ")));
        }
        find("#task-distribution-history-panel").hidden = !distribution.history.length;
        buttons.route.hidden = !distribution.can_route;
        buttons.allocate.hidden = !distribution.can_allocate || !distribution.candidates.length;

        const effectivePolicy = !task.project && review.reviewer_id ? "explicit" : review.policy;
        find("#task-review-route").textContent = `Маршрут: ${policies[effectivePolicy] ?? "Не определён"}.`;
        const activeSubmission = review.submissions.find((submission) =>
            submission.stages.some(({id}) => id === review.current_stage_id));
        const currentStage = activeSubmission?.stages.find(({id}) => id === review.current_stage_id);
        const currentStageKind = review.current_stage_kind ?? currentStage?.kind;
        const reviewCurrent = find("#task-review-current");
        const progress = currentStage ? ` · этап ${currentStage.position} из ${activeSubmission.stages.length}` : "";
        reviewCurrent.textContent = review.current_stage_id
            ? `Сейчас проверяет: ${stageNames[currentStageKind] ?? "назначенный проверяющий"}${progress}.`
            : "";
        if (review.can_submit && review.submissions.length) {
            reviewCurrent.textContent = "Можно отправить новую версию результата.";
        }
        const materialsPanel = find("#task-review-materials");
        const materials = find("#task-review-materials-content");
        materials.replaceChildren();
        materialsPanel.hidden = !activeSubmission;
        if (activeSubmission) {
            appendMaterials(materials, activeSubmission);
            if (!activeSubmission.comment && !activeSubmission.links.length) {
                materials.append(element("p", "muted", "Исполнитель отправил задачу без комментария и ссылок."));
            }
        }
        buttons.submit.hidden = !review.can_submit;
        buttons.approve.hidden = !review.can_approve || !review.current_stage_id;
        buttons.return.hidden = !review.can_return || !review.current_stage_id;
        buttons.policy.hidden = !review.can_manage;
        reviewHistory.replaceChildren();
        for (const submission of review.submissions) {
            reviewHistory.append(reviewHistoryEntry(submission));
        }
        find("#task-review-history-panel").hidden = !review.submissions.length;
        panel.hidden = false;
    }

    function open(type) {
        if (sending || buttons[type].hidden) {
            return;
        }
        action = type;
        const {distribution, review} = workflow;
        const titles = {
            route: "Направить на распределение",
            allocate: "Назначить исполнителя",
            submit: "Отправить результат на проверку",
            approve: "Принять результат",
            return: "Вернуть результат на доработку",
            policy: "Настроить проверку",
        };
        const hints = {
            route: current.department
                ? `Поручение поступит в очередь подразделения «${current.department.name}».` : "",
            allocate: current.project ? "В списке только доступные участники проектной команды." : "",
            submit: review.submissions.length ? "Новая версия пройдёт все обязательные этапы заново." : "",
            approve: "Результат перейдёт на следующий этап или завершит задачу, если проверка последняя."
                + (current.has_unfinished_children ? " У задачи ещё есть незавершённые подзадачи." : ""),
            return: "Исполнитель увидит ваш комментарий. Следующая версия начнёт проверку заново.",
        };
        find("#task-workflow-dialog-heading").textContent = titles[type];
        hint.textContent = hints[type] ?? "";
        hint.hidden = !hint.textContent;
        error.textContent = "";
        save.textContent = type === "policy" ? "Сохранить" : titles[type];

        const hasComment = ["submit", "approve", "return"].includes(type);
        setField(find("#task-workflow-target-field"), target, type === "allocate", true);
        setField(find("#task-workflow-comment-field"), comment, hasComment, type === "return");
        setField(find("#task-workflow-links-field"), links, type === "submit");
        setField(find("#task-workflow-policy-field"), policy, type === "policy");
        setField(find("#task-workflow-reviewer-field"), reviewer, type === "policy" && !current.project);
        comment.value = "";
        links.value = "";
        find("#task-workflow-comment-label").textContent = type === "return" ? "Что нужно доработать" : "Комментарий";
        if (type === "allocate") {
            selectOptions(target, "Выберите исполнителя", distribution.candidates.map(({id, username}) => [id, username]));
        }
        if (type === "policy") {
            policy.value = review.override ?? "";
            for (const option of policy.options) {
                option.disabled = !current.project && ["project", "department_project"].includes(option.value);
            }
            const choices = review.reviewers.map(({id, username}) => [id, username]);
            if (review.reviewer_id && !choices.some(([id]) => id === review.reviewer_id)) {
                choices.unshift([review.reviewer_id, "Текущий проверяющий"]);
            }
            selectOptions(reviewer, "По маршруту проверки", choices, review.reviewer_id);
        }
        const initial = type === "allocate" ? target : type === "policy" ? policy : hasComment ? comment : save;
        dialog.open(buttons[type], initial);
    }

    function command() {
        if (action === "route") {
            return ["distribution/", "POST", {}, "Задача направлена на распределение."];
        }
        if (action === "allocate") {
            return ["distribution/", "PUT", {assignee_id: Number(target.value)}, "Исполнитель назначен."];
        }
        if (action === "policy") {
            return ["review/policy/", "PUT", {
                policy: policy.value || null,
                reviewer_id: !reviewer.disabled && reviewer.value ? Number(reviewer.value) : null,
            }, "Маршрут проверки сохранён."];
        }
        const text = comment.value.trim();
        if (action === "submit") {
            const values = links.value.split(/\r?\n/).map((value) => value.trim()).filter(Boolean);
            if (values.length > 10) {
                links.focus();
                throw new Error("Добавьте не больше 10 ссылок.");
            }
            if (values.some((value) => !webUrl(value))) {
                links.focus();
                throw new Error("Используйте только полные ссылки HTTP или HTTPS.");
            }
            return ["review/submissions/", "POST", {comment: text, links: values}, "Результат отправлен на проверку."];
        }
        if (action === "return" && !text) {
            comment.focus();
            throw new Error("Объясните, что нужно доработать.");
        }
        const approved = action === "approve";
        return ["review/decisions/", "POST", {
            stage_id: workflow.review.current_stage_id,
            outcome: approved ? "approved" : "returned",
            comment: text,
        }, approved ? "Решение о принятии сохранено." : "Результат возвращён на доработку."];
    }

    for (const [type, button] of Object.entries(buttons)) {
        button.addEventListener("click", () => open(type));
    }
    form.addEventListener("submit", async (event) => {
        event.preventDefault();
        if (sending) {
            return;
        }
        error.textContent = "";
        let change;
        try {
            change = command();
        } catch (cause) {
            error.textContent = cause.message;
            return;
        }
        const [path, method, payload, message] = change;
        sending = true;
        dialog.setBusy(true);
        try {
            await request(api + path, method, token, payload);
            dialog.close();
            await refresh(message);
        } catch (cause) {
            error.textContent = errorMessage(cause);
        } finally {
            sending = false;
            dialog.setBusy(false);
        }
    });

    return {panel, render};
}

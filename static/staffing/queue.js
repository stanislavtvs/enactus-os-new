import {pageState, request, requestRow, requestStatuses} from "./shared.js";

const app = document.querySelector("#staffing-queue-app");
const page = pageState(app);
const filters = document.querySelector("#staffing-queue-filters");
const search = document.querySelector("#staffing-search");
const statusFilter = document.querySelector("#staffing-status-filter");
const list = document.querySelector("#staffing-queue-list");
let items = [];

function render() {
    const query = search.value.trim().toLocaleLowerCase();
    let shown = 0;
    list.replaceChildren();
    for (const item of items) {
        const text = [item.project_name, item.department_name, item.specialization].join(" ").toLocaleLowerCase();
        if ((!statusFilter.value || statusFilter.value === item.status) && text.includes(query)) {
            list.append(requestRow(item, app.dataset.queueUrl));
            shown++;
        }
    }
    page.finish(shown ? `Найдено: ${shown}.` : "По этим условиям запросов нет.");
}

async function load() {
    page.start("Загружаем запросы…");
    try {
        items = await request(`${app.dataset.orgUrl}staffing-requests/`);
        const previous = statusFilter.value;
        statusFilter.replaceChildren(new Option("Все статусы", ""));
        for (const code of new Set(items.map((item) => item.status))) {
            statusFilter.add(new Option(requestStatuses[code] ?? "Статус неизвестен", code));
        }
        statusFilter.value = [...statusFilter.options].some((option) => option.value === previous) ? previous : "";
        filters.hidden = false;
        render();
    } catch (error) {
        page.fail(error);
    }
}

search.addEventListener("input", render);
statusFilter.addEventListener("change", render);
page.retry.addEventListener("click", load);
load();

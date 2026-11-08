import {element} from "../departments/directory.js";
import {civilLabel, groupByDay} from "./dates.js";
import {scheduleChanged} from "./recurrence.js";

const sourceNames = {task_deadline: "Дедлайн задачи", meeting: "Встреча", event: "Мероприятие"};
const weekdayNames = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"];

export function sourceLabel(source) {
    return sourceNames[source] ?? "Событие";
}

function sameOriginPath(value) {
    if (!value) {
        return null;
    }
    try {
        const url = new URL(value, window.location.origin);
        return url.origin === window.location.origin ? url.pathname + url.search + url.hash : null;
    } catch {
        return null;
    }
}

function entryLink(segment, openEvent, compact = false) {
    const {item, time} = segment;
    const task = item.source === "task_deadline";
    const href = task ? sameOriginPath(item.url) : null;
    const entry = element(task ? (href ? "a" : "span") : "button", "calendar-entry");
    entry.dataset.source = item.source;
    entry.dataset.status = item.status;
    if (item.occurrence_key) {
        entry.dataset.occurrenceKey = item.occurrence_key;
        entry.dataset.seriesId = item.series_id;
    }
    const category = sourceLabel(item.source);
    const status = item.status_label || "";
    const recurrence = item.occurrence_key
        ? scheduleChanged(item) ? "Расписание изменено" : "Повторяющаяся встреча"
        : "";
    entry.setAttribute("aria-label", [category, item.title, time, status, recurrence].filter(Boolean).join(", "));
    entry.title = [item.title, time, status, recurrence].filter(Boolean).join(" · ");
    if (href) {
        entry.href = href;
    }
    if (!task) {
        entry.type = "button";
        entry.setAttribute("aria-haspopup", "dialog");
        entry.addEventListener("click", () => openEvent(item, entry));
    }
    entry.append(element("span", "calendar-entry-title", item.title));
    const meta = [compact ? "" : category, time, scheduleChanged(item) ? recurrence : "", status].filter(Boolean).join(" · ");
    if (meta) {
        entry.append(element("span", "calendar-entry-meta", meta));
    }
    return entry;
}

function dateButton(day, today, selectDay, label, count = null) {
    const button = element("button", "calendar-date-button", label);
    button.type = "button";
    button.dataset.date = day;
    button.setAttribute("aria-label", `${civilLabel(day)}${count === null ? "" : `. Записей: ${count}`}`);
    if (day === today) {
        button.setAttribute("aria-current", "date");
    }
    button.addEventListener("click", () => selectDay(day, true));
    return button;
}

function renderMonth(period, groups, {today, anchor, selectDay, openEvent}) {
    const wrapper = element("section", "calendar-month");
    wrapper.setAttribute("aria-label", "Календарь на месяц");
    const headers = element("div", "calendar-month-headers");
    for (const day of weekdayNames) {
        headers.append(element("span", "", day));
    }
    wrapper.append(headers);
    const grid = element("ul", "calendar-month-grid");
    grid.setAttribute("role", "list");
    for (const day of period.days) {
        const entries = groups.get(day);
        const cell = element("li", "calendar-month-cell");
        cell.dataset.date = day;
        if (day.slice(0, 7) !== anchor.slice(0, 7)) {
            cell.classList.add("calendar-outside-month");
        }
        cell.append(dateButton(day, today, selectDay, String(Number(day.slice(8))), entries.length));
        if (entries.length) {
            const count = element("span", "calendar-mobile-count", String(entries.length));
            count.setAttribute("aria-hidden", "true");
            cell.append(count);
        }
        const list = element("div", "calendar-month-entries");
        for (const entry of entries.slice(0, 3)) {
            list.append(entryLink(entry, openEvent, true));
        }
        if (entries.length > 3) {
            const more = element("button", "calendar-more", `Ещё ${entries.length - 3}`);
            more.type = "button";
            more.setAttribute("aria-label", `Открыть день, ещё ${entries.length - 3} записей. ${civilLabel(day)}`);
            more.addEventListener("click", () => selectDay(day, true));
            list.append(more);
        }
        cell.append(list);
        grid.append(cell);
    }
    wrapper.append(grid);
    return wrapper;
}

function agendaList(entries, openEvent) {
    const list = element("ul", "calendar-agenda-list");
    list.setAttribute("role", "list");
    for (const segment of entries) {
        const row = element("li", "calendar-agenda-row");
        row.append(element("span", "calendar-agenda-time", segment.time), entryLink(segment, openEvent));
        list.append(row);
    }
    return list;
}

function renderDay(entries, openEvent) {
    const section = element("section", "calendar-agenda calendar-day");
    section.setAttribute("aria-label", "Расписание дня");
    if (!entries.length) {
        section.append(element("p", "calendar-empty", "На этот день записей нет."));
        return section;
    }
    const allDay = entries.filter(({item}) => item.all_day);
    const timed = entries.filter(({item}) => !item.all_day);
    if (allDay.length) {
        section.append(element("h3", "calendar-agenda-heading", "Весь день"), agendaList(allDay, openEvent));
    }
    if (timed.length) {
        section.append(element("h3", "calendar-agenda-heading", "По времени"), agendaList(timed, openEvent));
    }
    return section;
}

function arrangeLanes(entries) {
    const intervals = entries.map((segment) => ({
        ...segment,
        displayEnd: Math.min(1440, Math.max(segment.endMinute, segment.startMinute + 40)),
    }));
    let cluster = [];
    let ends = [];
    let clusterEnd = -1;
    for (const interval of intervals) {
        if (interval.startMinute >= clusterEnd) {
            for (const item of cluster) {
                item.lanes = ends.length;
            }
            cluster = [];
            ends = [];
            clusterEnd = -1;
        }
        const freeLane = ends.findIndex((end) => end <= interval.startMinute);
        interval.lane = freeLane < 0 ? ends.length : freeLane;
        ends[interval.lane] = interval.displayEnd;
        clusterEnd = Math.max(clusterEnd, interval.displayEnd);
        cluster.push(interval);
    }
    for (const item of cluster) {
        item.lanes = ends.length;
    }
    return intervals;
}

function renderWeek(period, groups, {today, selectDay, openEvent}) {
    const wrapper = element("section", "calendar-week");
    wrapper.setAttribute("aria-label", "Календарь на неделю");
    const desktop = element("div", "calendar-week-grid");
    desktop.append(element("span", "calendar-week-spacer"));
    for (const [index, day] of period.days.entries()) {
        const heading = element("div", "calendar-week-heading");
        heading.append(element("span", "", weekdayNames[index]), dateButton(day, today, selectDay, String(Number(day.slice(8)))));
        desktop.append(heading);
    }

    desktop.append(element("span", "calendar-all-day-label", "Весь день"));
    for (const day of period.days) {
        const lane = element("div", "calendar-all-day-lane");
        for (const entry of groups.get(day).filter(({item}) => item.all_day)) {
            lane.append(entryLink(entry, openEvent, true));
        }
        desktop.append(lane);
    }

    const byDay = period.days.map((day) => arrangeLanes(groups.get(day).filter(({item}) => !item.all_day)));
    let startHour = 8;
    let endHour = 20;
    for (const intervals of byDay) {
        for (const entry of intervals) {
            startHour = Math.min(startHour, Math.floor(entry.startMinute / 60));
            endHour = Math.max(endHour, Math.ceil(entry.displayEnd / 60));
        }
    }
    const hours = endHour - startHour;
    const minutes = hours * 60;
    desktop.style.setProperty("--calendar-hours", hours);
    const scale = element("div", "calendar-time-scale");
    for (let hour = startHour; hour < endHour; hour++) {
        scale.append(element("span", "", `${String(hour).padStart(2, "0")}:00`));
    }
    desktop.append(scale);
    for (const [index, intervals] of byDay.entries()) {
        const column = element("div", "calendar-time-column");
        column.setAttribute("aria-label", civilLabel(period.days[index]));
        for (const entry of intervals) {
            const card = entryLink(entry, openEvent, true);
            card.style.setProperty("--calendar-slot-top", `${(entry.startMinute - startHour * 60) / minutes * 100}%`);
            card.style.setProperty("--calendar-slot-height", `${(entry.displayEnd - entry.startMinute) / minutes * 100}%`);
            card.style.setProperty("--calendar-slot-left", `${entry.lane / entry.lanes * 100}%`);
            card.style.setProperty("--calendar-slot-width", `${100 / entry.lanes}%`);
            column.append(card);
        }
        desktop.append(column);
    }
    wrapper.append(desktop);

    const agenda = element("div", "calendar-week-agenda");
    for (const day of period.days) {
        const group = element("section", "calendar-agenda");
        const heading = element("h3", "calendar-agenda-heading");
        heading.append(dateButton(day, today, selectDay, civilLabel(day, {weekday: "short", day: "numeric", month: "long"})));
        group.append(heading);
        const entries = groups.get(day);
        group.append(entries.length ? agendaList(entries, openEvent) : element("p", "calendar-empty", "Нет записей."));
        agenda.append(group);
    }
    wrapper.append(agenda);
    return wrapper;
}

export function renderCalendar(view, mode, period, items, options) {
    const groups = groupByDay(items, period.days, options.zone);
    const content = mode === "month" ? renderMonth(period, groups, options)
        : mode === "week" ? renderWeek(period, groups, options)
            : renderDay(groups.get(period.days[0]), options.openEvent);
    view.replaceChildren(content);
}

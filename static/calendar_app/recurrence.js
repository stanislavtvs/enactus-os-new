import {errorMessage, request} from "../departments/directory.js";
import {setupDialog} from "../ui/collection.js";
import {civilLabel, localDay} from "./dates.js";

const frequencyLabels = {
    daily: "ежедневно",
    weekly: "еженедельно",
    monthly: "ежемесячно",
    yearly: "ежегодно",
};
const intervalLabels = {daily: "дн.", weekly: "нед.", monthly: "мес.", yearly: "г."};
const weekdays = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"];

export function occurrencePath(api, event) {
    const base = `${api}events/${event.series_id ?? event.id}/`;
    return event.occurrence_key ? `${base}occurrences/${event.occurrence_key}/` : base;
}

export function scheduleChanged(event) {
    return Boolean(event.occurrence_key && event.is_exception && event.status === "scheduled"
        && (Date.parse(event.original_starts_at) !== Date.parse(event.starts_at)
            || Date.parse(event.original_ends_at) !== Date.parse(event.ends_at)));
}

export function recurrenceLabel(rule) {
    if (!rule) {
        return "";
    }
    const frequency = rule.interval === 1 ? frequencyLabels[rule.frequency]
        : `каждые ${rule.interval} ${intervalLabels[rule.frequency]}`;
    const days = rule.weekdays?.length ? ` (${rule.weekdays.map((day) => weekdays[day - 1]).join(", ")})` : "";
    const stopped = rule.stopped_from ? ` · Остановлена с ${civilLabel(rule.stopped_from, {dateStyle: "medium"})}` : "";
    const ending = rule.until ? ` · До ${civilLabel(rule.until, {dateStyle: "medium"})}`
        : rule.count ? ` · Повторений: ${rule.count}` : "";
    return `Повторяется ${frequency}${days}${ending}${stopped}`;
}

export function setupRuleFields(container) {
    const control = (name) => container.querySelector(`[name="recurrence_${name}"]`);
    const part = (name) => container.querySelector(`[data-recurrence-part="${name}"]`);
    const frequency = control("frequency");
    const interval = control("interval");
    const end = control("end");
    const until = control("until");
    const count = control("count");
    const dayInputs = [...container.querySelectorAll('[name="recurrence_weekdays"]')];

    function update() {
        const selected = frequency.value;
        part("interval").hidden = !selected;
        part("weekdays").hidden = selected !== "weekly";
        part("end").hidden = !selected;
        part("until").hidden = !selected || end.value !== "until";
        part("count").hidden = !selected || end.value !== "count";
        part("skip").hidden = !["monthly", "yearly"].includes(selected);
        interval.disabled = !selected;
        until.disabled = part("until").hidden;
        count.disabled = part("count").hidden;
        interval.required = Boolean(selected);
        until.required = !until.disabled;
        count.required = !count.disabled;
        container.querySelector("[data-recurrence-units]").textContent = selected ? `Единица интервала: ${intervalLabels[selected]}` : "";
    }

    frequency.addEventListener("change", update);
    end.addEventListener("change", update);

    function set(rule) {
        frequency.value = rule?.frequency ?? (frequency.options[0].value || "");
        interval.value = rule?.interval ?? 1;
        end.value = rule?.until ? "until" : rule?.count ? "count" : "";
        until.value = rule?.until ?? "";
        count.value = rule?.count ?? "";
        for (const input of dayInputs) {
            input.checked = rule?.weekdays?.includes(Number(input.value)) ?? false;
        }
        update();
    }

    function value(anchor) {
        if (!frequency.value) {
            return null;
        }
        const days = frequency.value === "weekly"
            ? dayInputs.filter((input) => input.checked).map((input) => Number(input.value)) : [];
        if (frequency.value === "weekly" && anchor) {
            const firstDay = new Date(`${anchor}T00:00:00Z`).getUTCDay() || 7;
            if (days.length && !days.includes(firstDay)) {
                throw new Error("Выберите день недели первой встречи.");
            }
        }
        return {
            frequency: frequency.value, interval: Number(interval.value), weekdays: days,
            until: until.disabled ? null : until.value, count: count.disabled ? null : Number(count.value),
        };
    }

    return {set, value, frequency};
}

export function setupSeriesDialogs({app, api, token, onSaved}) {
    const get = (id) => app.querySelector(`#calendar-${id}`);
    const ruleForm = get("recurrence-form");
    const stopForm = get("series-stop-form");
    const fields = setupRuleFields(ruleForm);
    const rule = setupDialog(get("recurrence-dialog"));
    const stop = setupDialog(get("series-stop-dialog"));
    const stopDate = get("series-stop-date");
    let activeSeries, saving = false;

    function handle(form, modal, error, action) {
        form.addEventListener("submit", async (event) => {
            event.preventDefault();
            if (saving || !activeSeries) {
                return;
            }
            saving = true;
            error.textContent = "";
            modal.setBusy(true);
            try {
                await action(activeSeries);
                modal.close();
                onSaved(form === ruleForm ? "Правило серии сохранено." : "Будущие повторения остановлены.");
            } catch (requestError) {
                error.textContent = errorMessage(requestError);
            } finally {
                saving = false;
                modal.setBusy(false);
            }
        });
    }

    handle(ruleForm, rule, get("recurrence-error"), (series) => request(
        `${api}events/${series.id}/recurrence/`, "PUT", token, {recurrence: fields.value(localDay(series.starts_at, series.timezone))},
    ));
    handle(stopForm, stop, get("series-stop-error"), (series) => request(
        `${api}events/${series.id}/recurrence/stop/`, "POST", token, {from_date: stopDate.value},
    ));

    return {
        openRule(trigger, series) {
            activeSeries = series;
            fields.set(series.recurrence);
            get("recurrence-error").textContent = "";
            rule.open(trigger, fields.frequency);
        },
        openStop(trigger, series, date) {
            activeSeries = series;
            get("series-stop-error").textContent = "";
            stopDate.min = localDay(Date.now(), series.timezone);
            stopDate.value = date > stopDate.min ? date : stopDate.min;
            stop.open(trigger, stopDate);
        },
    };
}

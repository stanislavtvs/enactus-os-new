const dayMs = 86_400_000;
const formatters = new Map();
const pad = (value, length = 2) => String(value).padStart(length, "0");

function civilDate(value) {
    return new Date(`${value}T00:00:00Z`);
}

function civilKey(date) {
    return date.toISOString().slice(0, 10);
}

export function addDays(day, amount) {
    return civilKey(new Date(civilDate(day).getTime() + amount * dayMs));
}

export function validZone(zone) {
    if (!zone || zone.startsWith("+") || zone.startsWith("-")) {
        return false;
    }
    try {
        new Intl.DateTimeFormat("ru", {timeZone: zone});
        return true;
    } catch {
        return false;
    }
}

function wallParts(instant, zone) {
    if (!formatters.has(zone)) {
        formatters.set(zone, new Intl.DateTimeFormat("en-CA", {
            timeZone: zone,
            year: "numeric", month: "2-digit", day: "2-digit",
            hour: "2-digit", minute: "2-digit", second: "2-digit",
            fractionalSecondDigits: 3, hourCycle: "h23",
        }));
    }
    return Object.fromEntries(formatters.get(zone).formatToParts(new Date(instant))
        .filter(({type}) => type !== "literal").map(({type, value}) => [type, value]));
}

function wallValue(parts) {
    return `${pad(parts.year, 4)}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}:${parts.second}.${parts.fractionalSecond}`;
}

export function localDateTime(instant, zone) {
    return wallValue(wallParts(instant, zone));
}

export function localDay(instant, zone) {
    return localDateTime(instant, zone).slice(0, 10);
}

export function localTime(instant, zone) {
    return localDateTime(instant, zone).slice(11, 16);
}

export function offsetLabel(instant, zone) {
    const minutes = Math.round((Date.parse(`${localDateTime(instant, zone)}Z`) - new Date(instant).getTime()) / 60_000);
    const magnitude = Math.abs(minutes);
    return `UTC${minutes < 0 ? "−" : "+"}${pad(Math.floor(magnitude / 60))}:${pad(magnitude % 60)}`;
}

export function localCandidates(value, zone) {
    const match = /^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2})(?::(\d{2})(?:\.(\d{1,3}))?)?$/.exec(value);
    if (!match || !validZone(zone)) {
        return [];
    }
    const normalized = `${match[1]}:${match[2] ?? "00"}.${(match[3] ?? "").padEnd(3, "0")}`;
    const target = Date.parse(`${normalized}Z`);
    if (!Number.isFinite(target)) {
        return [];
    }
    // Collect offsets on both sides of possible clock changes around this wall time.
    const offsets = new Set();
    for (let hours = -36; hours <= 36; hours += 6) {
        const probe = target + hours * 3_600_000;
        const probeWall = Date.parse(`${localDateTime(probe, zone)}Z`);
        offsets.add(probeWall - probe);
    }
    const candidates = [...offsets].map((offset) => target - offset)
        .filter((instant) => localDateTime(instant, zone) === normalized)
        .sort((a, b) => a - b);
    return candidates.map((instant) => new Date(instant).toISOString());
}

export function civilBoundary(day, zone) {
    const [midnight] = localCandidates(`${day}T00:00`, zone);
    if (midnight) {
        return midnight;
    }
    // A forward clock change can omit midnight or even the entire date.
    const reference = civilDate(day).getTime();
    let low = reference - 36 * 3_600_000;
    let high = reference + 36 * 3_600_000;
    while (low + 1 < high) {
        const middle = Math.floor((low + high) / 2);
        if (localDay(middle, zone) < day) {
            low = middle;
        } else {
            high = middle;
        }
    }
    return localDay(high, zone) === day ? new Date(high).toISOString() : null;
}

export function civilLabel(day, options = {dateStyle: "full"}) {
    return new Intl.DateTimeFormat("ru", {...options, timeZone: "UTC"}).format(civilDate(day));
}

function mondayIndex(day) {
    return (civilDate(day).getUTCDay() + 6) % 7;
}

export function calendarPeriod(mode, anchor) {
    let start = anchor;
    let end;
    let label;
    if (mode === "month") {
        const first = `${anchor.slice(0, 7)}-01`;
        const next = civilDate(first);
        next.setUTCMonth(next.getUTCMonth() + 1);
        const nextFirst = civilKey(next);
        start = addDays(first, -mondayIndex(first));
        end = addDays(nextFirst, (7 - mondayIndex(nextFirst)) % 7);
        label = civilLabel(first, {month: "long", year: "numeric"});
    } else if (mode === "week") {
        start = addDays(anchor, -mondayIndex(anchor));
        end = addDays(start, 7);
        label = `${civilLabel(start, {day: "numeric", month: "short"})} — ${civilLabel(addDays(end, -1), {day: "numeric", month: "short", year: "numeric"})}`;
    } else {
        end = addDays(start, 1);
        label = civilLabel(anchor);
    }

    const days = [];
    for (let day = start; day < end; day = addDays(day, 1)) {
        days.push(day);
    }
    return {start, end, label, days};
}

export function movePeriod(mode, anchor, direction) {
    if (mode !== "month") {
        return addDays(anchor, direction * (mode === "week" ? 7 : 1));
    }
    const date = civilDate(`${anchor.slice(0, 7)}-01`);
    date.setUTCMonth(date.getUTCMonth() + direction);
    return civilKey(date);
}

function minute(time) {
    return Number(time.slice(0, 2)) * 60 + Number(time.slice(3, 5));
}

export function groupByDay(items, days, viewerZone) {
    const result = new Map(days.map((day) => [day, []]));
    for (const item of items) {
        const zone = item.all_day ? item.timezone : viewerZone;
        const start = localDateTime(item.starts_at, zone);
        const first = start.slice(0, 10);
        const startTime = start.slice(11, 16);
        const deadline = item.source === "task_deadline";
        const end = item.ends_at ? localDateTime(item.ends_at, zone) : start;
        const last = end.slice(0, 10);
        const endTime = end.slice(11, 16);
        const offsetChanged = !item.all_day && item.ends_at && first === last
            && offsetLabel(item.starts_at, zone) !== offsetLabel(item.ends_at, zone);
        const endAtMidnight = end.slice(11) === "00:00:00.000";
        for (const day of days) {
            if (deadline ? day !== first : day < first || day > last || (day === last && (item.all_day || (endAtMidnight && day !== first)))) {
                continue;
            }
            const startMinute = day === first ? minute(startTime) : 0;
            const endMinute = day === last ? minute(endTime) : 1440;
            const time = item.all_day ? "Весь день" : deadline ? startTime
                : offsetChanged ? `${startTime} (${offsetLabel(item.starts_at, zone)})–${endTime} (${offsetLabel(item.ends_at, zone)})`
                    : `${day === first ? startTime : "00:00"}–${day === last ? endTime : "24:00"}`;
            result.get(day).push({item, startMinute, endMinute, time});
        }
    }
    for (const entries of result.values()) {
        entries.sort((a, b) => Number(b.item.all_day) - Number(a.item.all_day)
            || a.startMinute - b.startMinute || a.item.title.localeCompare(b.item.title, "ru"));
    }
    return result;
}

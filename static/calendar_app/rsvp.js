import {civilLabel, localDay, localTime, offsetLabel} from "./dates.js";

const modes = {
    informational: "Информационное событие",
    optional: "Необязательный ответ",
    required: "Обязательный ответ",
};

export const answerLabels = {
    yes: "Приду",
    no: "Не приду",
    maybe: "Пока не знаю",
};

export function modeLabel(mode) {
    return modes[mode] ?? "Режим RSVP";
}

export function answerLabel(answer) {
    return answerLabels[answer] ?? "Без ответа";
}

export function formatInstant(instant, zone) {
    return `${civilLabel(localDay(instant, zone), {dateStyle: "medium"})}, ${localTime(instant, zone)} (${offsetLabel(instant, zone)})`;
}

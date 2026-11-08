const token = window.location.hash.slice(1);
const panel = document.querySelector("#invitation-link-panel");
const form = document.querySelector("#invitation-link-form");

if (token) {
    history.replaceState(history.state, "", window.location.pathname + window.location.search);
}

if (panel && form && /^[A-Za-z0-9_-]{43}$/.test(token)) {
    form.elements.namedItem("token").value = token;
    panel.hidden = false;
}

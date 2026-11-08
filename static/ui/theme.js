(() => {
    const storageKey = "enactus-os-theme";
    const themes = new Set(["system", "light", "dark"]);
    let theme = "system";

    try {
        const savedTheme = localStorage.getItem(storageKey);
        if (themes.has(savedTheme)) {
            theme = savedTheme;
        }
    } catch {
        // A blocked storage API still allows selecting a theme for this page.
    }

    document.documentElement.dataset.theme = theme;
    document.addEventListener("DOMContentLoaded", () => {
        const control = document.querySelector("[data-theme-control]");
        if (!control) {
            return;
        }

        control.value = theme;
        control.closest("label").hidden = false;
        control.addEventListener("change", () => {
            if (!themes.has(control.value)) {
                return;
            }
            document.documentElement.dataset.theme = control.value;
            try {
                localStorage.setItem(storageKey, control.value);
            } catch {
                // The current page already uses the selected theme.
            }
        });
    });
})();

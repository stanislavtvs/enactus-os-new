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
        const controls = [...document.querySelectorAll("[data-theme-control]")];
        for (const control of controls) {
            control.value = theme;
            control.closest("label").hidden = false;
            control.addEventListener("change", () => {
                if (!themes.has(control.value)) {
                    return;
                }
                theme = control.value;
                document.documentElement.dataset.theme = theme;
                for (const sibling of controls) {
                    sibling.value = theme;
                }
                try {
                    localStorage.setItem(storageKey, theme);
                } catch {
                    // The current page already uses the selected theme.
                }
            });
        }
    });
})();

(() => {
  const setDirectoryPreset = () => {
    const button = document.querySelector("[data-fill-default-dirs]");
    if (!button) return;
    button.addEventListener("click", () => {
      const values = {
        input_dir: button.dataset.inputDir,
        output_dir: button.dataset.outputDir,
        archive_dir: button.dataset.archiveDir,
        failed_dir: button.dataset.failedDir,
      };
      Object.entries(values).forEach(([id, value]) => {
        const input = document.getElementById(id);
        if (input && value) input.value = value;
      });
      const firstInput = document.getElementById("input_dir");
      if (firstInput) firstInput.focus();
    });
  };

  const syncArchiveHint = () => {
    const moveOriginals = document.getElementById("move_originals");
    const enableArchive = document.getElementById("enable_archive");
    const note = document.getElementById("archive-dependency-note");
    if (!moveOriginals || !enableArchive || !note) return;
    const update = () => {
      if (!moveOriginals.checked) {
        note.textContent = note.dataset.moveDisabledText;
      } else if (!enableArchive.checked) {
        note.textContent = note.dataset.archiveDisabledText;
      } else {
        note.textContent = note.dataset.enabledText;
      }
    };
    moveOriginals.addEventListener("change", update);
    enableArchive.addEventListener("change", update);
    update();
  };

  const enableLogFilters = () => {
    const buttons = [...document.querySelectorAll("[data-log-filter]")];
    const rows = [...document.querySelectorAll("[data-log-row]")];
    const empty = document.querySelector("[data-log-filter-empty]");
    if (!buttons.length || !rows.length) return;
    buttons.forEach((button) => {
      button.addEventListener("click", () => {
        const filter = button.dataset.logFilter;
        let visible = 0;
        rows.forEach((row) => {
          const matches = filter === "all" || row.dataset.logLevel === filter;
          row.hidden = !matches;
          if (matches) visible += 1;
        });
        buttons.forEach((item) => item.classList.toggle("active", item === button));
        if (empty) empty.hidden = visible > 0;
      });
    });
  };

  document.addEventListener("DOMContentLoaded", () => {
    setDirectoryPreset();
    syncArchiveHint();
    enableLogFilters();
  });
})();

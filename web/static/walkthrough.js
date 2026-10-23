// Guided walkthrough: one step at a time with Back / Next, dots, arrow keys and #step-N links.
// Without JavaScript every step stays visible, one under another.
(function () {
  const tour = document.getElementById("tour");
  if (!tour) return;
  const steps = Array.from(tour.querySelectorAll(".tour-step"));
  const prev = document.getElementById("tour-prev");
  const next = document.getElementById("tour-next");
  const dots = document.getElementById("tour-dots");
  const count = document.getElementById("tour-count");
  const bar = document.getElementById("tour-progress");
  let current = 0;

  tour.classList.add("js");
  document.getElementById("tour-nav").hidden = false;

  steps.forEach((step, i) => {
    const dot = document.createElement("button");
    dot.type = "button";
    dot.className = "tour-dot";
    dot.setAttribute("role", "tab");
    dot.setAttribute("aria-label", `Step ${i + 1}: ${step.dataset.title}`);
    dot.addEventListener("click", () => show(i, true));
    dots.appendChild(dot);
  });

  function show(i, focus) {
    current = Math.max(0, Math.min(steps.length - 1, i));
    steps.forEach((step, j) => { step.hidden = j !== current; });
    Array.from(dots.children).forEach((dot, j) => dot.setAttribute("aria-selected", String(j === current)));
    prev.disabled = current === 0;
    next.hidden = current === steps.length - 1;
    count.textContent = `${current + 1} of ${steps.length} · ${steps[current].dataset.title}`;
    bar.style.width = `${((current + 1) / steps.length) * 100}%`;
    history.replaceState(null, "", `#step-${current + 1}`);
    if (focus) {
      window.scrollTo({ top: 0, behavior: "smooth" });
      const heading = steps[current].querySelector("h1, h2");
      heading.tabIndex = -1;
      heading.focus({ preventScroll: true });
    }
  }

  prev.addEventListener("click", () => show(current - 1, true));
  next.addEventListener("click", () => show(current + 1, true));
  document.addEventListener("keydown", (event) => {
    if (event.target.closest("input, textarea, select")) return;
    if (event.key === "ArrowRight") show(current + 1, true);
    if (event.key === "ArrowLeft") show(current - 1, true);
  });

  const match = location.hash.match(/^#step-(\d+)$/);
  show(match ? Number(match[1]) - 1 : 0, false);
})();

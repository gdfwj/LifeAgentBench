document.addEventListener("DOMContentLoaded", () => {
  const scrollButton = document.querySelector(".scroll-to-top");

  if (scrollButton) {
    const updateScrollButton = () => {
      scrollButton.classList.toggle("visible", window.scrollY > 500);
    };

    updateScrollButton();
    window.addEventListener("scroll", updateScrollButton, { passive: true });
    scrollButton.addEventListener("click", () => {
      const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      window.scrollTo({ top: 0, behavior: reduceMotion ? "auto" : "smooth" });
    });
  }

  document.querySelectorAll("[data-copy-target]").forEach((button) => {
    button.addEventListener("click", async () => {
      const target = document.getElementById(button.dataset.copyTarget);
      if (!target) return;

      const originalLabel = button.textContent;
      try {
        await navigator.clipboard.writeText(target.textContent.trim());
        button.textContent = "Copied";
      } catch (error) {
        const range = document.createRange();
        const selection = window.getSelection();
        range.selectNodeContents(target);
        selection.removeAllRanges();
        selection.addRange(range);
        button.textContent = "Selected";
      }

      window.setTimeout(() => {
        button.textContent = originalLabel;
      }, 1800);
    });
  });
});

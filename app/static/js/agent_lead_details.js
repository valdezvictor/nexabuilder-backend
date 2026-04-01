document.addEventListener("DOMContentLoaded", function () {
  const btn = document.getElementById("btn-regenerate-routing-summary");
  if (!btn) return;

  btn.addEventListener("click", async function () {
    const leadId = this.getAttribute("data-lead-id");
    const container = document.getElementById("ai-routing-summary-container");
    const textEl = document.getElementById("ai-routing-summary-text");

    if (!leadId || !container || !textEl) return;

    const originalHtml = container.innerHTML;
    container.innerHTML = "<p class='text-muted'>Regenerating summary...</p>";

    try {
      const resp = await fetch(`/api/ai/routing-summary?lead_id=${leadId}`, {
        method: "POST",
      });
      if (!resp.ok) throw new Error("Failed to regenerate summary");

      const data = await resp.json();

      let html = "";
      html += `<p id="ai-routing-summary-text">${data.summary_text}</p>`;

      if (data.risks && data.risks.length > 0) {
        html += "<h6 class='mt-3'>Risks</h6><ul>";
        for (const r of data.risks) {
          html += `<li>${r}</li>`;
        }
        html += "</ul>";
      }

      if (data.opportunities && data.opportunities.length > 0) {
        html += "<h6 class='mt-3'>Opportunities</h6><ul>";
        for (const o of data.opportunities) {
          html += `<li>${o}</li>`;
        }
        html += "</ul>";
      }

      container.innerHTML = html;
    } catch (e) {
      container.innerHTML = originalHtml;
      console.error(e);
    }
    container.classList.add("ai-fade-out");

    setTimeout(() => {
      container.innerHTML = "<div class='ai-loading'></div><div class='ai-loading'></div>";
      container.classList.remove("ai-fade-out");
      container.classList.add("ai-fade-in");
    }, 200);

    btn.classList.add("btn-refresh-spin");
    setTimeout(() => btn.classList.remove("btn-refresh-spin"), 600);

    container.classList.add("ai-fade-out");

    setTimeout(() => {
      container.innerHTML = "<div class='ai-loading'></div><div class='ai-loading'></div>";
      container.classList.remove("ai-fade-out");
      container.classList.add("ai-fade-in");
    }, 200);

    btn.classList.add("btn-refresh-spin");
    setTimeout(() => btn.classList.remove("btn-refresh-spin"), 600);

    // ⭐ Score count-up animation
    function animateScoreValue(el, finalValue) {
      let current = 0;
      const duration = 900;
      const step = finalValue / (duration / 16);

      const interval = setInterval(() => {
        current += step;
        if (current >= finalValue) {
          current = finalValue;
          clearInterval(interval);
        }
        el.textContent = Math.round(current);
      }, 16);
    }

    document.addEventListener("DOMContentLoaded", function () {
      const scoreEl = document.getElementById("lead-score-value");
      if (scoreEl) {
        const finalValue = parseInt(scoreEl.getAttribute("data-score"), 10);
        animateScoreValue(scoreEl, finalValue);
      }
    });

    document.getElementById("clear-filters").addEventListener("click", function () {
        document.getElementById("filter-status").value = "";
        document.getElementById("filter-vertical").value = "";

        table.column(7).search("");
        table.column(3).search("");
        table.draw();
    });
  });
});

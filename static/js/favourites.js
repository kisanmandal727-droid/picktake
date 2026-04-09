// favourites.js
// Uses capture phase (true) so card's onclick stopPropagation doesn't block us.

document.addEventListener("click", async (e) => {
  const icon = e.target.closest(".fav-icon");
  if (!icon) return;

  // Stop the card's navigation onclick from firing
  e.preventDefault();
  e.stopPropagation();
  e.stopImmediatePropagation();

  const card = icon.closest(".card");
  if (!card) return;
  const id = card.dataset.id;
  if (!id) return;

  // Immediate visual feedback
  const isNowActive = !icon.classList.contains("active");
  icon.classList.toggle("active");

  try {
    const res = await fetch("/api/favourites/toggle", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id })
    });

    if (!res.ok) {
      // Not logged in or error - revert
      icon.classList.toggle("active");
      if (res.status === 401) {
        alert("Please log in to save listings.");
      }
      return;
    }

    const data = await res.json();

    // Remove from saved grid if unfavourited there
    if (data.action === "removed") {
      const savedGrid = document.getElementById("saved-grid");
      if (savedGrid && card.closest("#saved-grid")) {
        card.remove();
        if (savedGrid.querySelectorAll(".card").length === 0) {
          const savedPanel = document.getElementById("saved-panel");
          if (savedPanel) savedPanel.innerHTML =
            `<div class="empty-state"><i class="fa fa-heart"></i><p>No saved listings yet.</p></div>`;
        }
      }
    }

  } catch (err) {
    icon.classList.toggle("active"); // revert on network error
    console.error("Favourite toggle failed:", err);
  }

}, true); // <-- CAPTURE PHASE: fires before any element's own onclick

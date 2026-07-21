/* meerail address autocomplete: attaches to a comma-separated recipients input
   and suggests contacts from /api/contacts as you type the current token. */

App.autocomplete = (function () {
  function attach(input) {
    let box = null;
    let items = [];
    let active = -1;
    let timer = null;

    function currentToken() {
      const v = input.value;
      const i = v.lastIndexOf(",");
      return v.slice(i + 1).trim();
    }

    function replaceToken(address) {
      const v = input.value;
      const i = v.lastIndexOf(",");
      const prefix = i >= 0 ? v.slice(0, i + 1) + " " : "";
      input.value = prefix + address + ", ";
    }

    function close() {
      if (box) { box.remove(); box = null; }
      items = [];
      active = -1;
    }

    function render() {
      if (!box) {
        box = document.createElement("div");
        box.className = "ac-box";
        document.body.appendChild(box);
      }
      const r = input.getBoundingClientRect();
      box.style.left = r.left + "px";
      box.style.top = r.bottom + 2 + "px";
      box.style.width = r.width + "px";
      box.innerHTML = items.map((c, i) =>
        `<div class="ac-item ${i === active ? "active" : ""}" data-i="${i}">
          <span class="ac-name">${App.esc(c.name || c.address)}</span>
          <span class="ac-addr">${App.esc(c.address)}</span>
        </div>`).join("");
      box.querySelectorAll(".ac-item").forEach((el) => {
        el.addEventListener("mousedown", (e) => { e.preventDefault(); accept(Number(el.dataset.i)); });
      });
    }

    function accept(i) {
      if (items[i]) replaceToken(items[i].address);
      close();
      input.focus();
    }

    async function run() {
      const token = currentToken();
      if (token.length < 1) return close();
      let res = [];
      try { res = await App.api.contacts(token); } catch (_) { res = []; }
      // The token may have changed while awaiting.
      if (currentToken().length < 1) return close();
      if (!res.length) return close();
      items = res;
      active = 0;
      render();
    }

    input.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(run, 130); });
    input.addEventListener("keydown", (e) => {
      if (!box || !items.length) return;
      if (e.key === "ArrowDown") { e.preventDefault(); active = (active + 1) % items.length; render(); }
      else if (e.key === "ArrowUp") { e.preventDefault(); active = (active - 1 + items.length) % items.length; render(); }
      else if (e.key === "Enter" || e.key === "Tab") {
        if (active >= 0) { e.preventDefault(); accept(active); }
      } else if (e.key === "Escape") { close(); }
    });
    input.addEventListener("blur", () => setTimeout(close, 150));
  }

  return { attach };
})();

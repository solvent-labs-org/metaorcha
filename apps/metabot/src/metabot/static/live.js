/* A run's live page: redrawn from the server each time the run changes.
   The server streams one event per change on /live/<id>/events; on each,
   the page fetches its main part again and swaps it in. The section that
   waits for you is left alone while you have a field focused or typed in,
   so an answer under way is never wiped. With scripts off, the page's
   noscript refresh does the same, every two seconds. */
(function () {
  "use strict";
  var live = document.getElementById("live");
  if (!live || !window.EventSource || !window.fetch) return;
  var path = location.pathname;
  var busy = false;
  var again = false;

  function typing(section) {
    var active = document.activeElement;
    if (active && section.contains(active)) return true;
    var fields = section.querySelectorAll("input:not([type=hidden]), textarea");
    for (var i = 0; i < fields.length; i++) {
      if (fields[i].value) return true;
    }
    return false;
  }

  function swap(html) {
    var box = document.createElement("div");
    box.innerHTML = html;
    var fresh = box.firstElementChild;
    if (!fresh || fresh.id !== "live") return;
    ["head", "asks", "rest"].forEach(function (id) {
      var now = document.getElementById(id);
      var next = fresh.querySelector("#" + id);
      if (!now || !next) return;
      if (id === "asks" && typing(now)) return;
      now.replaceWith(next);
    });
  }

  function redraw() {
    if (busy) { again = true; return; }
    busy = true;
    fetch(path + "?part=main", { credentials: "same-origin", cache: "no-store" })
      .then(function (response) { return response.ok ? response.text() : ""; })
      .then(function (html) { if (html) swap(html); })
      .catch(function () {})
      .then(function () {
        busy = false;
        if (again) { again = false; redraw(); }
      });
  }

  var source = new EventSource(path + "/events");
  source.onmessage = function () { redraw(); };
  source.addEventListener("end", function () {
    source.close();
    redraw();
  });
})();

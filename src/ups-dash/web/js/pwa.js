/* PWA registration and auto-update.

   THE PROBLEM THIS SOLVES: a service worker, once installed, keeps serving
   whatever it cached until something makes the browser fetch a new sw.js.
   Deploying new files is not enough on its own -- installed clients happily
   run the old build forever. That is the classic PWA failure, and for a
   power dashboard "silently running last week's code" is not acceptable.

   THREE MECHANISMS, deliberately overlapping:

     1. sw.js is network-first for the app shell, so page content is fresh on
        every online load regardless of anything here.
     2. registration.update() runs on load, whenever the tab becomes visible
        again, and every 15 minutes -- so a long-lived tab on a wall display
        still picks up new builds.
     3. When a new worker takes control, reload once so it actually applies.

   The reload is guarded by a flag: controllerchange fires on the first
   install too, and reloading unconditionally there is a well-known way to
   produce an infinite refresh loop. */

const UPDATE_EVERY_MS = 15 * 60 * 1000;

let reloading = false;

function watchForNewWorker(reg) {
  reg.addEventListener("updatefound", () => {
    const incoming = reg.installing;
    if (!incoming) return;
    incoming.addEventListener("statechange", () => {
      // A worker reaching "installed" while one is already in control means
      // this is an update rather than a first install.
      if (incoming.state === "installed" && navigator.serviceWorker.controller) {
        try {
          incoming.postMessage("skip-waiting");
        } catch (err) {
          /* the worker will take over on the next navigation anyway */
        }
      }
    });
  });
}

function scheduleUpdateChecks(reg) {
  const check = () => {
    try {
      reg.update();
    } catch (err) {
      /* offline, or the server is unreachable -- try again next time */
    }
  };
  setInterval(check, UPDATE_EVERY_MS);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") check();
  });
  window.addEventListener("online", check);
}

export function registerServiceWorker() {
  if (!("serviceWorker" in navigator)) return;

  navigator.serviceWorker.addEventListener("controllerchange", () => {
    // Guard: this also fires on the very first install, and an unguarded
    // reload here is the standard way to create a refresh loop.
    if (reloading) return;
    reloading = true;
    window.location.reload();
  });

  window.addEventListener("load", () => {
    navigator.serviceWorker.register("sw.js").then((reg) => {
      watchForNewWorker(reg);
      scheduleUpdateChecks(reg);
      reg.update().catch(() => null);
    }).catch((err) => {
      console.warn("[pwa] service worker registration failed:", err);
    });
  });
}

/* Installability is not something the page can force -- the browser decides.
   Capturing the event lets the app offer its own install affordance instead
   of relying on a menu item people never find.

   ⚠ It only fires in a SECURE CONTEXT. Over plain http:// (for example a plain
   LAN address such as http://10.0.0.10:8088) the browser will show an install
   menu entry and then refuse, which reads as "not installable". Use the
   HTTPS hostname. */
export function watchInstallability(onAvailable) {
  let deferred = null;
  window.addEventListener("beforeinstallprompt", (event) => {
    event.preventDefault();
    deferred = event;
    if (typeof onAvailable === "function") {
      onAvailable(async () => {
        if (!deferred) return null;
        deferred.prompt();
        const choice = await deferred.userChoice;
        deferred = null;
        return choice && choice.outcome;
      });
    }
  });
}

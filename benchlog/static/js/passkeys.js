// Browser-side WebAuthn helpers. Uses the JSON-format APIs that ship in
// modern Chrome/Firefox/Safari. No external deps.

function csrfToken() {
  return document.querySelector('meta[name="csrf-token"]')?.content || '';
}

function flash(message, isError) {
  const el = document.getElementById("passkey-status");
  if (!el) {
    if (isError) alert(message);
    return;
  }
  el.textContent = message;
  el.className = isError ? "card mb-4 border-rust text-rust-deep text-sm" : "card mb-4 text-sm";
  el.style.display = "block";
}

function ensureNameModal() {
  if (document.getElementById("passkey-name-modal")) return;
  const dialog = document.createElement("dialog");
  dialog.id = "passkey-name-modal";
  dialog.className = "modal";
  dialog.setAttribute("aria-labelledby", "passkey-name-modal-title");
  dialog.innerHTML = `
    <form method="dialog" data-passkey-form>
      <h3 id="passkey-name-modal-title" class="text-lg" style="margin-bottom:0.25rem;">Name this passkey</h3>
      <p class="meta" style="margin-bottom:1rem;">Something memorable — e.g. "iPhone", "YubiKey"</p>
      <input id="passkey-name-input" type="text" maxlength="128" />
      <div style="display:flex;justify-content:flex-end;gap:0.5rem;margin-top:1rem;">
        <button type="button" class="btn-ghost" data-passkey-cancel>Cancel</button>
        <button type="submit" class="btn-primary" data-passkey-save>Save</button>
      </div>
    </form>`;
  document.body.appendChild(dialog);
}

function promptPasskeyName(defaultValue = "Passkey") {
  return new Promise((resolve) => {
    ensureNameModal();
    const dialog = document.getElementById("passkey-name-modal");
    const form = dialog.querySelector("[data-passkey-form]");
    const input = dialog.querySelector("#passkey-name-input");
    const cancelBtn = dialog.querySelector("[data-passkey-cancel]");

    input.value = defaultValue;

    let result = null;
    const onSubmit = (e) => {
      e.preventDefault();
      result = (input.value || "").trim() || defaultValue;
      dialog.close();
    };
    const onCancel = () => {
      result = null;
      dialog.close();
    };
    const onClose = () => {
      form.removeEventListener("submit", onSubmit);
      cancelBtn.removeEventListener("click", onCancel);
      dialog.removeEventListener("close", onClose);
      dialog.removeEventListener("click", onBackdropClick);
      resolve(result);
    };
    const onBackdropClick = (e) => {
      if (e.target === dialog) onCancel();
    };

    form.addEventListener("submit", onSubmit);
    cancelBtn.addEventListener("click", onCancel);
    dialog.addEventListener("close", onClose);
    dialog.addEventListener("click", onBackdropClick);

    dialog.showModal();
    setTimeout(() => {
      input.focus();
      input.select();
    }, 0);
  });
}

async function passkeyRegister() {
  if (!window.PublicKeyCredential) {
    flash("Your browser doesn't support passkeys.", true);
    return;
  }
  const friendlyName = await promptPasskeyName("Passkey");
  if (friendlyName === null) return;

  let optionsJSON;
  try {
    const r = await fetch("/account/passkeys/register/start", {
      method: "POST",
      headers: { "X-CSRF-Token": csrfToken() },
    });
    if (!r.ok) throw new Error(await r.text());
    optionsJSON = await r.json();
  } catch (e) {
    flash("Could not start registration: " + e.message, true);
    return;
  }

  let credential;
  try {
    credential = await navigator.credentials.create({
      publicKey: PublicKeyCredential.parseCreationOptionsFromJSON(optionsJSON),
    });
  } catch (e) {
    flash("Registration cancelled or failed.", true);
    return;
  }

  const payload = credential.toJSON();
  payload.friendly_name = friendlyName;
  try {
    const r = await fetch("/account/passkeys/register/finish", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken() },
      body: JSON.stringify(payload),
    });
    if (!r.ok) throw new Error(await r.text());
    location.reload();
  } catch (e) {
    flash("Server rejected the passkey: " + e.message, true);
  }
}

async function passkeySignIn() {
  if (!window.PublicKeyCredential) {
    flash("Your browser doesn't support passkeys.", true);
    return;
  }

  let optionsJSON;
  try {
    const r = await fetch("/auth/passkey/start", {
      method: "POST",
      headers: { "X-CSRF-Token": csrfToken() },
    });
    if (!r.ok) throw new Error(await r.text());
    optionsJSON = await r.json();
  } catch (e) {
    flash("Could not start sign-in: " + e.message, true);
    return;
  }

  let assertion;
  try {
    assertion = await navigator.credentials.get({
      publicKey: PublicKeyCredential.parseRequestOptionsFromJSON(optionsJSON),
      mediation: "optional",
    });
  } catch (e) {
    flash("Sign-in cancelled.", true);
    return;
  }

  try {
    const r = await fetch("/auth/passkey/finish", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken() },
      body: JSON.stringify(assertion.toJSON()),
    });
    if (!r.ok) throw new Error(await r.text());
    const body = await r.json();
    location.href = body.redirect || "/";
  } catch (e) {
    flash("Sign-in failed: " + e.message, true);
  }
}

async function signupWithPasskey() {
  if (!window.PublicKeyCredential) {
    flash("Your browser doesn't support passkeys.", true);
    return;
  }
  const form = document.getElementById("signup-form");
  if (!form) return;
  const email = form.email.value.trim();
  const username = form.username.value.trim();
  const displayName = form.display_name.value.trim();
  if (!email || !username || !displayName) {
    flash("Fill in email, username, and display name first.", true);
    return;
  }

  const friendlyName = await promptPasskeyName("Passkey");
  if (friendlyName === null) return;

  const startBody = new URLSearchParams();
  startBody.append("email", email);
  startBody.append("username", username);
  startBody.append("display_name", displayName);

  let optionsJSON;
  try {
    const r = await fetch("/signup/passkey/start", {
      method: "POST",
      headers: {
        "Content-Type": "application/x-www-form-urlencoded",
        "X-CSRF-Token": csrfToken(),
      },
      body: startBody,
    });
    if (!r.ok) throw new Error(await r.text());
    optionsJSON = await r.json();
  } catch (e) {
    flash("Could not start passkey signup: " + e.message, true);
    return;
  }

  let credential;
  try {
    credential = await navigator.credentials.create({
      publicKey: PublicKeyCredential.parseCreationOptionsFromJSON(optionsJSON),
    });
  } catch (e) {
    flash("Passkey creation cancelled or failed.", true);
    return;
  }

  const payload = credential.toJSON();
  payload.friendly_name = friendlyName;

  try {
    const r = await fetch("/signup/passkey/finish", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken() },
      body: JSON.stringify(payload),
    });
    if (!r.ok) throw new Error(await r.text());
    const body = await r.json();
    location.href = body.redirect || "/";
  } catch (e) {
    flash("Signup failed: " + e.message, true);
  }
}

document.addEventListener("DOMContentLoaded", () => {
  document.querySelector("[data-passkey-signin]")?.addEventListener("click", passkeySignIn);
  document.querySelector("[data-passkey-register]")?.addEventListener("click", passkeyRegister);
  document.querySelector("[data-passkey-signup]")?.addEventListener("click", signupWithPasskey);
});

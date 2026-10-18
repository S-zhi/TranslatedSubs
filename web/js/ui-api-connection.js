import { Api, USE_MOCK } from "./api.js";
import { getApiToken, setApiToken } from "./api-auth.js";
import { setView } from "./store.js";

export function initApiConnection({ root = document.querySelector("#apiConnection"), base = Api.base, reload = () => window.location.reload(), showSettings = () => setView("other-settings") } = {}) {
  if (!root) return;
  const form = root.querySelector("form");
  const input = root.querySelector("#apiConnectionToken");
  const feedback = root.querySelector("#apiConnectionFeedback");
  root.querySelector("#apiConnectionBase").textContent = base || "当前站点";
  input.value = getApiToken(base);
  let prompted = false;
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    setApiToken(base, input.value);
    reload();
  });
  root.querySelector("#apiConnectionClear").addEventListener("click", () => {
    setApiToken(base, "");
    input.value = "";
    reload();
  });
  document.addEventListener("subtrans:api-auth-required", () => {
    if (prompted || USE_MOCK) return;
    prompted = true;
    showSettings();
    root.open = true;
    feedback.textContent = "后端要求有效的 API Token，请填写后保存。";
    input.focus();
  });
}

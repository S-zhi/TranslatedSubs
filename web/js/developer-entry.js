/* 隐藏开发者入口：品牌图标五击后显示过渡动效并进入独立页面。 */

export function createClickGate({
  requiredClicks = 5,
  windowMs = 1500,
  now = () => Date.now(),
  onTrigger = () => {},
} = {}) {
  let count = 0;
  let startedAt = null;

  return function registerClick() {
    const timestamp = now();
    if (startedAt === null || timestamp - startedAt > windowMs) {
      count = 0;
      startedAt = timestamp;
    }
    count += 1;
    if (count < requiredClicks) return false;
    count = 0;
    startedAt = null;
    onTrigger();
    return true;
  };
}

function showTransition() {
  if (document.querySelector(".developer-transition")) return;
  const overlay = document.createElement("div");
  overlay.className = "developer-transition";
  overlay.setAttribute("role", "status");
  overlay.setAttribute("aria-live", "polite");
  overlay.innerHTML = `
    <div class="developer-transition__card">
      <span class="developer-loader" aria-hidden="true"><i></i><i></i><i></i></span>
      <span>正在打开开发者页面</span>
    </div>
  `;
  document.body.append(overlay);
  window.setTimeout(() => {
    window.location.assign("/developer.html");
  }, 560);
}

export function initDeveloperEntry({ root = document } = {}) {
  const mark = root.querySelector(".brand__mark");
  if (!mark) return () => {};

  const registerClick = createClickGate({ onTrigger: showTransition });
  const onClick = () => registerClick();
  const onKeydown = (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      registerClick();
    }
  };
  mark.addEventListener("click", onClick);
  mark.addEventListener("keydown", onKeydown);
  mark.setAttribute("role", "button");
  mark.setAttribute("tabindex", "0");
  mark.setAttribute("aria-label", "TranslatedSubs，连续点击五次打开开发者页面");

  return () => {
    mark.removeEventListener("click", onClick);
    mark.removeEventListener("keydown", onKeydown);
  };
}

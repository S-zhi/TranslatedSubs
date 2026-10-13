/* 其他设置的二级标签页。使用 data 映射，方便未来继续扩展更多设置页。 */

import { $, $$ } from "./utils.js";
import { state, subscribe, setSettingsTab } from "./store.js";

const SETTINGS_TABS = new Set(["engines", "audio", "probe", "drive"]);

function selectTab(tabName, tabs, panels, syncState = true) {
  if (!SETTINGS_TABS.has(tabName)) tabName = "engines";
  tabs.forEach((tab) => {
    const active = tab.dataset.settingsTab === tabName;
    tab.classList.toggle("is-active", active);
    tab.setAttribute("aria-selected", String(active));
    tab.tabIndex = active ? 0 : -1;
  });
  panels.forEach((panel) => {
    panel.hidden = panel.dataset.settingsPanel !== tabName;
  });
  if (syncState && state.settingsTab !== tabName) setSettingsTab(tabName);
}

export function initAdvancedSettings() {
  const root = $("#advancedSettings");
  if (!root) return;
  const tabs = $$('[data-settings-tab]', root);
  const panels = $$('[data-settings-panel]', root);
  if (!tabs.length || !panels.length) return;

  const firstTab = tabs[0];
  const initial = SETTINGS_TABS.has(state.settingsTab) ? state.settingsTab : firstTab.dataset.settingsTab;
  selectTab(initial, tabs, panels, false);

  // 路由初始化可能先于本模块设置 settingsTab，深链接需要立即选中对应面板。
  subscribe(({ type }) => {
    if (type === "settings-tab") selectTab(state.settingsTab, tabs, panels, false);
  });

  // 让旧链接、浏览器前进后退以及其他模块触发的状态变更都能切换面板。
  document.addEventListener("viewchange", (event) => {
    if (event.detail?.view !== "other-settings") return;
    selectTab(event.detail.settingsTab, tabs, panels, false);
  });

  tabs.forEach((tab, index) => {
    tab.addEventListener("click", () => selectTab(tab.dataset.settingsTab, tabs, panels));
    tab.addEventListener("keydown", (event) => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      const nextIndex = event.key === "Home"
        ? 0
        : event.key === "End"
          ? tabs.length - 1
          : (index + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
      tabs[nextIndex].focus();
      selectTab(tabs[nextIndex].dataset.settingsTab, tabs, panels);
    });
  });
}

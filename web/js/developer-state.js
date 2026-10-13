/* 开发者页状态工厂：每次页面加载都创建新的内存对象。 */

export function createDeveloperState(logs = []) {
  return {
    logs: logs.map((item) => ({ ...item })),
    level: "all",
  };
}

export function visibleLogs(state) {
  return state.level === "all"
    ? state.logs
    : state.logs.filter((item) => item.level === state.level);
}

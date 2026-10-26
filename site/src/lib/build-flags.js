// Vite 注入构建期字面量；默认开启，SUBTRANS_ENABLE_CLOUD_TRIAL=0 关闭。
// 直接导出以便移除关闭分支中的试用 JS；此模块需经 Vite 处理。
export const CLOUD_TRIAL_ENABLED = __CLOUD_TRIAL_ENABLED__;

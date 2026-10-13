import { test } from "node:test";
import assert from "node:assert/strict";

globalThis.window = {
  APP_CONFIG: {
    USE_MOCK: false,
    API_BASE_URL: "http://localhost:8000",
    API_TIMEOUT_MS: 15000,
  },
};

const { downgradeReasonText } = await import("../js/ui-queue.js");

test("downgradeReasonText formats USER_CLEANED without errno", () => {
  assert.equal(downgradeReasonText("USER_CLEANED"), "资源已被用户或系统清理");
});

test("downgradeReasonText formats DISK_FAILURE with errno", () => {
  assert.equal(downgradeReasonText("DISK_FAILURE", 5), "磁盘故障或读写错误 (errno: 5)");
});

test("downgradeReasonText formats VOLUME_MIGRATED", () => {
  assert.equal(downgradeReasonText("VOLUME_MIGRATED"), "存储卷已迁移或卸载");
});

test("downgradeReasonText formats UNKNOWN with errno", () => {
  assert.equal(downgradeReasonText("UNKNOWN", 13), "存储资源丢失 (errno: 13)");
});

test("downgradeReasonText defaults to 存储资源丢失 when reason is null", () => {
  assert.equal(downgradeReasonText(null), "存储资源丢失");
});

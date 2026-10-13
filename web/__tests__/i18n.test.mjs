import { test } from "node:test";
import assert from "node:assert/strict";
import { normalizeLocale, preferredLocale, translate, UI_LOCALES } from "../js/i18n.js";
import { LOCALIZED_TEXT, LOCALIZED_TEMPLATES } from "../js/locales.js";

test("the requested interface locales have bundled translations", () => {
  assert.deepEqual(Object.keys(UI_LOCALES), ["zh-CN", "en", "hi", "es", "ar", "fr", "pt", "ru"]);
  assert.deepEqual(Object.keys(LOCALIZED_TEXT), ["en", "hi", "es", "ar", "fr", "pt", "ru"]);
  assert.deepEqual(Object.keys(LOCALIZED_TEMPLATES), ["en", "hi", "es", "ar", "fr", "pt", "ru"]);
  for (const locale of Object.keys(UI_LOCALES).filter((item) => item !== "zh-CN")) {
    assert.deepEqual(Object.keys(LOCALIZED_TEXT[locale]).sort(), Object.keys(LOCALIZED_TEXT.en).sort(), locale);
    assert.notEqual(translate("开始处理", locale), "开始处理", locale);
    assert.notEqual(translate("目标语言", locale), "目标语言", locale);
    for (const [source, target] of Object.entries(LOCALIZED_TEMPLATES[locale])) {
      assert.deepEqual(source.match(/⟦\d+⟧/g)?.sort(), target.match(/⟦\d+⟧/g)?.sort(), locale);
    }
  }
});

test("model guidance has complete translations in every supported locale", () => {
  const modelGuidance = [
    "本地模型优先使用 CPU；通过下方模型库管理本机模型；仅已就绪的模型可以设为当前模型。",
    "由模型设置统一管理 Whisper",
    "启用后新任务使用当前模型；关闭后可在任务页选择已就绪模型",
    "当前模型缺失或未就绪，请选择一个已就绪模型",
  ];
  for (const locale of Object.keys(UI_LOCALES).filter((item) => item !== "zh-CN")) {
    for (const source of modelGuidance) {
      const target = LOCALIZED_TEXT[locale][source];
      assert.ok(target?.trim(), `${locale}: ${source}`);
      assert.notEqual(target, source, `${locale}: ${source}`);
      assert.equal(translate(source, locale), target, `${locale}: ${source}`);
    }
  }
});

test("a saved locale takes precedence over deployment and browser defaults", () => {
  assert.equal(preferredLocale({ saved: "ur", configured: "es", languages: ["fr-FR"] }), "es");
  assert.equal(preferredLocale({ saved: "bn", configured: "auto", languages: ["fr-FR"] }), "fr");
  assert.equal(preferredLocale({ saved: "invalid", configured: "pt", languages: ["en-US"] }), "pt");
  assert.equal(preferredLocale({ configured: "auto", languages: ["ar-EG"] }), "ar");
  assert.equal(normalizeLocale("zh-Hans-CN"), "zh-CN");
});

test("dynamic task labels localize without changing task values", () => {
  assert.equal(translate("英语 → 简体中文", "en"), "English → Chinese (China)");
  assert.equal(translate("已选 3 项", "en"), "3 items selected");
  assert.equal(translate("任务 · TranslatedSubs", "en"), "Task · TranslatedSubs");
  assert.equal(translate("开始处理", "zh-CN"), "开始处理");
});

test("localized guidance keeps subtitle filenames exact", () => {
  const guidance = "先在「任务」里跑一次完整流水线，生成 original.srt / translated.srt 后再回来。";
  for (const locale of Object.keys(UI_LOCALES).filter((item) => item !== "zh-CN")) {
    assert.ok(translate(guidance, locale).includes("original.srt"), locale);
    assert.ok(translate(guidance, locale).includes("translated.srt"), locale);
  }
});

---
version: alpha
name: TranslatedSubs — Open-inspired dark frame
description: >
  Custom dark adaptation of the blue-professional preset's restrained hierarchy,
  asymmetric composition, soft panels, and clear CTA. Its original light canvas,
  single-cobalt palette, gray text ladder, and one-accent rule are replaced to honor
  the selected Open-inspired dark, multicolor direction.
unit: the frame — 1920x1080 primary
principle: product truth first; one focal action per shot; color follows the workflow
source_preset: blue-professional (custom adaptation; not an unchanged preset)

colors:
  bg: "#080B17"
  text: "#F5F7FF"
  cyan: "#51E6F1"
  violet: "#7C5CFC"
  coral: "#FF927A"
  text-muted: "rgba(245,247,255,0.72)"
  text-soft: "rgba(245,247,255,0.52)"
  panel: "rgba(245,247,255,0.055)"
  panel-strong: "rgba(245,247,255,0.095)"
  border: "rgba(245,247,255,0.17)"
  cyan-glow: "rgba(81,230,241,0.23)"
  violet-glow: "rgba(124,92,252,0.25)"
  coral-glow: "rgba(255,146,122,0.19)"

typography:
  family: Sora
  regular: 400
  bold: 700
  display: { fontFamily: Sora, weight: 700, color: text, tracking: "-0.035em" }
  body: { fontFamily: Sora, weight: 400, color: text-muted, lineHeight: 1.45 }
  label: { fontFamily: Sora, weight: 700, color: cyan, tracking: "0.10em", upper: true }

fonts:
  regular: assets/fonts/sora-400.woff2
  bold: assets/fonts/sora-700.woff2

spacing:
  safe-x: "5cqw"
  safe-y: "6cqh"
  content-gap: "2cqw"

radii:
  ui-panel: "16px"
  ui-control: "10px"
  pill: "100px"

components:
  product-window:
    backgroundColor: "{colors.panel}"
    border: "2px solid {colors.border}"
    rounded: "{radii.ui-panel}"
    description: "One large, legible illustrated workbench surface; subtle localized color only."
  action-control:
    backgroundColor: "{colors.cyan}"
    textColor: "{colors.bg}"
    rounded: "{radii.ui-control}"
    description: "A single clear primary action in a scene; no decorative button grid."
  subtitle-rail:
    backgroundColor: "{colors.panel-strong}"
    activeColor: "{colors.violet}"
    editColor: "{colors.coral}"
    description: "Timed text rows and selected state in the interface illustration."
  progress-line:
    backgroundColor: "{colors.cyan}"
    description: "Only for known workflow stages; never imply unverified speed or percentages."
  micro-label:
    textColor: "{colors.text-soft}"
    description: "Small provenance and interface-illustration metadata, kept readable."
---

# TranslatedSubs — custom dark frame adaptation

## Brand and concept

A spoken story becomes readable, editable, and ready to share. The video treats a single line of speech as its recurring motif: waveform, timed transcript, translated subtitle, export path, then brand underline. The imagery evokes a premium media workbench, not a generic analytics dashboard.

The `blue-professional` preset supplied the discipline of one dominant message, asymmetric framing, soft surfaces, and an unmistakable action. Its light background, cobalt-only rule, data cards, and consulting tone are intentionally superseded. The YAML frontmatter here is the normative source for this project.

## Frame craft

- Canvas: 1920x1080, 16:9, 30 fps. Use a safe field of at least 5cqw horizontally and 6cqh vertically. Set a sized root and use frame-relative units against it.
- Most shots use asymmetric 60/40 framing. The illustrated workbench or sample video is the hero, occupying at least 40% of the frame. Reserve centered symmetry for the final wordmark.
- Keep three layers: near-black textured ground; focal media or UI; restrained foreground metadata such as time marks, guide rules, and the recurring subtitle baseline. Aim for 8–10 purposeful elements, not 8–10 cards.
- Display text should read at distance: roughly 64–120px for key lines, 28–42px for explanatory text, and 18–24px for labels. Use only Sora 400 and 700 from the local font files above; resolve asset URLs relative to each composition file.
- All visible words, UI labels, language names, sample subtitles, and spoken lines are English. Place `Interface illustration` visibly but discreetly on reconstructed UI. The UI must reflect shipped behavior, never pretend to be a live task recording.

## Color and surfaces

Near-black `#080B17` is constant across shots. `#F5F7FF` carries primary text. Cyan `#51E6F1` marks source intake, waveform, and forward progress. Violet `#7C5CFC` marks translated text and result states. Coral `#FF927A` is a scarce human touch: an edit caret, a selected timing field, or one final punctuation hit. A few shots may contain both cyan and violet; color richness comes from these purposeful roles, not an indiscriminate rainbow.

Use solid ground plus localized radial blooms, narrow light trails, low-opacity grain, and faint timecode guides. Avoid full-screen linear gradients on the dark background. Keep panel borders quiet. Use glow behind a focal object, never as a substitute for text contrast. No invented positive/negative metrics, prices, testimonials, customer logos, or benchmarks.

## Composition treatments

1. **Hook:** a wide media aperture, waveform, and oversized type; cyan language rail crosses into violet on the payoff.
2. **Import:** one generous workbench window with source options and target-language control; cursor actions are readable at video scale.
3. **Processing:** a task queue and three labeled workflow stages; the sample image is a static illustrative picture with finite, seek-safe framing changes.
4. **Review:** player and subtitle editor share the frame; demonstrate text edits and explicit start/end time fields, without a draggable timeline claim.
5. **Export:** one source branches into three output treatments and a small MCP access branch; avoid identical card walls.
6. **Close:** unchanged `capture/assets/translatedsubs-logo.svg` and wordmark, a one-line invitation, and a single CTA.

## Motion and media rules

Each shot develops over its full duration. Reveal on the corresponding voice cue, including the latter half, then hold long enough to read. Use smooth long-tail settling; no bouncy default, indefinite orbit, repeating breathing, random particle placement, wall-clock animation, or late camera drift. All composition motion must be seek-safe on the paused GSAP timeline using the bundled `assets/vendor/gsap.min.js`.

The unchanged logo asset is the only brand mark. `demo-landscape.png`, once available, is illustrative sample footage inside UI or a media aperture, never a claim of a captured customer task. Apply only finite position/scale reframing to that still; do not fabricate a real-time video or processing result. Preserve the visible `Interface illustration` label wherever the rebuilt UI is presented.

## Self-audit

Check the first and latter half of every frame for a new meaningful reveal; check every on-screen phrase for English; check Sora weights and exact colors; check that export variants, MCP, and timing editing match the verified product; check that the logo remains unchanged. The final frame should hold still for its final read.

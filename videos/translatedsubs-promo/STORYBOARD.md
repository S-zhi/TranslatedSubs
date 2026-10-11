---
format: 1920x1080
message: "Turn a video's voice into subtitles people can understand, refine, and share."
arc: "Hook → Import → Process → Review → Export → CTA"
audience: "Video creators, localization teams, and people who work with spoken media"
language: en
duration: 40s
fps: 30
style: "Open-inspired dark technology; custom adaptation of blue-professional structure"
---

# TranslatedSubs — product launch storyboard

## Video direction

Six connected shots follow one illustrative video from source to finished result. The recurring graphic is a horizontal speech-to-subtitle rail. Cyan identifies intake and forward progress; violet identifies translated text and the completed result; coral is reserved for the human edit and a final CTA accent. The ground stays near-black. Frame 3 and Frame 4 use `assets/demo-landscape.png` as a still illustrative preview with a finite, subtle framing change; it is never represented as captured footage or a real running task. Every rebuilt workbench shot includes the visible note `Interface illustration`. Use `assets/translatedsubs-logo.svg` unchanged.

The shot descriptions below name **local time** from the beginning of each frame. Each shot delays meaningful reveals into its latter half, timed to its spoken line, then holds for a clear read. The quoted screen copy is the exact English wording intended for render. No other language, fabricated metric, customer proof, speed claim, OCR, emotion, tone, classification, dubbing, or unsupported platform promise may appear.

Voice: warm, clear English Kokoro Heart at speed 1.0. Music: restrained cinematic electronic synth pulse, low and spacious under narration, rising harmonically at the output reveal, then easing under the closing line. Sparse SFX cues are suggestions for the shared audio engine or later hand placement: soft waveform onset, one subdued UI click, two small process ticks, edit confirmation, export handoff, and a quiet brand resolve. Avoid loud impacts or dense UI foley. Actual voice durations and word timestamps should refine the guide timing before final animation.

## Frame 1 — Give the voice meaning

- status: animated
- src: compositions/frames/01-hook.html
- duration: 5s
- poster: 4.2s
- transition_in: cut
- type: hook
- persuasion: desire
- beat: voice becomes understandable
- blueprint: kinetic-type-beats (Adapt)
- scene: A luminous speech rail crosses a cinematic media aperture as the headline changes from voice to understanding.
- voiceover: "Every video has a voice. Help more people understand it."
- screen_text: "EVERY VIDEO HAS A VOICE"; "MAKE IT UNDERSTOOD"
- asset_candidates: assets/demo-landscape.png
- focal: assets/demo-landscape.png
- roles: demo-landscape = illustrative media aperture; waveform = foreground motif; type = primary message
- sfx: soft waveform onset

Adapt the blueprint's in-place statement change to a media subject: keep the fixed typographic anchor and the decisive word/phrase swap, while the illustrative landscape provides context behind the speech rail. The still image may change framing once on a finite timeline; it must not simulate moving source footage.

Scene 1 (0.0–1.5s): Near-black textured ground, large landscape aperture on the right and `EVERY VIDEO HAS A VOICE` anchored left; a cyan waveform self-draws (`svg-path-draw`) beneath the first phrase while the words reveal in paced chunks (`dynamic-content-sequencing`). The image has one subtle finite scale/position settle.

Scene 2 (1.5–3.6s): As the second sentence begins, the first line's key phrase gives way in place (`discrete-text-sequence`); cyan speech ticks travel along the rail and resolve into evenly timed caption markers (`dynamic-content-sequencing`). The camera stays locked to preserve the clean reading path.

Scene 3 (3.6–5.0s): `MAKE IT UNDERSTOOD` becomes the sole dominant line, with a violet keyword glow landing on `UNDERSTOOD` (`asr-keyword-glow`). The rail completes its passage and settles; hold the headline and aperture for the last half-second. Cut on the rail's lateral velocity into the next shot.

## Frame 2 — Start with a source

- status: animated
- src: compositions/frames/02-import.html
- duration: 6s
- poster: 5.2s
- transition_in: cut
- type: product_intro
- persuasion: simple entry
- beat: supported link or local file, then target language
- blueprint: cursor-ui-demo (Adapt)
- scene: A single illustrated workbench surface responds to source selection and a target-language choice.
- voiceover: "Meet TranslatedSubs. Add a video link or a local file, then choose a target language."
- screen_text: "TranslatedSubs"; "Source"; "Supported video URL"; "Local file"; "Target language"; "English"; "Start task"; "Interface illustration"
- asset_candidates: assets/demo-landscape.png
- focal: assets/demo-landscape.png
- roles: source controls = primary UI; demo-landscape = illustrative thumbnail; cursor = action driver; interface label = provenance
- sfx: subdued UI click

Keep the blueprint's visible cursor and live UI response. The cursor shows the two available source routes, chooses one, sets the target, and starts a task. Do not type or display a platform-specific URL, since support varies by source. The same UI window remains the subject throughout; avoid fanning out generic cards.

Scene 1 (0.0–1.7s): A large, legible workbench window settles on the left two-thirds; `TranslatedSubs` and `Source` appear first, then `Supported video URL` and `Local file` become readable on their voice cues. A custom cyan cursor sweeps the two options (`camera-cursor-tracking`, `dynamic-content-sequencing`). The discreet `Interface illustration` note sits in the chrome.

Scene 2 (1.7–3.6s): Cursor clicks one source option (`cursor-click-ripple`) and a single illustrative thumbnail using `demo-landscape.png` occupies the preview zone. A narrow cyan source rail draws across the lower edge (`svg-path-draw`). The interface responds in the same beat; the picture holds as a still.

Scene 3 (3.6–5.1s): As narration names the language, focus shifts to `Target language`, the selector resolves to `English`, and the task summary appears (`viewport-change`, `dynamic-content-sequencing`). Keep labels large enough to read without a long zoom.

Scene 4 (5.1–6.0s): Cursor presses `Start task` with restrained compression and ripple (`press-release-spring`, `cursor-click-ripple`); a queued task row begins to appear. The end state holds briefly, handing the task row's position to Frame 3.

## Frame 3 — Follow the transformation

- status: animated
- src: compositions/frames/03-process.html
- duration: 8s
- poster: 7.2s
- transition_in: crossfade
- type: feature_showcase
- persuasion: visible process
- beat: speech, transcript, translated subtitle, task progress
- blueprint: device-surface-showcase (Adapt: static workbench tour)
- scene: One task window progresses from speech waveform to transcript rows and an English subtitle preview.
- voiceover: "Watch speech become a transcript, then translated subtitles, with progress you can follow."
- screen_text: "Task progress"; "Transcribing"; "Translating"; "Subtitles ready"; "We begin at dawn."; "Interface illustration"
- asset_candidates: assets/demo-landscape.png
- focal: assets/demo-landscape.png
- roles: task window = hero surface; demo-landscape = illustrative video preview; progress stages = supporting proof; translated subtitle = payoff
- sfx: two soft process ticks

Use the blueprint's single held device/window and advancing inner states; avoid a 3D hand, WebGL portal, or false live-rendered video. Transcript content is represented by timed neutral line bars, so all legible information remains English. The English target subtitle `We begin at dawn.` is illustrative and continues into Frame 4. No percentages, ETA, or performance claim appears.

Scene 1 (0.0–2.0s): The queued row from Frame 2 occupies a large product window on the right; `Task progress` and `Transcribing` reveal in sequence. A cyan stage line fills only to its next state (`stat-bars-and-fills`), while the illustrative landscape sits still in a clipped preview panel.

Scene 2 (2.0–4.5s): Waveform segments resolve into timed transcript-row shapes one by one as the VO says “transcript” (`dynamic-content-sequencing`, `svg-path-draw`). A finite, almost imperceptible reframe of the still image completes before the back half; no ongoing camera drift.

Scene 3 (4.5–6.6s): `Translating` lights in violet on its spoken cue; the second rail assembles row by row beneath the source rail (`dynamic-content-sequencing`, `asr-keyword-glow`). Keep the prior stage visible but quieter so the workflow is intelligible.

Scene 4 (6.6–8.0s): `Subtitles ready` appears, followed by the English subtitle `We begin at dawn.` over the preview (`dynamic-content-sequencing`). The status and subtitle settle into a clean held read. Frame 4 inherits the subtitle's position.

## Frame 4 — Refine in context

- status: animated
- src: compositions/frames/04-review.html
- duration: 8s
- poster: 7.2s
- transition_in: crossfade
- type: feature_showcase
- persuasion: human control
- beat: review subtitle wording and explicit time values
- blueprint: cursor-ui-demo (Reproduce)
- scene: Cursor edits one subtitle row and its Start/End fields while the adjacent preview updates.
- voiceover: "Preview the video. Refine the words and timing right beside the picture."
- screen_text: "Preview"; "Subtitle editor"; "We begin at dawn."; "We begin at first light."; "Start"; "End"; "00:02.1"; "00:02.3"; "00:04.7"; "00:04.9"; "Interface illustration"
- asset_candidates: assets/demo-landscape.png
- focal: assets/demo-landscape.png
- roles: demo-landscape = illustrative preview; subtitle row and time fields = primary UI; cursor = action driver; coral caret = human edit accent
- sfx: soft edit confirmation

The shot demonstrates verified text and timing editing through visible form fields. **Do not render a draggable subtitle timeline, handle, or scrub-to-trim interaction.** The time values are illustrative UI data, not a speed or quality claim. The still landscape may move only through one finite seek-safe crop settle; do not make it look like actual playback.

Scene 1 (0.0–2.0s): The preview and `Subtitle editor` occupy balanced left/right zones; `We begin at dawn.` sits over the illustrative still and in the selected row (`dynamic-content-sequencing`). The `Interface illustration` note remains visible.

Scene 2 (2.0–4.2s): Cursor clicks the row, selects `dawn`, and replaces the line with `We begin at first light.` (`cursor-click-ripple`, `discrete-text-sequence`). The preview caption updates in the same beat; the coral caret and selected row draw the eye without flooding the panel.

Scene 3 (4.2–6.5s): Cursor focuses explicit `Start` and `End` input fields; `00:02.1` becomes `00:02.3`, then `00:04.7` becomes `00:04.9` (`camera-cursor-tracking`, `discrete-text-sequence`). A quiet timing rule shifts to reflect the new interval (`stat-bars-and-fills`), without showing a draggable control.

Scene 4 (6.5–8.0s): Updated caption and time fields hold together beside the still preview. A small violet confirmation state resolves (`asr-keyword-glow`), then the surface rests for readability. No new edit is implied after the VO ends.

## Frame 5 — Choose the result

- status: animated
- src: compositions/frames/05-export.html
- duration: 8s
- poster: 7.2s
- transition_in: cut
- type: feature_showcase
- persuasion: flexible delivery
- beat: subtitle file, soft subtitles, burned-in captions, MCP
- blueprint: grid-card-assemble (Adapt: branching export rail)
- scene: A finished source branches sequentially into three output formats and a small MCP connector.
- voiceover: "Download subtitles, choose soft or burned-in captions, and connect the same workflow to your tools through MCP."
- screen_text: "Subtitle file"; "Soft subtitles"; "Burned-in captions"; "MCP access"; "Interface illustration"
- asset_candidates: assets/demo-landscape.png
- focal: assets/demo-landscape.png
- roles: finished source = origin; three output branches = primary comparison; MCP connector = supporting integration; demo-landscape = illustrative thumbnail
- sfx: gentle export handoff

Adapt the blueprint's staggered self-assembly into a **single source with branching outputs**, not four identical feature cards. The soft-subtitle output visibly shows a separate selectable text track; the burned-in output visibly places the English caption into the picture. The SRT-style subtitle file remains a file glyph. MCP is a connector to the workflow, not a fourth media format. Do not imply Google Drive is built in or universally available.

Scene 1 (0.0–2.0s): The refined preview from Frame 4 shrinks to a strong origin tile on the left; a cyan export line draws to the right (`svg-path-draw`). The updated English caption remains visible in the origin for continuity.

Scene 2 (2.0–4.7s): On “Download subtitles” and “soft,” `Subtitle file` arrives first as a text-document glyph, then `Soft subtitles` as a video glyph with a distinct subtitle-track rail (`dynamic-content-sequencing`, `center-outward-expansion`). Keep both large enough to read.

Scene 3 (4.7–6.3s): `Burned-in captions` arrives on its voice cue as a third output, with the caption visibly embedded in the preview image (`dynamic-content-sequencing`). Violet traces the completed branches while cyan remains on the source line.

Scene 4 (6.3–8.0s): A thinner line branches to `MCP access` and a simple connector glyph as the VO names MCP (`svg-path-draw`, `dynamic-content-sequencing`). The three output treatments and MCP hold together briefly; no download animation asserts a live generated file.

## Frame 6 — Take the story further

- status: animated
- src: compositions/frames/06-close.html
- duration: 5s
- poster: 4.4s
- transition_in: crossfade
- type: cta
- persuasion: invitation
- beat: brand recall and action
- blueprint: logo-assemble-lockup (Adapt: use the existing logo unchanged)
- scene: Speech and subtitle rails converge beneath the unchanged logo, then the wordmark and CTA settle.
- voiceover: "TranslatedSubs. Take your story further. Start translating."
- screen_text: "TranslatedSubs"; "Take your story further."; "Start translating"
- asset_candidates: assets/translatedsubs-logo.svg
- focal: assets/translatedsubs-logo.svg
- roles: original logo = brand anchor; wordmark = name; CTA = single action; rails = recurring visual motif
- sfx: quiet brand resolve

Adapt the blueprint's assembly to the existing SVG: **do not redraw, recolor, or modify the logo's internal paths**. Assemble the surrounding rails and wordmark, then let the real logo appear unchanged. No invented domain or URL.

Scene 1 (0.0–1.5s): The preceding output rails clear under the frame transition; a cyan waveform line and violet subtitle line enter from opposite sides and converge beneath the original logo position (`center-outward-expansion` adapted inward, `svg-path-draw`). The logo itself fades in unchanged.

Scene 2 (1.5–3.2s): `TranslatedSubs` builds beside the logo in a measured letterwise reveal (`dynamic-content-sequencing`), followed by `Take your story further.` on the voice cue. The nearby blooms settle; no orbital or repeating motion.

Scene 3 (3.2–4.2s): `Start translating` enters as the only CTA with a cyan action surface (`spring-pop-entrance` in smooth-settle mode); a single coral accent punctuates its final arrival (`asr-keyword-glow`).

Scene 4 (4.2–5.0s): Entire lockup, invitation, and CTA hold still for a final read. End on a clean near-black field without an extra invented transition or URL.

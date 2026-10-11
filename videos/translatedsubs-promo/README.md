# TranslatedSubs Promo

A 40-second, 1080p, 30 fps English product promo for TranslatedSubs. The video uses an Open-inspired dark technology direction: near-black surfaces with cyan for source and progress, violet for translated results, and restrained coral for human edits.

The workbench scenes are interface illustrations, not live screen recordings. The scenic preview image is AI-generated artwork used as illustrative sample media. The product information shown is limited to implemented workflows: video-link or local-file intake, transcription and translation tasks with progress, subtitle text and explicit timing edits, subtitle-file and soft or burned-in subtitle outputs, and MCP access. The video does not claim automated dubbing, emotion or tone analysis, or other unshipped capabilities.

The original TranslatedSubs logo is used unchanged. Scenic artwork was AI-generated; the background music was retrieved through HeyGen; English narration is generated locally with Kokoro Heart. An optional `narration.srt` may accompany the narration when generated.

## Edit and preview

Edit the compositions in `compositions/frames/` and the storyboard in `STORYBOARD.md`. The project pins its HyperFrames CLI through the npm scripts:

```sh
npm install
npm run dev
```

## Check and render

Run the project check before rendering:

```sh
npm run check
npm run render
```

The render is written under `renders/`. Use `node scripts/finalize-project.mjs` after frame assembly to mark existing storyboard sources animated, set the local voice label and caption policy, and replace the generated index's GSAP CDN path. Use `--skip-index` to leave `index.html` untouched, or pass `--voice "Voice label"` to set a different voice label.

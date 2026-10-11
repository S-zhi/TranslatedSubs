# Product showcase

The public homepage at `/` presents the TranslatedSubs product film, workflow, and output formats. The working task interface remains at `/tasks`; the MCP client information is available at `/mcp`.

Showcase media and local font files live in `web/assets/showcase/`. The eye-and-subtitles brand mark is `web/assets/eye-subtitles-logo.svg`. The page uses these static assets directly, with no CDN dependencies.

Run the application locally with:

```sh
uv run uvicorn src.handler.app:app --reload --port 8000
```

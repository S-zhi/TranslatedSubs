# Phosphor regular webfont

Vendored from the official npm package [`@phosphor-icons/web@2.1.1`](https://registry.npmjs.org/@phosphor-icons/web/-/web-2.1.1.tgz), maintained at <https://github.com/phosphor-icons/web>.

`regular/style.css`, all four fonts it references, and `LICENSE` are unchanged
upstream files. Only the regular weight is included; it uses `ph ph-ICON` classes.
The SVG, TTF, WOFF, and WOFF2 files are retained so every upstream CSS URL resolves
locally. No JavaScript entrypoint, npm scripts, other weights, or selection data
are included. The application loads `vendor/phosphor/regular/style.css`.

The MIT license and copyright notice are preserved in `LICENSE`.
`provenance.json` records the package URL, npm SHA512 integrity, archive SHA256,
and the original archive path, byte count, and SHA256 of each vendored file.

The supplied archive was checked against its pinned SHA256 and registry SHA512.
All tar entries were checked for absolute paths, traversal, duplicate names,
symlinks, hard links, and special files before selected regular files were
manually extracted into a new empty staging directory. Archive code was never
executed. The upstream README was read as package documentation.

Run the offline static checks from the repository root:

```sh
.venv/bin/python -I web/vendor/phosphor/test_vendor.py
```

The checks cover recorded hashes, local CSS URLs, font formats, CSS-to-SVG glyph
coverage, and literal icon names in application HTML, JavaScript, and CSS.
They do not claim browser rendering or packaged WKWebView acceptance.
The existing UI's `ph-spin` helper is excluded from icon-name checks: upstream
does not supply an animation for it; animation styling belongs to the application.

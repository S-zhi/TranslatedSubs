# Three.js browser runtime

Vendored from the official `three` npm package, version **0.186.1** (MIT).

- `three.module.min.js`: WebGL renderer ES module.
- `three.core.js`: its core dependency, minified by jsDelivr and saved under the
  relative import name used by the renderer.
- `LICENSE`: upstream license.

Source: https://www.npmjs.com/package/three/v/0.186.1

The workbench dynamically imports this local runtime when its decorative scene
is visible. Serving these files with the existing static frontend needs no build
step, CDN connection, external service, model download, or Python dependency.
Keep both JavaScript files on the same version when updating them.

The scene follows the fundamentals, animation, and shader guidance from
https://github.com/cloudai-x/threejs-skills and the Three.js documentation.

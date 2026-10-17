import * as THREE from "../vendor/three/three.module.min.js";

// Three interwoven satin ribbons share one parameterization with their filaments.
// All deformation happens on the GPU; the CPU only advances a few uniforms.
const RIBBON_CURVE = `
  uniform float uTime;
  vec3 ribbonPoint(vec3 parameter, float lift) {
    float u = clamp(parameter.x, 0.0, 1.0);
    float width = parameter.y;
    float envelope = pow(max(sin(u * 3.14159265), 0.0), 0.78);
    float phase = u * 8.6 + parameter.z * 2.0943951 + uTime * 0.13;
    phase += sin(u * 6.2831853 - uTime * 0.32) * 0.14;
    phase += width * 0.25;
    float radius = envelope * (1.0 + width * 0.025 + lift);
    return vec3(
      (u - 0.5) * 4.05,
      cos(phase) * radius * 0.77 + sin(u * 6.2831853) * 0.07,
      sin(phase) * radius * 0.68
    );
  }
`;

const RIBBON_VERTEX = `
  ${RIBBON_CURVE}
  varying vec3 vNormal;
  varying vec3 vViewPosition;
  varying vec3 vParameter;
  void main() {
    vec3 point = ribbonPoint(position, 0.0);
    vec3 along = ribbonPoint(position + vec3(0.001, 0.0, 0.0), 0.0)
               - ribbonPoint(position - vec3(0.001, 0.0, 0.0), 0.0);
    vec3 across = ribbonPoint(position + vec3(0.0, 0.01, 0.0), 0.0)
                - ribbonPoint(position - vec3(0.0, 0.01, 0.0), 0.0);
    vNormal = normalize(normalMatrix * cross(along, across));
    vec4 viewPosition = modelViewMatrix * vec4(point, 1.0);
    vViewPosition = viewPosition.xyz;
    vParameter = position;
    gl_Position = projectionMatrix * viewPosition;
  }
`;

const RIBBON_FRAGMENT = `
  uniform vec3 uDeep;
  uniform vec3 uCyan;
  uniform vec3 uHighlight;
  varying vec3 vNormal;
  varying vec3 vViewPosition;
  varying vec3 vParameter;
  void main() {
    vec3 normal = normalize(vNormal) * (gl_FrontFacing ? 1.0 : -1.0);
    vec3 view = normalize(-vViewPosition);
    vec3 light = normalize(vec3(-0.4, 0.9, 1.2));
    float diffuse = max(dot(normal, light), 0.0);
    float rim = pow(1.0 - max(dot(normal, view), 0.0), 2.4);
    float specular = pow(max(dot(normal, normalize(light + view)), 0.0), 42.0);
    float gradient = 0.5 + 0.5 * sin(vParameter.x * 4.6 + vParameter.z * 1.8);
    vec3 color = mix(uDeep, uCyan, gradient * 0.72);
    color *= 0.48 + diffuse * 0.72;
    color += uHighlight * (specular * 0.72 + rim * 0.26);
    gl_FragColor = vec4(color, 1.0);
    #include <colorspace_fragment>
  }
`;

const FILAMENT_VERTEX = `
  ${RIBBON_CURVE}
  varying float vFront;
  varying float vAlong;
  void main() {
    vec3 point = ribbonPoint(position, 0.035);
    vFront = point.z * 0.65 + 0.5;
    vAlong = position.x;
    gl_Position = projectionMatrix * modelViewMatrix * vec4(point, 1.0);
  }
`;

const FILAMENT_FRAGMENT = `
  uniform vec3 uFilament;
  uniform vec3 uDeep;
  uniform float uLineOpacity;
  varying float vFront;
  varying float vAlong;
  void main() {
    float taper = smoothstep(0.0, 0.08, vAlong) * (1.0 - smoothstep(0.92, 1.0, vAlong));
    float front = clamp(vFront, 0.0, 1.0);
    vec3 color = mix(uDeep, uFilament, 0.35 + front * 0.65);
    gl_FragColor = vec4(color, taper * (0.28 + front * 0.48) * uLineOpacity);
    #include <colorspace_fragment>
  }
`;

const PARTICLE_VERTEX = `
  uniform float uTime;
  uniform float uDpr;
  attribute vec2 aSeed;
  varying float vAlpha;
  void main() {
    vec3 point = position;
    point.y += sin(uTime * 0.24 + aSeed.x) * 0.045;
    point.x += cos(uTime * 0.18 + aSeed.x) * 0.035;
    vAlpha = 0.24 + aSeed.y * 0.42;
    gl_PointSize = (1.1 + aSeed.y * 1.4) * uDpr;
    gl_Position = projectionMatrix * modelViewMatrix * vec4(point, 1.0);
  }
`;

const PARTICLE_FRAGMENT = `
  uniform vec3 uFilament;
  varying float vAlpha;
  void main() {
    float radius = length(gl_PointCoord - vec2(0.5));
    float alpha = (1.0 - smoothstep(0.15, 0.5, radius)) * vAlpha;
    gl_FragColor = vec4(uFilament, alpha);
    #include <colorspace_fragment>
  }
`;

function createRibbonGeometry() {
  const segments = 176;
  const widthSegments = 8;
  const verticesPerRibbon = (segments + 1) * (widthSegments + 1);
  const positions = new Float32Array(verticesPerRibbon * 3 * 3);
  const indices = new Uint16Array(segments * widthSegments * 6 * 3);
  let vertexOffset = 0;
  let indexOffset = 0;
  for (let ribbon = 0; ribbon < 3; ribbon += 1) {
    for (let along = 0; along <= segments; along += 1) {
      for (let across = 0; across <= widthSegments; across += 1) {
        // Avoid degenerate normals at the pinched ends of the ribbons.
        positions[vertexOffset++] = 0.002 + (along / segments) * 0.996;
        positions[vertexOffset++] = (across / widthSegments) * 2 - 1;
        positions[vertexOffset++] = ribbon;
        if (along < segments && across < widthSegments) {
          const a = ribbon * verticesPerRibbon + along * (widthSegments + 1) + across;
          const b = a + widthSegments + 1;
          indices[indexOffset++] = a;
          indices[indexOffset++] = b;
          indices[indexOffset++] = a + 1;
          indices[indexOffset++] = b;
          indices[indexOffset++] = b + 1;
          indices[indexOffset++] = a + 1;
        }
      }
    }
  }
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute("position", new THREE.BufferAttribute(positions, 3));
  geometry.setIndex(new THREE.BufferAttribute(indices, 1));
  // Positions encode parameters; use the actual deformed extent for culling.
  geometry.boundingSphere = new THREE.Sphere(new THREE.Vector3(), 2.35);
  return geometry;
}

function createFilamentGeometry() {
  const strands = 18;
  const segments = 176;
  const positions = new Float32Array(3 * strands * segments * 2 * 3);
  let offset = 0;
  for (let ribbon = 0; ribbon < 3; ribbon += 1) {
    for (let strand = 0; strand < strands; strand += 1) {
      const width = (strand / (strands - 1)) * 3.6 - 1.8;
      for (let segment = 0; segment < segments; segment += 1) {
        positions[offset++] = segment / segments;
        positions[offset++] = width;
        positions[offset++] = ribbon;
        positions[offset++] = (segment + 1) / segments;
        positions[offset++] = width;
        positions[offset++] = ribbon;
      }
    }
  }
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute("position", new THREE.BufferAttribute(positions, 3));
  geometry.boundingSphere = new THREE.Sphere(new THREE.Vector3(), 2.4);
  return geometry;
}

function createParticleGeometry() {
  const count = 34;
  const positions = new Float32Array(count * 3);
  const seeds = new Float32Array(count * 2);
  // Deterministic, sparse elliptical distribution; no ambient dust field.
  for (let index = 0; index < count; index += 1) {
    const phase = index * 2.39996323;
    const spread = ((index * 17) % 31) / 31;
    positions[index * 3] = Math.cos(phase) * (1.45 + spread * 0.75);
    positions[index * 3 + 1] = Math.sin(phase) * (0.68 + spread * 0.33);
    positions[index * 3 + 2] = Math.sin(phase * 1.7) * 0.65;
    seeds[index * 2] = phase;
    seeds[index * 2 + 1] = spread;
  }
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute("position", new THREE.BufferAttribute(positions, 3));
  geometry.setAttribute("aSeed", new THREE.BufferAttribute(seeds, 2));
  geometry.boundingSphere = new THREE.Sphere(new THREE.Vector3(), 2.5);
  return geometry;
}

/**
 * Mount the decorative hero sculpture. Visibility and user motion preferences
 * belong to the caller; reducedMotion permanently opts this instance out of RAF.
 */
export function createWorkbenchScene(host, { reducedMotion = false } = {}) {
  if (!host || !host.ownerDocument || typeof host.appendChild !== "function") {
    throw new TypeError("A host element is required for the workbench scene.");
  }

  const view = host.ownerDocument.defaultView;
  const geometries = [];
  const materials = [];
  const scene = new THREE.Scene();
  const sculpture = new THREE.Group();
  const camera = new THREE.OrthographicCamera(-2.5, 2.5, 1.25, -1.25, 0.1, 20);
  camera.position.z = 7;
  sculpture.rotation.set(0.18, -0.19, -0.13);
  scene.add(sculpture);

  const uniforms = {
    uTime: { value: 0 },
    uDpr: { value: 1 },
    uDeep: { value: new THREE.Color(0x2547b4) },
    uCyan: { value: new THREE.Color(0x58dcea) },
    uHighlight: { value: new THREE.Color(0xd0f6ff) },
    uFilament: { value: new THREE.Color(0x9ee6ff) },
    uLineOpacity: { value: 0.85 },
  };

  let renderer;
  let canvas;
  let observer;
  let disposed = false;
  let unavailable = false;
  let contextLost = false;
  let ready = false;
  let hasSize = false;
  let running = false;
  let motionEnabled = !reducedMotion;
  let lastTimestamp = null;
  let time = 0;
  let pointerX = 0;
  let pointerY = 0;
  let easedX = 0;
  let easedY = 0;

  function render() {
    if (disposed || unavailable || contextLost || !hasSize) return;
    renderer.render(scene, camera);
    if (!ready) {
      ready = true;
      canvas.style.visibility = "";
      host.classList.remove("scene-unavailable");
      host.classList.add("is-ready");
    }
  }

  function showFallback() {
    unavailable = true;
    ready = false;
    running = false;
    lastTimestamp = null;
    renderer.setAnimationLoop(null);
    canvas.style.visibility = "hidden";
    host.classList.remove("is-ready");
    host.classList.add("scene-unavailable");
  }

  function animate(timestamp) {
    if (disposed || unavailable || contextLost || !running) return;
    const delta = lastTimestamp === null ? 0 : Math.min(Math.max((timestamp - lastTimestamp) / 1000, 0), 0.05);
    lastTimestamp = timestamp;
    time += delta;
    uniforms.uTime.value = time;
    const damping = 1 - Math.exp(-delta * 5);
    easedX += (pointerX - easedX) * damping;
    easedY += (pointerY - easedY) * damping;
    sculpture.rotation.x = 0.18 + easedY * 0.12 + Math.sin(time * 0.21) * 0.035;
    sculpture.rotation.y = -0.19 + easedX * 0.18 + Math.sin(time * 0.17) * 0.045;
    sculpture.rotation.z = -0.13 + easedX * 0.025;
    sculpture.position.y = Math.sin(time * 0.3) * 0.025;
    try {
      render();
    } catch {
      showFallback();
    }
  }

  function syncAnimation() {
    if (disposed) return;
    const shouldRun = motionEnabled && !reducedMotion && hasSize && !unavailable && !contextLost;
    if (shouldRun === running) return;
    running = shouldRun;
    lastTimestamp = null;
    renderer.setAnimationLoop(shouldRun ? animate : null);
  }

  function resize() {
    if (disposed || contextLost) return;
    const width = host.clientWidth;
    const height = host.clientHeight;
    if (width <= 0 || height <= 0) return;
    const dpr = Math.min(view.devicePixelRatio || 1, 1.5);
    uniforms.uDpr.value = dpr;
    renderer.setPixelRatio(dpr);
    renderer.setSize(width, height, false);
    const aspect = width / height;
    const halfHeight = Math.max(1.22, 2.5 / aspect);
    camera.left = -halfHeight * aspect;
    camera.right = halfHeight * aspect;
    camera.top = halfHeight;
    camera.bottom = -halfHeight;
    camera.updateProjectionMatrix();
    hasSize = true;
    render();
    syncAnimation();
  }

  function onResize() {
    try {
      resize();
    } catch {
      showFallback();
    }
  }

  function onPointerMove(event) {
    if (!running || event.pointerType === "touch") return;
    const bounds = host.getBoundingClientRect();
    if (bounds.width <= 0 || bounds.height <= 0) return;
    pointerX = THREE.MathUtils.clamp(((event.clientX - bounds.left) / bounds.width) * 2 - 1, -1, 1);
    pointerY = THREE.MathUtils.clamp(((event.clientY - bounds.top) / bounds.height) * 2 - 1, -1, 1);
  }

  function onPointerLeave() {
    pointerX = 0;
    pointerY = 0;
  }

  function onContextLost(event) {
    event.preventDefault();
    contextLost = true;
    showFallback();
  }

  function onContextRestored() {
    if (disposed) return;
    contextLost = false;
    unavailable = false;
    onResize();
  }

  function setMotionEnabled(enabled) {
    if (disposed) return;
    motionEnabled = Boolean(enabled);
    if (!motionEnabled) onPointerLeave();
    syncAnimation();
    try {
      render();
    } catch {
      showFallback();
    }
  }

  function setTheme(isDark) {
    if (disposed) return;
    uniforms.uDeep.value.setHex(isDark ? 0x2547b4 : 0x163c9c);
    uniforms.uCyan.value.setHex(isDark ? 0x58dcea : 0x087fae);
    uniforms.uHighlight.value.setHex(isDark ? 0xd0f6ff : 0xb6e5ff);
    uniforms.uFilament.value.setHex(isDark ? 0x9ee6ff : 0x2863b5);
    uniforms.uLineOpacity.value = isDark ? 0.85 : 0.75;
    try {
      render();
    } catch {
      showFallback();
    }
  }

  function dispose() {
    if (disposed) return;
    disposed = true;
    running = false;
    renderer?.setAnimationLoop(null);
    observer?.disconnect();
    view.removeEventListener("resize", onResize);
    host.removeEventListener("pointermove", onPointerMove);
    host.removeEventListener("pointerleave", onPointerLeave);
    host.removeEventListener("pointercancel", onPointerLeave);
    canvas?.removeEventListener("webglcontextlost", onContextLost);
    canvas?.removeEventListener("webglcontextrestored", onContextRestored);
    for (const geometry of geometries) geometry.dispose();
    for (const material of materials) material.dispose();
    scene.clear();
    renderer?.dispose();
    renderer?.forceContextLoss();
    canvas?.remove();
    host.classList.remove("is-ready");
  }

  try {
    renderer = new THREE.WebGLRenderer({
      alpha: true,
      antialias: true,
      powerPreference: "low-power",
      preserveDrawingBuffer: false,
    });
    renderer.setClearColor(0x000000, 0);
    renderer.outputColorSpace = THREE.SRGBColorSpace;
    renderer.debug.onShaderError = () => {
      throw new Error("The workbench scene shaders could not be compiled.");
    };
    canvas = renderer.domElement;
    canvas.className = "studio-hero__canvas";
    canvas.setAttribute("aria-hidden", "true");
    canvas.style.cssText = "position:absolute;inset:0;display:block;width:100%;height:100%;pointer-events:none;";

    const ribbonGeometry = createRibbonGeometry();
    geometries.push(ribbonGeometry);
    const filamentGeometry = createFilamentGeometry();
    geometries.push(filamentGeometry);
    const particleGeometry = createParticleGeometry();
    geometries.push(particleGeometry);
    const ribbonMaterial = new THREE.ShaderMaterial({
      uniforms,
      vertexShader: RIBBON_VERTEX,
      fragmentShader: RIBBON_FRAGMENT,
      side: THREE.DoubleSide,
      toneMapped: false,
    });
    materials.push(ribbonMaterial);
    const filamentMaterial = new THREE.ShaderMaterial({
      uniforms,
      vertexShader: FILAMENT_VERTEX,
      fragmentShader: FILAMENT_FRAGMENT,
      transparent: true,
      depthWrite: false,
      toneMapped: false,
    });
    materials.push(filamentMaterial);
    const particleMaterial = new THREE.ShaderMaterial({
      uniforms,
      vertexShader: PARTICLE_VERTEX,
      fragmentShader: PARTICLE_FRAGMENT,
      transparent: true,
      depthWrite: false,
      toneMapped: false,
    });
    materials.push(particleMaterial);
    const ribbons = new THREE.Mesh(ribbonGeometry, ribbonMaterial);
    const filaments = new THREE.LineSegments(filamentGeometry, filamentMaterial);
    const particles = new THREE.Points(particleGeometry, particleMaterial);
    sculpture.add(ribbons, filaments, particles);

    canvas.addEventListener("webglcontextlost", onContextLost, false);
    canvas.addEventListener("webglcontextrestored", onContextRestored, false);
    host.addEventListener("pointermove", onPointerMove, { passive: true });
    host.addEventListener("pointerleave", onPointerLeave, { passive: true });
    host.addEventListener("pointercancel", onPointerLeave, { passive: true });
    host.appendChild(canvas);
    resize();
    if (view.ResizeObserver) {
      observer = new view.ResizeObserver(onResize);
      observer.observe(host);
    }
    view.addEventListener("resize", onResize, { passive: true });
    return { setMotionEnabled, setTheme, dispose };
  } catch (error) {
    dispose();
    host.classList.add("scene-unavailable");
    throw error;
  }
}

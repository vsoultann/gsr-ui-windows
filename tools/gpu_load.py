"""Offscreen stand-in for a game: renders a heavy fragment shader into an invisible 1920x1080
framebuffer as fast as possible and prints its frame rate once per second. No window is shown.

    python tools/gpu_load.py SECONDS [--cap FPS]

Used by perf_test.py to measure how many frames a "game" loses while recording.
"""
import sys
import time

from PySide6.QtGui import QGuiApplication, QOffscreenSurface, QOpenGLContext, QSurfaceFormat
from PySide6.QtOpenGL import QOpenGLFramebufferObject, QOpenGLShader, QOpenGLShaderProgram

seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 10
cap = float(sys.argv[sys.argv.index("--cap") + 1]) if "--cap" in sys.argv else 0

app = QGuiApplication(sys.argv[:1])
fmt = QSurfaceFormat()
fmt.setSwapInterval(0)
surf = QOffscreenSurface()
surf.setFormat(fmt)
surf.create()
ctx = QOpenGLContext()
ctx.setFormat(fmt)
assert ctx.create(), "no OpenGL context"
ctx.makeCurrent(surf)
gl = ctx.functions()
W, H = 1920, 1080
fbo = QOpenGLFramebufferObject(W, H)

prog = QOpenGLShaderProgram()
prog.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Vertex, """
attribute vec2 pos;
varying vec2 uv;
void main() { uv = pos * 0.5 + 0.5; gl_Position = vec4(pos, 0.0, 1.0); }
""")
# a raymarched field: enough per-pixel work to keep the 3D engine busy like a game frame
prog.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Fragment, """
uniform float t;
varying vec2 uv;
float map(vec3 p) {
    vec3 q = mod(p, 2.0) - 1.0;
    return length(q) - 0.35 + 0.1 * sin(p.x * 3.0 + t) * sin(p.y * 3.0) * sin(p.z * 3.0 + t);
}
void main() {
    vec3 ro = vec3(0.0, 0.0, t);
    vec3 rd = normalize(vec3(uv * 2.0 - 1.0, 1.2));
    float d = 0.0;
    for (int i = 0; i < 96; i++) { float s = map(ro + rd * d); if (s < 0.001) break; d += s; }
    gl_FragColor = vec4(vec3(1.0 / (1.0 + d * 0.15)), 1.0);
}
""")
assert prog.link(), prog.log()
prog.bind()
loc = prog.attributeLocation("pos")
tloc = prog.uniformLocation("t")
import array  # noqa: E402
from PySide6.QtOpenGL import QOpenGLBuffer  # noqa: E402
quad = array.array("f", [-1, -1, 1, -1, -1, 1, 1, 1]).tobytes()
vbo = QOpenGLBuffer(QOpenGLBuffer.Type.VertexBuffer)
vbo.create()
vbo.bind()
vbo.allocate(quad, len(quad))
GL_FLOAT = 0x1406
prog.enableAttributeArray(loc)
prog.setAttributeBuffer(loc, GL_FLOAT, 0, 2, 0)

GL_TRIANGLE_STRIP = 0x0005
fbo.bind()
gl.glViewport(0, 0, W, H)
start = last = time.perf_counter()
frames = 0
renderer = gl.glGetString(0x1F01)
print(f'{{"renderer": "{renderer}"}}', flush=True)
while True:
    now = time.perf_counter()
    if now - start >= seconds:
        break
    prog.setUniformValue1f(tloc, now - start)
    gl.glDrawArrays(GL_TRIANGLE_STRIP, 0, 4)
    gl.glFinish()
    frames += 1
    if cap:
        nxt = start + frames / cap
        while time.perf_counter() < nxt:
            time.sleep(0.0005)
    if now - last >= 1.0:
        print(f'{{"fps": {frames / (now - last):.1f}}}', flush=True)
        frames, last = 0, now

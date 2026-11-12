# SPDX-License-Identifier: Apache-2.0
"""Which GTK renderer the desktop app asks for.

GTK 4's default Vulkan renderer cannot import WebKit's dmabuf frames on some drivers (NVIDIA): it
then copies every frame through the CPU, which keeps a core half busy while the window is open. The
GL renderer imports them directly. Its name changed: "ngl" from GTK 4.14, "gl" from 4.18 (where
"ngl" still works but logs a warning). Kept free of gi so the tests can import it.
"""


def gl_renderer(version):
    """GSK_RENDERER value for the GL renderer on GTK `version` (major, minor)."""
    return "gl" if tuple(version) >= (4, 18) else "ngl"


def choose(environ, version):
    """Ask for the GL renderer unless the user already chose one (an explicit choice wins)."""
    if not environ.get("GSK_RENDERER"):
        environ["GSK_RENDERER"] = gl_renderer(version)
    return environ["GSK_RENDERER"]

"""Native, hidden SDL checks against functions extracted from actual QEMU source."""
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
BUILD = Path(os.environ.get('WCP_QEMU_BUILD_ROOT', ROOT / 'state/qemu-window-build'))


def extract_function(source, name, required=True):
    # Mask comments/literals without changing offsets, so braces inside them do not count.
    masked = re.sub(r'/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'',
                    lambda match: ' ' * len(match.group()), source, flags=re.S)
    match = re.search(r'^\s*(?:static\s+)?(?:void|bool)\s+' + re.escape(name)
                      + r'\s*\([^;{}]*\)\s*\{', masked, re.M)
    if not match:
        if required:
            raise AssertionError('Missing real QEMU function: ' + name)
        return ''
    depth = 1
    end = match.end()
    while end < len(masked) and depth:
        depth += (masked[end] == '{') - (masked[end] == '}')
        end += 1
    if depth:
        raise AssertionError('Unbalanced QEMU function: ' + name)
    return source[match.start():end]


class QemuSdlBackendTests(unittest.TestCase):
    def test_real_qemu_sdl_input_and_window(self):
        if not str(BUILD).isascii():
            self.skipTest('GCC requires an ASCII build path; set WCP_QEMU_BUILD_ROOT to an ASCII junction for state/qemu-window-build')
        source = BUILD / 'qemu-11.1.0/ui/sdl2.c'
        mingw = BUILD / 'msys64/mingw64'
        compiler = mingw / 'bin/gcc.exe'
        runtime = ROOT / 'runtime/qemu-11.1.0'
        required = [source, compiler, mingw / 'include/SDL2/SDL.h',
                    mingw / 'lib/libSDL2.dll.a', runtime / 'SDL2.dll']
        missing = [str(path) for path in required if not path.is_file()]
        if missing:
            self.skipTest('QEMU source/SDL toolchain unavailable: ' + ', '.join(missing)
                          + '; set WCP_QEMU_BUILD_ROOT to the prepared ASCII build root')
        original = source.read_text(encoding='utf-8')
        functions = [extract_function(original, name) for name in
                     ('sdl2_window_client_size', 'sdl2_window_resize', 'sdl2_window_create')]
        functions += [extract_function(original, name, required=False) for name in
                      ('sdl2_pointer_at_edge', 'sdl2_mouse_coords')]
        functions += [extract_function(original, name) for name in
                      ('handle_mousemotion', 'handle_mousebutton')]
        template = (ROOT / 'tests/QemuSdlRegression.c').read_text(encoding='utf-8')
        generated = template.replace('/* QEMU_FUNCTIONS */', '\n\n'.join(functions))
        env = os.environ.copy()
        env['PATH'] = os.pathsep.join([str(mingw / 'bin'),
                                      str(BUILD / 'msys64/usr/bin'), env.get('PATH', '')])
        env['SDL_VIDEODRIVER'] = 'dummy'
        env['SDL_RENDER_DRIVER'] = 'software'
        # The ASCII junction is required by GCC; keep generated artifacts there too.
        with tempfile.TemporaryDirectory(prefix='sdl-regression-', dir=BUILD) as temporary:
            c_file = Path(temporary) / 'QemuSdlRegression.c'
            executable = Path(temporary) / 'QemuSdlRegression.exe'
            c_file.write_text(generated, encoding='utf-8')
            command = [str(compiler), '-std=c11', '-O0', '-Wall', '-Wextra',
                       '-I' + str(mingw / 'include/SDL2'), str(c_file),
                       '-L' + str(mingw / 'lib'), '-lSDL2', '-lm', '-o', str(executable)]
            build = subprocess.run(command, env=env, cwd=temporary, capture_output=True,
                                   timeout=60, creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(build.returncode, 0, (build.stdout + build.stderr).decode(errors='replace'))
            runtime_env = env.copy()
            runtime_env['PATH'] = os.pathsep.join([str(runtime), env['PATH']])
            result = subprocess.run([str(executable)], env=runtime_env, cwd=temporary,
                                    capture_output=True, timeout=30,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(result.returncode, 0, (result.stdout + result.stderr).decode(errors='replace'))


if __name__ == '__main__':
    unittest.main()

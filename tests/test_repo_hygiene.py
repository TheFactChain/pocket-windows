from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import check_workspace


class RepositoryHygieneTests(unittest.TestCase):
    def test_tracked_files_are_sources_only(self):
        self.assertEqual(check_workspace.tracked_problems(ROOT), [])

    def test_curated_readme_images_and_translation_are_allowed(self):
        names = ['README.en.md'] + ['docs/assets/readme/' + name for name in (
            'launcher.png', 'connection.png', 'settings.png', 'diagnostics.png', 'desktop.png', 'files.png', 'social-preview.png')]
        with mock.patch.object(check_workspace.subprocess, 'check_output',
                               return_value=('\0'.join(names) + '\0').encode()):
            self.assertEqual(check_workspace.tracked_problems(ROOT), [])

    def test_readme_exception_does_not_allow_other_captures_or_binaries(self):
        names = ['docs/assets/readme/extra.png', 'docs/assets/readme/nested/launcher.png',
                 'docs/launcher.png', 'launcher-wpf/launcher.png', 'docs/assets/readme/app.zip',
                 'docs/assets/readme/launcher.png.exe', 'docs/assets/readme/.env', '.beads/snapshot']
        with mock.patch.object(check_workspace.subprocess, 'check_output',
                               return_value=('\0'.join(names) + '\0').encode()):
            problems = check_workspace.tracked_problems(ROOT)
        for name in names[:-1]:
            self.assertTrue(any(problem.endswith(name) for problem in problems), name)
        self.assertTrue(any('Unexpected folder' in problem and '.beads' in problem for problem in problems))

    def test_curated_readme_images_still_have_a_size_limit(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            name = 'docs/assets/readme/launcher.png'
            image = root / name
            image.parent.mkdir(parents=True)
            with image.open('wb') as output:
                output.truncate(check_workspace.MAX_TRACKED_BYTES + 1)
            with mock.patch.object(check_workspace.subprocess, 'check_output', return_value=(name + '\0').encode()):
                self.assertIn('Tracked file is larger than 3 MiB: ' + name, check_workspace.tracked_problems(root))

    def test_workspace_flags_leftovers_and_extra_generations(self):
        with tempfile.TemporaryDirectory() as folder:
            service = Path(folder) / 'Разработка/Служебное'
            for name in ('release-2026.10.23.7/public', 'release-2026.10.23.6', 'release-2026.10.24.1',
                         'compact-master-2026.10.23.7', 'slider-fix-20261023', 'Разработка-и-история'):
                (service / name).mkdir(parents=True)
            (Path(folder) / 'Разработка/Исходники/runtime/python-3.12.14/Lib/__pycache__').mkdir(parents=True)
            text = '\n'.join(check_workspace.workspace_problems(Path(folder)))
            self.assertIn('Bytecode cache inside runtime', text)
            self.assertIn('slider-fix-20261023', text)
            self.assertIn('Keep at most two folders of release', text)
            self.assertIn('release-2026.10.23.7', text.split('Build leftover')[1])
            self.assertNotIn('Разработка-и-история', text)
            self.assertNotIn('compact-master', text)


if __name__ == '__main__':
    unittest.main()

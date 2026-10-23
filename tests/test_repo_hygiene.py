from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import check_workspace


class RepositoryHygieneTests(unittest.TestCase):
    def test_tracked_files_are_sources_only(self):
        self.assertEqual(check_workspace.tracked_problems(ROOT), [])

    def test_workspace_flags_leftovers_and_extra_generations(self):
        with tempfile.TemporaryDirectory() as folder:
            service = Path(folder) / 'Разработка/Служебное'
            for name in ('release-2026.10.23.7/public', 'release-2026.10.23.6', 'release-2026.10.24.1',
                         'compact-master-2026.10.23.7', 'slider-fix-20261023', 'Разработка-и-история'):
                (service / name).mkdir(parents=True)
            text = '\n'.join(check_workspace.workspace_problems(Path(folder)))
            self.assertIn('slider-fix-20261023', text)
            self.assertIn('Keep at most two folders of release', text)
            self.assertIn('release-2026.10.23.7', text.split('Build leftover')[1])
            self.assertNotIn('Разработка-и-история', text)
            self.assertNotIn('compact-master', text)


if __name__ == '__main__':
    unittest.main()

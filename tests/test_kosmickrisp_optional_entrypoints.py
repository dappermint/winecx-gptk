import importlib.util
from pathlib import Path
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'runtime/kosmickrisp/optional_entrypoints.py'
spec = importlib.util.spec_from_file_location('optional_entrypoints', SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class OptionalEntrypointsTests(unittest.TestCase):
    def test_collect_only_marked_declarations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in module.HEADERS:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('VKAPI_ATTR void VKAPI_CALL kk_Optional(int x) '
                                'VK_ENTRY_WEAK VK_ENTRY_HIDDEN;\n'
                                'VKAPI_ATTR void VKAPI_CALL kk_Required(int x);\n')
            self.assertEqual(module.collect(root), {'schema': 1, 'symbols': ['_kk_Optional']})

    def test_missing_declarations_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / module.HEADERS[0]
            path.parent.mkdir(parents=True)
            path.write_text('VKAPI_ATTR void VKAPI_CALL kk_Required(void);')
            with self.assertRaisesRegex(ValueError, 'no optional'):
                module.collect(root)

    def test_only_known_weak_dynamic_imports_in_driver_are_optional(self):
        manifest = {'schema': 1, 'symbols': ['_kk_Optional']}
        line = '(undefined) weak external _kk_Optional (dynamically looked up)'
        self.assertTrue(module.is_optional_import(module.DRIVER, '_kk_Optional', line, manifest))
        for path, symbol, text in (
            ('Libraries/Wine/lib/wine/x86_64-unix/ntdll.so', '_kk_Optional', line),
            (module.DRIVER, '_kk_Unknown', line),
            (module.DRIVER, '_kk_Optional', '(undefined) external _kk_Optional (dynamically looked up)'),
            (module.DRIVER, '_kk_Optional', '(undefined) weak external _kk_Optional (from Foundation)'),
            (module.DRIVER, '_pipe2', '(undefined) weak external _pipe2 (from libSystem)'),
        ):
            with self.subTest(path=path, symbol=symbol, line=text):
                self.assertFalse(module.is_optional_import(path, symbol, text, manifest))


if __name__ == '__main__':
    unittest.main()

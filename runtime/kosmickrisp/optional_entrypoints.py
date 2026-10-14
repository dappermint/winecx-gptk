"""Identify Mesa's generated optional Vulkan imports for the bundle audit."""
import argparse
import json
from pathlib import Path
import re


HEADERS = (
    'src/kosmickrisp/vulkan/kk_entrypoints.h',
    'src/vulkan/runtime/vk_common_entrypoints.h',
    'src/vulkan/runtime/vk_cmd_enqueue_entrypoints.h',
    'src/vulkan/wsi/wsi_common_entrypoints.h',
)
DECLARATION = re.compile(
    r'VKAPI_CALL\s+([A-Za-z_]\w*)\([^;]*?\)\s+VK_ENTRY_WEAK\s+VK_ENTRY_HIDDEN\s*;', re.S)
DRIVER = Path('Libraries/Wine/lib/kosmickrisp/libvulkan_kosmickrisp.dylib')


def collect(build):
    symbols = set()
    for header in HEADERS:
        names = DECLARATION.findall((build / header).read_text())
        if not names:
            raise ValueError(f'no optional entrypoint declarations in {header}')
        symbols.update('_' + name for name in names)
    return {'schema': 1, 'symbols': sorted(symbols)}


def is_optional_import(library, symbol, nm_line, manifest):
    if manifest.get('schema') != 1:
        raise ValueError('unsupported optional entrypoint manifest')
    return (Path(library) == DRIVER and ' weak ' in nm_line
            and '(dynamically looked up)' in nm_line
            and symbol in manifest['symbols'])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('build', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    args.output.write_text(json.dumps(collect(args.build), indent=2) + '\n')

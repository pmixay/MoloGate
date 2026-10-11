"""Build the bundled AI-lab logos from @lobehub/icons-static-svg (MIT, https://github.com/lobehub/lobe-icons).

    npm pack @lobehub/icons-static-svg@1.95.1
    tar -xzf lobehub-icons-static-svg-1.95.1.tgz
    python scripts/build-lab-logos.py package/icons

Each logo is the lab's mono mark in light ink on the dark rounded tile Mologate draws its own mark
on, so model avatars read the same in both themes. Labs and their icons are listed in
backend/open_webui/utils/model_labs.py; output goes to backend/open_webui/assets/lab_logos/.
"""

import importlib.util
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / 'backend' / 'open_webui' / 'assets' / 'lab_logos'

TILE = 48
GLYPH = 28
TILE_RADIUS = 8.5
TILE_FILL = '#0f1016'
GLYPH_FILL = '#eceef5'

ALLOWED_TAGS = {'svg', 'title', 'path', 'ellipse', 'circle', 'rect', 'polygon', 'g'}


def load_labs() -> dict:
    spec = importlib.util.spec_from_file_location(
        'model_labs', ROOT / 'backend' / 'open_webui' / 'utils' / 'model_labs.py'
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.LABS


def build_tile(source: str, title: str) -> str:
    tags = set(re.findall(r'<([a-zA-Z][\w-]*)', source))
    if not tags <= ALLOWED_TAGS or re.search(r'\bon\w+=|href|url\(|<!|<\?', source):
        raise ValueError(f'unexpected SVG content: {sorted(tags - ALLOWED_TAGS)}')

    root = re.match(r'\s*<svg([^>]*)>', source)
    view_box = re.search(r'viewBox="([^"]+)"', root.group(1))
    min_x, min_y, width, height = (float(value) for value in view_box.group(1).split())
    fill_rule = re.search(r'fill-rule="([^"]+)"', root.group(1))

    inner = source[root.end() : source.rindex('</svg>')]
    inner = re.sub(r'<title>.*?</title>', '', inner, flags=re.S).strip()

    scale = GLYPH / max(width, height)
    offset_x = (TILE - width * scale) / 2 - min_x * scale
    offset_y = (TILE - height * scale) / 2 - min_y * scale
    fill_rule_attr = f' fill-rule="{fill_rule.group(1)}"' if fill_rule else ''

    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {TILE} {TILE}" width="{TILE}" height="{TILE}">'
        f'<title>{title}</title>'
        f'<rect width="{TILE}" height="{TILE}" rx="{TILE_RADIUS}" fill="{TILE_FILL}"/>'
        f'<g transform="translate({offset_x:.3f} {offset_y:.3f}) scale({scale:.4f})" '
        f'fill="{GLYPH_FILL}" color="{GLYPH_FILL}"{fill_rule_attr}>{inner}</g>'
        '</svg>\n'
    )


def main(icons_dir: str) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for lab_id, (name, icon) in load_labs().items():
        source = (Path(icons_dir) / f'{icon}.svg').read_text()
        (OUT_DIR / f'{lab_id}.svg').write_text(build_tile(source, name))
        print(f'{lab_id}.svg  <- {icon}.svg')


if __name__ == '__main__':
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])

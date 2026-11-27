from pathlib import Path
import json
root=Path(__file__).resolve().parents[1]
v=json.loads((root/'version.json').read_text())
m=json.loads((root/'extension/manifest.json').read_text())
assert m['version']=='1.0.11'
assert m['version'] in '\n'.join(v['highlights']) and m['version'] in '\n'.join(v['highlights_en'])
assert '手动' in '\n'.join(v['highlights']) and 'every browser profile' in '\n'.join(v['highlights_en'])
html=(root/'admin.html').read_text()
assert 'id="extensionUpgradeNotice"' in html and '"extension-upgrade-notice":' in html
assert html.index('id="tab-cookie"') < html.index('id="extensionUpgradeNotice"') < html.index('id="tab-tasks"')
assert 'href="/admin/extension"' in html
setup = html.split('id="chatProviderTemplate"', 1)[1].split('</template>', 1)[0]
assert 'chrome://inspect/#remote-debugging' in setup and '--user-data-dir' not in setup
assert "releases/download/" not in html.split('id="extensionUpgradeNotice"', 1)[1].split('</div>', 1)[0]
assert 'id="extensionVersion"' in (root/'extension/popup.html').read_text()
assert '@version      1.1.2' in (root/'tools/muse2api_cookie_importer.user.js').read_text()
print('PASS: bilingual upgrade notice, version badge, release instructions and download link')

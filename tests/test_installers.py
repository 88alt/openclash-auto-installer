#!/usr/bin/env python3
"""Offline tests of the actual shell functions (no router changes or network)."""
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def function(script, name):
    source = (ROOT / script).read_text()
    match = re.search(r'^' + re.escape(name) + r'\(\) \{\n.*?^\}', source, re.M | re.S)
    if not match:
        raise AssertionError(f'{script}: missing {name}')
    return match.group()


def shell(code, **env):
    return subprocess.run(['sh', '-c', 'set -eu\n' + code], text=True,
                          capture_output=True, env={**os.environ, **env})


class InstallerTests(unittest.TestCase):
    def test_passwall_package_generation(self):
        for plugin in ('passwall', 'passwall2'):
            fn = 'normalize_release_for_' + plugin
            for release, manager, expected in (
                ('25.12.5', 'apk', '25.12'),
                ('24.10.8', 'opkg', '24.10'),
                ('25.12.2', 'opkg', '24.10'),  # KWRT issue #10
                ('23.05.6', 'opkg', '23.05'),
                ('22.03.7', 'opkg', '22.03'),
                ('SNAPSHOT', 'apk', 'snapshots'),
                ('GDQ', 'opkg', ''),
            ):
                with self.subTest(plugin=plugin, release=release, manager=manager):
                    result = shell(function(plugin + '.sh', fn) +
                                   f'\n{fn} "$REL" "$MGR"', REL=release, MGR=manager)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout, expected)

    def test_nikki_supported_branches(self):
        for release, expected in [('25.12.5', 'openwrt-25.12'),
                                  ('24.10.8', 'openwrt-24.10'),
                                  ('SNAPSHOT', 'SNAPSHOT'), ('23.05-SNAPSHOT', '')]:
            result = shell(function('nikki.sh', 'detect_nikki_branch') +
                           '\ndetect_nikki_branch', REL_RAW=release)
            self.assertEqual(result.stdout, expected)

    def test_nikki_download_failures_do_not_execute(self):
        for behavior in ('exit 8', 'exit 0'):
            with tempfile.TemporaryDirectory() as tmp:
                downloader = Path(tmp) / 'wget'
                downloader.write_text('#!/bin/sh\n' + behavior + '\n')
                downloader.chmod(0o755)
                result = shell('die() { echo "$*" >&2; exit 1; }\n' +
                               'TMP_SCRIPT=""\ntrap \'[ -z "$TMP_SCRIPT" ] || rm -f "$TMP_SCRIPT"\' EXIT\n' +
                               function('nikki.sh', 'run_official_script') +
                               '\nrun_official_script https://example.invalid/feed.sh\necho FALSE_SUCCESS',
                               PATH=tmp + ':' + os.environ['PATH'])
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn('FALSE_SUCCESS', result.stdout)

    def test_passwall_asset_selection_json_and_html_fallback(self):
        for plugin in ('passwall', 'passwall2'):
            names = ([f'23.05-24.10_luci-app-{plugin}_26.10.4-r1_all.ipk',
                      f'25.12+_luci-app-{plugin}-26.10.4-r1.apk'] if plugin == 'passwall'
                     else [f'luci-app-{plugin}_26.10.1-r2_all.ipk',
                           f'luci-app-{plugin}-26.10.1-r2.apk'])
            urls = [f'https://github.com/Openwrt-Passwall/openwrt-{plugin}/releases/download/test/{n}'
                    for n in names]
            code = (function(plugin + '.sh', 'github_release_prefix') + '\n'
                    if plugin == 'passwall' else '')
            code += function(plugin + '.sh', 'find_github_pkg_url')
            code += '\nfind_github_pkg_url "$PKG" "$EXT"'
            for manager, ext, index in [('opkg', 'ipk', 0), ('apk', 'apk', 1)]:
                for fallback in (False, True):
                    result = shell(code, PKG='luci-app-' + plugin, EXT=ext,
                                   PKG_MGR=manager, SUPPORTED_RELEASE='24.10' if index == 0 else '25.12',
                                   GH_RELEASE_JSON='' if fallback else json.dumps({'assets': [
                                       {'browser_download_url': u} for u in urls]}),
                                   GH_RELEASE_ASSET_URLS='\n'.join(urls) if fallback else '')
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout.strip(), urls[index])

    def test_revision_updates_and_unversioned_release_revision(self):
        code = (function('check-updates.sh', 'normalize_version') + '\n' +
                function('check-updates.sh', 'print_result') +
                '\nprint_result test "$INST" "$LATEST_TEST"')
        for installed, latest, expected in (
            ('26.10.1-r1', '26.10.1-2', '有新版本可更新'),
            ('26.10.1-r2', '26.10.1-2', '已是最新'),
            ('1.26.1-r1', 'v1.26.1', '已是最新'),
        ):
            result = shell(code, INST=installed, LATEST_TEST=latest)
            self.assertIn(expected, result.stdout)

    def test_daed_apk_compares_openwrt_build_not_generic_release(self):
        code = '''
PKG_MGR=apk
TMP_ROOT=/tmp
DAED_RELEASES_API=https://example.invalid/generic
DAED_RELEASES_PAGE=https://example.invalid/generic-page
daed() { echo daed-671e65d_wing-dc50308_core-caa6f5e; }
LUCI_DAED_API=https://example.invalid/openwrt-build
get_installed_apk_version() {
    case "$1" in daed) echo 2026.07.31-r1;; *) echo 1.4-r1;; esac
}
fetch_latest_tag_jsonfilter() { echo daed_2026.07.31-r1; }
fetch_url() { echo WRONG_GENERIC_SOURCE >&2; return 1; }
print_result() { printf '%s|%s|%s\\n' "$1" "$2" "$3"; }
print_result_no_compare() { :; }
'''
        result = shell(code + function('check-updates.sh', 'check_daed') + '\ncheck_daed')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('2026.07.31-r1|2026.07.31-r1', result.stdout)
        self.assertNotIn('WRONG_GENERIC_SOURCE', result.stderr)

    def test_wrong_signing_key_stops_before_install(self):
        for plugin in ('passwall', 'passwall2'):
            code = '''
need_cmd() { command -v "$1" >/dev/null; }
register_tmp() { :; }
download_file() { printf 'wrong key' > "$2"; }
die() { echo "$*" >&2; exit 1; }
trap '[ -z "${key_tmp:-}" ] || rm -f "$key_tmp"' EXIT
SF_BASE=https://example.invalid
'''
            result = shell(code + function(plugin + '.sh', 'install_signed_apk') +
                           '\ninstall_signed_apk\necho FALSE_SUCCESS')
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('公钥摘要不匹配', result.stderr)
            self.assertNotIn('FALSE_SUCCESS', result.stdout)


if __name__ == '__main__':
    unittest.main()

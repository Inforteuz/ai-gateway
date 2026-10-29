"""Exercise the actual launcher with a recording browser instead of Chrome."""
import json
import os
from pathlib import Path
import subprocess

import pytest


LAUNCHER = Path(__file__).resolve().parents[2] / 'docker/cluster/launch-chrome.sh'


def launch(tmp_path, graphics):
    binary = tmp_path / 'bin'
    binary.mkdir()
    recorder = binary / 'google-chrome'
    recorder.write_text('#!/usr/bin/env python3\nimport json,os,sys\n'
                        'open(os.environ["CHROME_ARGS_FILE"], "w").write(json.dumps(sys.argv[1:]))\n')
    recorder.chmod(0o755)
    output = tmp_path / 'args.json'
    env = {**os.environ, 'PATH': str(binary) + os.pathsep + os.environ['PATH'],
           'SF_CLUSTER_PROFILE_DIR': str(tmp_path / 'profiles'),
           'SF_CLUSTER_PROXIES_FILE': str(tmp_path / 'missing-proxies'),
           'SF_CHROME_SANDBOX': '0', 'SF_CHROME_BLOCK_TRACKERS': '0',
           'CHROME_ARGS_FILE': str(output)}
    env.pop('SF_CHROME_GRAPHICS', None)
    if graphics is not None:
        env['SF_CHROME_GRAPHICS'] = graphics
    result = subprocess.run(['bash', str(LAUNCHER), '3', 'gemini'], env=env,
                            capture_output=True, text=True, timeout=10)
    return result, json.loads(output.read_text()) if output.exists() else None


@pytest.mark.parametrize('graphics', [None, 'basic'])
def test_basic_mode_disables_software_3d_but_preserves_debugging_and_profile(tmp_path, graphics):
    result, args = launch(tmp_path, graphics)
    assert result.returncode == 0, result.stderr
    assert '--disable-gpu' in args and '--disable-software-rasterizer' in args
    assert not any('swiftshader' in arg for arg in args)
    assert '--remote-debugging-port=9225' in args
    assert '--remote-debugging-address=127.0.0.1' in args
    assert f'--user-data-dir={tmp_path}/profiles/profile_3' in args
    assert args[-1] == 'https://gemini.google.com/app'


def test_software_gl_is_an_explicit_compatible_option(tmp_path):
    result, args = launch(tmp_path, 'software-gl')
    assert result.returncode == 0, result.stderr
    assert '--use-angle=swiftshader' in args and '--enable-unsafe-swiftshader' in args
    assert '--disable-gpu' not in args and '--disable-software-rasterizer' not in args


def test_unknown_graphics_mode_does_not_silently_change_rendering(tmp_path):
    result, args = launch(tmp_path, 'typo')
    assert result.returncode != 0 and args is None
    assert 'invalid SF_CHROME_GRAPHICS' in result.stderr

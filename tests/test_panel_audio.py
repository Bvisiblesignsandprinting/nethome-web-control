import json
import subprocess
from unittest.mock import patch

import imageio_ffmpeg
import pytest
from fastapi import HTTPException

from app.music import MusicStorage
from app.panel_audio import convert_for_panel, PROFILE


def test_real_audio_conversion_reduces_size_and_preserves_duration(tmp_path):
    source = tmp_path / 'tone.mp3'
    exe = imageio_ffmpeg.get_ffmpeg_exe()
    subprocess.run([exe, '-v', 'error', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=8',
                    '-ac', '2', '-ar', '44100', '-b:a', '192k', '-y', str(source)], check=True)
    output = convert_for_panel(source.read_bytes())
    assert len(output) < source.stat().st_size / 3
    target = tmp_path / 'small.mp3'
    target.write_bytes(output)
    inspected = subprocess.run([exe, '-hide_banner', '-i', str(target), '-f', 'null', '-'],
                               capture_output=True, text=True, check=True).stderr
    assert '22050 Hz, mono' in inspected
    assert '00:00:08.' in inspected


@pytest.mark.parametrize('data', [b'', b'not an mp3'])
def test_invalid_audio_is_not_uploaded(data):
    with pytest.raises((ValueError, subprocess.CalledProcessError)):
        convert_for_panel(data)


def test_panel_cached_copy_uses_private_profile_and_leaves_original_unchanged():
    storage = MusicStorage()
    storage.bucket = 'nethome-music'
    storage.origin = 'https://example.supabase.co'
    name = 'a' * 32 + '_Song.mp3'
    calls = []
    def request(method, path, payload=None):
        calls.append((method, path, payload))
        if path.startswith('/object/sign/'):
            return {'signedURL': '/object/sign/nethome-music/cached.mp3?token=test'}
        return {'id': 'original-id', 'updated_at': '2026-10-26', 'size': 12345}
    with patch.object(storage, 'check_bucket'), patch.object(storage, 'request', request), patch('app.music.convert_for_panel') as convert:
        result = storage.playback(name, panel=True)
    assert result['audio_profile'] == PROFILE and result['bitrate_kbps'] == 32
    assert result['playback_device'] == 'panel'
    assert any('/panel-audio/' in path for _, path, _ in calls)
    assert all(method != 'DELETE' for method, _, _ in calls)
    convert.assert_not_called()


def test_bad_track_path_is_rejected_before_storage_access():
    storage = MusicStorage()
    with patch.object(storage, 'request') as request:
        with pytest.raises(HTTPException):
            storage.playback('../secret.mp3', panel=True)
        request.assert_not_called()

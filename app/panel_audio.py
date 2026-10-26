"""Create bounded, private low-bandwidth copies; never modify original tracks."""
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory

MAX_INPUT_BYTES = 25 * 1024 * 1024
PROFILE = 'mono-32k-22050-v1'


def convert_for_panel(data: bytes) -> bytes:
    # A packaging issue must not prevent unrelated app routes from starting.
    import imageio_ffmpeg
    if not data or len(data) > MAX_INPUT_BYTES:
        raise ValueError('Invalid track size')
    with TemporaryDirectory(prefix='nethome-audio-') as folder:
        source, target = Path(folder) / 'source.mp3', Path(folder) / 'panel.mp3'
        source.write_bytes(data)
        subprocess.run([
            imageio_ffmpeg.get_ffmpeg_exe(), '-nostdin', '-hide_banner', '-loglevel', 'error',
            '-protocol_whitelist', 'file,pipe', '-f', 'mp3', '-i', str(source),
            '-map', '0:a:0', '-vn', '-map_metadata', '-1', '-ac', '1', '-ar', '22050',
            '-codec:a', 'libmp3lame', '-b:a', '32k', '-threads', '1',
            '-y', str(target),
        ], check=True, timeout=30, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        result = target.read_bytes()
        if not result or len(result) > MAX_INPUT_BYTES:
            raise ValueError('Invalid converted track')
        return result

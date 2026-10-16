"""Run once with server-only music storage variables in the environment."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import HTTPException
from app.music import MAX_FILE_BYTES, storage


def main():
    if not storage.configured:
        raise SystemExit('Configure NETHOME_MUSIC_STORAGE_URL and NETHOME_MUSIC_STORAGE_KEY first.')
    try:
        storage.request('GET', f'/bucket/{storage.bucket}')
    except HTTPException as error:
        if error.status_code != 404:
            raise SystemExit(error.detail) from None
        storage.request('POST', '/bucket', {
            'id': storage.bucket, 'name': storage.bucket, 'public': False,
            'file_size_limit': MAX_FILE_BYTES, 'allowed_mime_types': ['audio/mpeg'],
        })
    # Existing buckets are checked, never silently made public or reconfigured.
    storage.check_bucket()
    print('Private MP3 storage is configured. No database or AC settings changed.')


if __name__ == '__main__':
    main()

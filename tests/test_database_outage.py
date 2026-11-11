"""Login must remain available without pretending database controls work."""
import pytest
from fastapi.testclient import TestClient
from psycopg import OperationalError
from psycopg_pool import PoolTimeout
from app import main


@pytest.mark.parametrize('failure', [OperationalError, PoolTimeout])
def test_database_errors_return_safe_unavailable(failure):
    import asyncio
    response = asyncio.run(main.database_unavailable(None, failure('private connection string')))
    assert response.status_code == 503
    assert response.headers['retry-after'] == '60'
    assert b'private connection' not in response.body
    assert b'"database_available":false' in response.body


def test_lifespan_continues_after_failed_database_probe(monkeypatch):
    import asyncio
    from contextlib import asynccontextmanager
    calls = []
    def fail():
        raise OperationalError('offline')
    @asynccontextmanager
    async def session():
        yield
    monkeypatch.setattr(main, 'init_db', fail)
    monkeypatch.setattr(main, 'apply_requested_smart_room_schedule', lambda: calls.append('profile'))
    monkeypatch.setattr(main.nethome_mcp.session_manager, 'run', session)
    async def run():
        async with main.lifespan(main.app):
            calls.append('started')
    asyncio.run(run())
    assert calls == ['started']

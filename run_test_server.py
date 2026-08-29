"""Local smoke-test ASGI entrypoint; not for production deployment."""

from openjarvis.server.app import create_app


class TestEngine:
    def health(self) -> bool:
        return True


app = create_app(TestEngine(), "test", engine_name="test", api_key="test-key")

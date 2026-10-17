"""Start the local workspace with source defaults and server-side OpenAI credentials."""

from .config import Settings


def main():
    import uvicorn

    settings = Settings.from_env()
    uvicorn.run("analyst.app:app", host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()

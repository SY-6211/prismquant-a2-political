import uvicorn

from app.core.config import PROJECT_ROOT, Settings


def main() -> None:
    settings = Settings()
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.environment == "development",
        reload_dirs=[str(PROJECT_ROOT / "app")]
        if settings.environment == "development"
        else None,
    )


if __name__ == "__main__":
    main()

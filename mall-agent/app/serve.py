import uvicorn

from app.runtime import run


def server_config():
    return uvicorn.Config(
        "app.main:create_app",
        factory=True,
        host="127.0.0.1",
        port=8099,
        log_level="info",
        timeout_keep_alive=15,
    )


if __name__ == "__main__":
    run(uvicorn.Server(server_config()).serve())

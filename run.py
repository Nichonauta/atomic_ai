import uvicorn

from app.config import settings

if __name__ == "__main__":
    uvicorn.run("app.main:app", host=settings.proxy_host, port=settings.proxy_port, reload=False)

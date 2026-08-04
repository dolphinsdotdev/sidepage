from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(title="Fixture API")


class Echo(BaseModel):
    message: str


@app.get("/")
def root():
    return {"status": "ok"}


@app.post("/echo")
def echo(payload: Echo):
    return {"echo": payload.message}


if __name__ == "__main__":
    # Deliberately hardcodes a port, like real-world FastAPI apps often do —
    # this fixture exists specifically to prove sidepage launches via
    # `uvicorn <module>:<app> --port <allocated>` instead of running this
    # script directly, which would otherwise silently ignore the port
    # sidepage actually allocated.
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=9999)

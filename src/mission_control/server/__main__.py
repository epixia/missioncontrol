from __future__ import annotations

import uvicorn


def main() -> None:
    uvicorn.run("mission_control.server.app:app", host="127.0.0.1", port=8420, reload=False)


if __name__ == "__main__":
    main()

"""Start one backend worker on the hosting platform's assigned port."""

import os

from vif.serve.server import run


def main():
    if os.environ.get("VIF_ONLINE") != "1":
        raise ValueError("Cloud deployment requires VIF_ONLINE=1")
    if len(os.environ.get("VIF_OPERATOR_CODE", "")) < 16:
        raise ValueError("Set VIF_OPERATOR_CODE to at least 16 characters in the hosting dashboard")
    port = int(os.environ.get("PORT", "8000"))
    if not 1 <= port <= 65535:
        raise ValueError("PORT must be between 1 and 65535")
    run(host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()

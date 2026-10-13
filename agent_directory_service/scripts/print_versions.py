from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version


PACKAGES = [
    "a2a-sdk",
    "agntcy-dir",
    "protobuf",
    "pydantic",
    "httpx",
]


def main() -> None:
    for package in PACKAGES:
        try:
            value = version(package)
        except PackageNotFoundError:
            value = "not installed"
        print(f"{package}={value}")


if __name__ == "__main__":
    main()

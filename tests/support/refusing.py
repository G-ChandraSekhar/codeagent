"""Test-only: exceptions that refuse the structured-evidence attributes
(`codeagent_*`) on read and/or write, by raising a given exception."""

from __future__ import annotations


def refusing(base: type, raised: BaseException, *, on_read: bool = True, on_write: bool = True) -> BaseException:
    class Refusing(base):
        def __getattribute__(self, name):
            if on_read and name.startswith("codeagent_"):
                raise raised
            return super().__getattribute__(name)

        def __setattr__(self, name, value):
            if on_write and name.startswith("codeagent_"):
                raise raised
            super().__setattr__(name, value)

    return Refusing("SENTINEL-refusing-/secret/path")


REFUSALS = (
    lambda: AttributeError("refused"),
    lambda: RuntimeError("SENTINEL-refused"),
    lambda: KeyboardInterrupt("SENTINEL-refused"),
)

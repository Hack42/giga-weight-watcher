# Contributing

Bug reports, protocol captures, documentation fixes, and code contributions
are welcome.

## Development setup

```console
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
python -m unittest -v
```

Please include tests for protocol changes and state-machine behavior. Hardware
tests should identify the scale revision, USB adapter VID/PID, serial settings,
and a redacted hexadecimal packet sample.

Do not test automatic PAUSE behavior against an active printer unless you have
confirmed that interrupting the print is safe.

By contributing, you agree that your contribution is licensed under the BSD
2-Clause License used by this repository.

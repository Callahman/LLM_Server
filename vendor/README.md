# Vendored wheels

Prebuilt wheels for dependencies whose upstream sources are no longer
available.

- `pymumble-1.7-*.whl` — pymumble 1.7 (modern SSLContext API, `client_type`
  support). The original source (`steff85/pymumble` @ `1.7`) now 404s on
  GitHub, and the PyPI release / azlux fork still use `ssl.wrap_socket`
  (removed in Python 3.12). This wheel is a pure-Python rebuild of the
  installed 1.7 package, so a single `py3-none-any` wheel covers every
  platform (Pi aarch64 + tower x86_64, Python 3.13/3.14).

The `requirements.txt` files reference this directory via
`--find-links=../vendor` and pin `pymumble==1.7`; pip finds the wheel here
and installs it without building.

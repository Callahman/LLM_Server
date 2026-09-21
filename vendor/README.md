# Vendored wheels

Prebuilt wheels for dependencies whose upstream sources are no longer
available.

- `pymumble-1.7*.whl` — pymumble 1.7 (modern SSLContext API, `client_type`
  support). The original source (`steff85/pymumble` @ `1.7`) now 404s on
  GitHub; the PyPI release and the azlux fork still use `ssl.wrap_socket`
  (removed in Python 3.12). Each machine's 1.7 wheel was built locally during
  the original install and is committed here; pip selects the wheel matching
  this machine's platform tags.

The `requirements.txt` files reference this directory via
`--find-links=../vendor` and pin `pymumble==1.7`; pip finds the matching wheel
here and installs it without building.

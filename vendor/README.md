# Vendored sources

Source artifacts for dependencies whose upstream sources are no longer
available.

- `pymumble-1.7.zip` — pymumble 1.7 (modern SSLContext API, `client_type`
  support). The original source (`steff85/pymumble` @ `1.7`) now 404s on
  GitHub; the PyPI release and the azlux fork still use `ssl.wrap_socket`
  (removed in Python 3.12). This zip is the original GitHub archive,
  recovered from a machine's pip HTTP cache. It is
  architecture-independent — pip builds a wheel for the local platform at
  install time.

The `requirements.txt` files reference this directory via
`--find-links=../vendor` and pin `pymumble==1.7`; pip finds the zip there
and builds it for the local machine.

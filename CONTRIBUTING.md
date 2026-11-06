# Contributing

Bug reports, documentation improvements and small, tested changes are welcome.

## Development

Use Python 3.11 or newer in a virtual environment, then install from a checkout:

```sh
python -m pip install -e .
python -m unittest discover -s tests -v
python -m iclr_alert --help
```

All automated tests use mocked service responses. They do not log in to
OpenReview or send a real Bark notification. Do not add real credentials to
tests, fixtures, screenshots, CI variables or public issues.

## Pull requests

- Explain the concrete problem and resulting behavior.
- Keep the change focused and include meaningful tests for changed behavior.
- Preserve the distinction between missing reviews, unreadable fields,
  authentication failures, access verification and transport failures.
- Respect OpenReview permissions and rate limits. Do not add CAPTCHA solving,
  browser-cookie extraction, client impersonation or access-control bypasses.
- Preserve durable notification state and avoid claiming exactly-once delivery.
- Update both README languages when the public setup commands change.

## Useful bug reports

Include the operating system, Python version, command, safe error code and a
minimal reproduction with synthetic data. Redact paper links if they are not
public, and remove tokens, device keys, passwords, request headers and database
contents. Security issues are covered in [SECURITY.md](SECURITY.md).

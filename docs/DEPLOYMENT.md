# Deployment guide / 部署指南

This guide covers persistent local, Docker, and Railway workers. For the Chinese getting-started guide, see [README.md](../README.md); for the English overview, see [README.en.md](../README.en.md).

The application is a background worker, not a web server. It needs outbound HTTPS access to OpenReview and the Bark public service, a writable durable state directory, and one running process per state directory. It does not require a public domain or inbound port.

## 1. Choose a deployment

| Option | Suitable for | Requirement |
| --- | --- | --- |
| Local Python | Trying the tool or an always-on workstation | The computer stays online |
| Docker / Compose | An existing server or NAS | A durable volume and environment secrets |
| Railway from GitHub | A hosted worker without your own server | Your Railway account, available resources, and durable volume |
| Source bundle | Advanced deployments through tools that accept an image and variables | The verified runtime bundle and bootstrap variables |

A hosting trial is not a long-term availability guarantee. Review the provider's current pricing and account limits before enabling a worker. This project does not purchase or upgrade plans.

## 2. Prepare the target and Bark

Run these in your checkout after installing it into a virtual environment:

```text
python -m iclr_alert paper https://openreview.net/forum?id=YOUR_FORUM_ID
python -m iclr_alert bark-key
python -m iclr_alert bark-test
```

Replace the paper placeholder. Use the **Device Key from the Bark app's push URL**, not the APNs Device Token in its settings. The current application targets `https://api.day.app`.

The test command sends one test notification. Confirm the test ID on the phone. HTTP/provider success alone is not proof that the iPhone displayed it.

<a id="session-setup"></a>

## 3. Prepare a normal OpenReview session

Authentication follows the normal [OpenReview API login/MFA protocol](https://github.com/openreview/openreview-py/blob/master/openreview/api/client.py). Do not use browser-cookie extraction or attempt to automate a verification challenge.

### Windows: protected local session

```text
python -m iclr_alert login-gui
python -m iclr_alert probe
python -m iclr_alert cloud-config
```

Configure the paper before login. A terminal alternative is `python -m iclr_alert login`. Password and OTP prompts are hidden; they are not persisted. The normal login flow saves the issued session through Windows DPAPI.

`cloud-config` writes `.local/railway-variables.json` unless you override `ICLR_CONFIG_DIR`. It contains the issued Token and Bark Key in plaintext for deliberate secret configuration. Keep it private and out of source control. The command does not upload anything.

Windows DPAPI session files are tied to the local user/machine context. They are not portable to Linux containers. Supply the issued session as a hosting Secret instead.

### macOS / Linux: authenticate in memory

The CLI's persistent login flow is Windows-only. The platform-independent `auth.login` function can authenticate normally without persisting a session.

After configuring the paper and Bark Key, save the following as `.local/run_session.py` and run it with the virtual environment's Python. The project must be installed, for example with `pip install -e .`.

```python
import getpass
import os

from iclr_alert.auth import login
from iclr_alert.runner import run

session = login(
    input("OpenReview account: ").strip(),
    getpass.getpass("Password: "),
    otp_provider=lambda method: getpass.getpass(f"{method} code: "),
)
os.environ["OPENREVIEW_TOKEN"] = session["token"]
run(watch=True)
```

```bash
python .local/run_session.py
```

This starts real monitoring and may send score notifications. The session is held in that process's environment, not exported to the parent shell or saved for the next run. Stop with Ctrl+C. Run the login again for a new session.

Do not pipe the script into `python -` when using its interactive account prompt: the script would occupy standard input. Save it as a file, as shown above.

### macOS / Linux: prepare hosting variables

For a cloud worker, you need a normal session in a hosting Secret. This alternative creates the same variable JSON as the Windows export. Save it as `.local/prepare_cloud.py`, then run it interactively; no credential is printed.

```python
import getpass
import json
import os

from iclr_alert.auth import login
from iclr_alert.settings import settings
from iclr_alert.web import PRIVATE_DIR

session = login(
    input("OpenReview account: ").strip(),
    getpass.getpass("Password: "),
    otp_provider=lambda method: getpass.getpass(f"{method} code: "),
)
os.environ["OPENREVIEW_TOKEN"] = session["token"]
config = settings()
if not config["paper"] or not config["bark_key"]:
    raise SystemExit("Configure the paper and Bark Key first.")

variables = {
    "OPENREVIEW_PAPER": config["paper"],
    "OPENREVIEW_TOKEN": session["token"],
    "BARK_DEVICE_KEY": config["bark_key"],
    "CHECK_INTERVAL_SECONDS": str(config["interval"]),
    "DATA_DIR": "/data",
}
PRIVATE_DIR.mkdir(parents=True, exist_ok=True)
target = PRIVATE_DIR / "railway-variables.json"
descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
os.fchmod(descriptor, 0o600)
with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
    json.dump(variables, stream, indent=2)
print("Private deployment variables written to:", target)
```

```bash
python .local/prepare_cloud.py
```

Transfer only the required variable values to your hosting provider's private variables interface. Do not upload the password, private directory, SQLite database, or DPAPI file as application source.

### Session limitations

The normal login request asks for up to one week of session validity. The server determines the actual validity and can revoke the session earlier. This project does not retain passwords or authenticator secrets for automatic renewal.

A successfully issued Token does not guarantee that every network or cloud IP can access the paper. Always verify the deployed worker's actual API outcome. If a challenge remains, use the official verification flow or contact OpenReview; do not treat a challenge as an empty review list.

## 4. Docker / Compose

Copy the supplied example and edit it locally:

```bash
cp cloud.env.example .env
```

The file needs your paper, a currently valid session, and the Bark Device Key. `.env` is ignored by Git. The application does not read it by itself; the container tooling loads it explicitly.

### Compose

```bash
docker compose up -d --build
docker compose logs -f
docker compose stop
```

The supplied Compose file loads `.env`, mounts durable state at `/data`, and avoids unconditional restarts after a human-action pause. Do not add a public port just to make the background process look like a web application.

### Plain Docker

```bash
docker build -t iclr-alert:local .
docker volume create iclr-alert-state
docker run -d --name iclr-alert \
  --env-file .env \
  -v iclr-alert-state:/data \
  iclr-alert:local
docker logs -f iclr-alert
```

Retain the named volume. Deleting it loses snapshots and accepted-event history, so previously visible scores can be treated as new after a fresh deployment. If you use a bind mount instead, ensure the container can write to it.

To update environment values, recreate the container with the updated values and the same volume. Restarting an existing plain Docker container does not replace the environment originally supplied at creation.

## 5. Railway from your GitHub repository

The standard route uses the repository's `Dockerfile`; the project does not need a custom public image.

1. Fork the repository to your account.
2. Create a Railway project and a service from that GitHub repository. Ensure Railway can access the repository.
3. Use the repository's Dockerfile and `railway.json`. The worker command is `python -m iclr_alert watch`.
4. Configure the following private variables:

   | Variable | Value |
   | --- | --- |
   | `OPENREVIEW_PAPER` | Your OpenReview URL or forum ID |
   | `OPENREVIEW_TOKEN` | Your normally issued session Token |
   | `BARK_DEVICE_KEY` | The Key in the Bark app's push URL |
   | `CHECK_INTERVAL_SECONDS` | `120`, or another integer from 60 to 86400 |
   | `DATA_DIR` | `/data` |

5. Create a persistent volume and mount it at `/data`.
6. Keep one running instance. Disable service sleeping: this worker cannot be awakened by a scheduled inbound browser request.
7. Do not add an HTTP healthcheck path unless you implement a real HTTP endpoint. This application does not listen on a port.
8. Deploy, then inspect runtime logs rather than relying only on the deployment badge.

A healthy check resembles the following; the timestamp and counts here are illustrative:

```text
... status=ok code=- push=none reviews=0 scored=0
```

`reviews=0` means no matching official reviews are visible to the current identity in that check. It is not evidence of the venue's overall release state. If scores were already visible, the first successful check can send a real notification.

Railway's official references: [Dockerfiles](https://docs.railway.com/builds/dockerfiles), [config as code](https://docs.railway.com/config-as-code/reference), [volumes](https://docs.railway.com/volumes), and [sleeping behavior](https://docs.railway.com/deployments/serverless).

## 6. Advanced source-bundle deployment

Some deployment tools can configure an existing public image and environment variables but cannot upload a local Docker build context. The optional source-bundle route supports that case. Prefer the normal Dockerfile/GitHub workflow when it is available.

```bash
python bundle_cloud.py
```

The command creates `.local/railway-source-variables.json`, containing source-related variables only:

- `ICLR_SOURCE_B64`: gzip-compressed JSON mapping source paths to base64 file bytes.
- `ICLR_SOURCE_SHA256`: SHA-256 of the decoded gzip bytes.
- `ICLR_BOOTSTRAP_B64`: the `bootstrap_cloud.py` loader.
- `PYTHONUNBUFFERED` and `PYTHONDONTWRITEBYTECODE`.

The runtime package includes only the monitoring modules, not local credentials, personal state, GUI assets, or tests. Each generated variable must stay within the provider's size limit; the generator enforces the limit used by this integration.

With the official public `python:3.12-slim` image, the start command is:

```text
python -c "import base64,os; exec(compile(base64.b64decode(os.environ['ICLR_BOOTSTRAP_B64']), 'bootstrap_cloud.py', 'exec'))"
```

Also supply the separate private authentication/push variables and `/data` volume from the standard deployment. The loader validates the source digest, allowed paths, file counts, and size limits before writing files, then starts the worker. A SHA-256 check establishes consistency of that bundle; it is not an independent signature or substitute for reviewing the source and controlling access to deployment variables.

Update the source, digest, and bootstrap variables together after running tests. The worker is still the same Python application. This route does not make authentication challenges disappear or eliminate normal hosting charges.

## 7. Maintenance and recovery

| Symptom | Action |
| --- | --- |
| `unauthorized` / `needs_auth` | Log in normally, update the deployed session Secret, and start a fresh worker |
| `challenge_required` / `access_verification` | Use the official manual verification path; confirm supported API access before resuming |
| `network_error` / service error | The worker backs off; inspect provider/OpenReview connectivity if it persists |
| `bark_rejected` or pending push | Check the App Key and Bark service; pending events are retained for later successful checks |
| `already_running` | Stop the duplicate process or give independent workers different state directories |
| `storage_error` | Check the state-volume mount, permissions, free space, and persistence |
| Trial or quota exhaustion | Review hosting-account limits; the repository cannot extend credits or purchase service |

An authentication/human-verification pause exits normally so an `ON_FAILURE` restart policy does not create a verification loop. Updating a Secret is not sufficient if the platform does not restart/redeploy the worker automatically; check that a new process is actually running.

The worker attempts a deduplicated status alert before pausing. That alert can also fail if the Bark service or network is unavailable, so keep access to your provider's process state and logs.

SQLite stores snapshots and an outbox before transmission. Accepted events remain accepted across restarts. An ambiguous timeout can still cause a repeated delivery; retries reuse the event ID, without an exactly-once guarantee. Run one process per database.

## 8. Keep private data out of public repositories

Do not commit `.env`, `.local`, Tokens, Keys, databases, provider exports, or personal deployment records. Custom configuration/data locations also need protection. Use the hosting provider's secret variables and retain only source and placeholder examples in Git.

Notifications currently include paper titles, reviewer labels, ratings, and confidence through the Bark public service and push chain. This implementation does not provide end-to-end message encryption. See [SECURITY.md](../SECURITY.md) and [Bark's official documentation](https://github.com/Finb/Bark).

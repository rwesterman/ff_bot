# DigitalOcean deployment

Production runs as one Docker Compose service on a DigitalOcean Droplet. GitHub Actions tests the repository, builds an
immutable container image, publishes it to GitHub Container Registry (GHCR), and connects to the Droplet over SSH to
recreate the bot from that exact image.

The bot opens outbound HTTPS and Discord Gateway connections. It is not an HTTP server, so it does not need Caddy,
Nginx, or inbound ports 80 and 443. The only expected inbound port is SSH on TCP 22 for administration and deployment.

The SQLite database is stored on the Droplet at `/opt/ff-bot/data/chat_history.db` and bind-mounted inside the container
as `/data/chat_history.db`. It is not stored in the Git repository or container image.

## Before starting

The instructions use these names:

- **Trusted workstation** — a local computer with this repository, `ssh`, `scp`, and `flyctl`.
- **Administrative user** — the existing account that can SSH to the Droplet and run `sudo`. Replace
  `YOUR_ADMIN_USER` in commands with that account. If logged in as `root`, `sudo` can be omitted.
- **Deployment user** — the password-disabled `ffbot` account used by GitHub Actions and Docker Compose.
- **Droplet address** — `157.230.80.234`.

Run workstation commands from the repository root unless stated otherwise. Do not put private keys, populated
environment files, GHCR tokens, or SQLite databases in Git.

The intended end state is:

```text
GitHub Actions
  ├── tests the commit with uv
  ├── publishes ghcr.io/rwesterman/ff_bot:<commit-sha>
  └── connects to ffbot@157.230.80.234 over SSH
        └── Docker Compose runs the image
              └── /opt/ff-bot/data/chat_history.db persists on the Droplet
```

Complete the sections in order:

1. Prepare Docker, the `ffbot` account, storage, and networking.
2. Create the server-managed application environment file.
3. Install a dedicated GitHub Actions SSH key.
4. Create the GitHub `production` environment and its secrets.
5. Authenticate the Droplet to GHCR.
6. Migrate the SQLite database.
7. Stop Fly and trigger the first deployment.
8. Verify the bot before decommissioning anything.
9. Use the operational and rollback procedures as needed.
10. Configure a new database backup before deleting Fly resources.

## 1. Prepare the Droplet

### 1.1 Connect with the existing administrative account

From the trusted workstation:

```bash
ssh YOUR_ADMIN_USER@157.230.80.234
```

Confirm the server is Ubuntu:

```bash
cat /etc/os-release
uname -m
```

These instructions target a supported 64-bit Ubuntu release.

### 1.2 Check whether Docker is already installed

Another deployment tool may already have installed Docker. Check before installing or removing anything:

```bash
docker --version
docker compose version
sudo systemctl is-active docker
sudo docker ps -a
sudo docker system df
```

If `docker`, the Compose plugin, and the service are working, skip to section 1.4. Existing containers should be
identified before removing them. Do not use `docker system prune` on a server with data you have not reviewed.

### 1.3 Install Docker Engine and the Compose plugin

These commands follow Docker's official Ubuntu repository installation. Run them on the Droplet as the administrative
user:

```bash
sudo apt update
sudo apt install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
```

Add Docker's Apt repository:

```bash
sudo tee /etc/apt/sources.list.d/docker.sources >/dev/null <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
```

Install and verify Docker:

```bash
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
sudo docker run --rm hello-world
docker compose version
```

See [Docker's Ubuntu installation guide](https://docs.docker.com/engine/install/ubuntu/) if the operating system is not
supported or the package repository fails.

### 1.4 Create the deployment user and application directories

Create the account once:

```bash
sudo adduser --disabled-password --gecos "" ffbot
sudo usermod -aG docker ffbot
```

If `ffbot` already exists, skip `adduser` and run `usermod` again safely.

The `docker` group can control the Docker daemon and is effectively privileged access to the host. Only the dedicated
deployment key should be authorized for this account.

Create the application directory and a data directory writable by UID/GID `10001`, which is the unprivileged user
inside the image:

```bash
sudo install -d -m 750 -o ffbot -g ffbot /opt/ff-bot
sudo install -d -m 750 -o 10001 -g 10001 /opt/ff-bot/data
```

The account has no password by design. To test it from the administrative account, start a fresh login context with
`sudo` rather than entering a password with `su`:

```bash
sudo -iu ffbot whoami
sudo -iu ffbot docker version
sudo -iu ffbot docker compose version
```

The first command should print `ffbot`. If Docker reports a permission error, confirm the group was applied:

```bash
id ffbot
```

The output must include the `docker` group.

### 1.5 Configure the DigitalOcean firewall

The bot needs:

- Inbound TCP 22 for SSH.
- Outbound TCP 443 for Discord, ESPN, OpenAI, DeepSeek, GitHub, GHCR, and package downloads.
- Outbound DNS, normally UDP/TCP 53.

It does not need inbound TCP 80 or 443. Docker Compose does not publish any application ports.

GitHub-hosted runner addresses are not fixed. With the current SSH deployment workflow, the Droplet's TCP 22 rule must
allow GitHub-hosted runners as well as the administrator's workstation. The simple configuration is key-only SSH on
port 22 with password authentication disabled. A later hardening option is a private network such as Tailscale.

DigitalOcean Cloud Firewalls are stateful and separate from host firewalls such as UFW. Review both if SSH or outbound
connections fail. See [DigitalOcean's firewall documentation](https://docs.digitalocean.com/products/networking/firewalls/how-to/configure-rules/).

## 2. Configure application secrets on the Droplet

The workflow deliberately does not copy application secrets. They remain in `/opt/ff-bot/app.env` across deployments.

From the trusted workstation, upload only the example through the existing administrative account:

```bash
scp deploy/app.env.example \
  YOUR_ADMIN_USER@157.230.80.234:/tmp/ff-bot-app.env
```

On the Droplet, install it with restrictive permissions:

```bash
sudo install -m 600 -o ffbot -g ffbot \
  /tmp/ff-bot-app.env \
  /opt/ff-bot/app.env
rm /tmp/ff-bot-app.env
```

Edit it on the Droplet:

```bash
sudoedit /opt/ff-bot/app.env
```

The example intentionally contains blank values. Populate required values before deployment. For optional settings,
either provide a valid value or remove the line entirely; an empty value is not always equivalent to an unset variable.
Important settings are:

- `DISCORD_BOT_TOKEN`, `LEAGUE_ID`, and `LEAGUE_YEAR` — required to start the bot.
- `PENALTY_CHANNEL_ID` — Discord channel for ten-minute unsportsmanlike-conduct bonus announcements. Blank/unset
  disables polling. Bonus records use the existing persistent database; see [penalty bonuses](../docs/penalty-bonuses.md).
- `ESPN_S2` and `SWID` — required only for a private ESPN league; remove both for a public league.
- `OPENAI_API_KEY` — required for `/ask` embeddings and `/rules`.
- `DEEPSEEK_API_KEY` — required for `/ask` and `/rules` answers.
- `DEEPSEEK_THINKING_ENABLED` and `DEEPSEEK_MAX_TOKENS` — populate with valid values or remove them to use code
  defaults.
- `OPENAI_EMBEDDING_MODEL`, `OPENAI_EMBEDDING_DIMENSIONS`, and `RAG_SYNC_INTERVAL_SECONDS` — populate them or remove
  them to use code defaults. Empty numeric values will prevent startup.
- `RAG_CHANNEL_IDS` — replace with comma-separated numeric Discord channel IDs, or remove the line to use the code's
  built-in allowlist.
- `RULES_GITHUB_TOKEN` — required when `longview_league_rules` is private; remove it when anonymous GitHub access is
  sufficient.
- `START_DATE`, `END_DATE`, `DEBUG`, and `TIMEZONE` — retained because they exist in the local `.env`, but the active
  Discord entry point does not currently read them.

Do not copy the repository's existing `.env` wholesale without reviewing it. In particular, do not populate
`BUCKET_NAME` or the related `AWS_*` variables on the Droplet yet. A non-empty `BUCKET_NAME` enables the current
Fly/Tigris-specific Litestream configuration.

Confirm ownership and permissions without printing the secrets:

```bash
sudo stat -c '%U %G %a %n' /opt/ff-bot/app.env
```

Expected output:

```text
ffbot ffbot 600 /opt/ff-bot/app.env
```

## 3. Give GitHub Actions SSH access

This uses a dedicated key pair:

```text
Private key → GitHub production environment secret
Public key  → /home/ffbot/.ssh/authorized_keys on the Droplet
```

Never upload the private key to the Droplet or commit either key to the repository.

### 3.1 Generate the key on a trusted workstation

Generate it outside the repository:

```bash
ssh-keygen \
  -t ed25519 \
  -f ~/.ssh/ff-bot-github-actions \
  -C "ff-bot GitHub Actions" \
  -N ""
```

The empty passphrase is intentional because the non-interactive workflow cannot answer a passphrase prompt. The key is
dedicated to this deployment and protected by GitHub's secret storage.

The command creates:

```text
~/.ssh/ff-bot-github-actions       private key
~/.ssh/ff-bot-github-actions.pub   public key
```

Protect the private key:

```bash
chmod 600 ~/.ssh/ff-bot-github-actions
```

### 3.2 Install the public key for `ffbot`

Upload only the `.pub` file through the existing administrative account:

```bash
scp ~/.ssh/ff-bot-github-actions.pub \
  YOUR_ADMIN_USER@157.230.80.234:/tmp/ff-bot-github-actions.pub
```

On the Droplet:

```bash
sudo install -d -m 700 -o ffbot -g ffbot /home/ffbot/.ssh
sudo touch /home/ffbot/.ssh/authorized_keys
sudo tee -a /home/ffbot/.ssh/authorized_keys \
  < /tmp/ff-bot-github-actions.pub \
  >/dev/null
sudo chown ffbot:ffbot /home/ffbot/.ssh/authorized_keys
sudo chmod 600 /home/ffbot/.ssh/authorized_keys
rm /tmp/ff-bot-github-actions.pub
```

Append the key only once. A duplicate public-key line is harmless but unnecessary.

### 3.3 Test the deployment login

From the trusted workstation:

```bash
ssh \
  -i ~/.ssh/ff-bot-github-actions \
  -o IdentitiesOnly=yes \
  ffbot@157.230.80.234
```

On the resulting `ffbot` shell:

```bash
whoami
docker version
docker compose version
test -d /opt/ff-bot
test -f /opt/ff-bot/app.env
```

All commands should succeed. Exit back to the workstation:

```bash
exit
```

### 3.4 Record and verify the Droplet host keys

An SSH client uses host keys to confirm that it reached the expected server. `ssh-keyscan` alone does not prove the
server's identity, so compare its fingerprint with the key shown through a trusted DigitalOcean console session.

On the Droplet:

```bash
sudo ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
```

On the trusted workstation:

```bash
ssh-keyscan -t ed25519 157.230.80.234 2>/dev/null |
  ssh-keygen -lf -
```

The SHA256 fingerprints must match. After they match, collect the complete host-key records:

```bash
ssh-keyscan -H 157.230.80.234 > /tmp/ff-bot-known-hosts
```

Keep this temporary file for the next section.

## 4. Configure the GitHub `production` environment

A GitHub environment is a named deployment target, not a file or server. The workflow's deploy job declares
`environment: production`, so GitHub only makes that environment's secrets available to that job.
See [GitHub's environment-secret documentation](https://docs.github.com/github/automating-your-workflow-with-github-actions/creating-and-using-encrypted-secrets?tool=cli)
for the distinction between environment, repository, and organization secrets.

### 4.1 Create the environment

In the `rwesterman/ff_bot` repository on GitHub:

1. Open **Settings**.
2. Select **Environments** in the left sidebar.
3. Select **New environment**.
4. Enter `production` exactly.
5. Select **Configure environment**.

If available for the repository's GitHub plan, restrict deployment branches to `master`. A required reviewer can provide
a useful cutover gate: the image can build and publish while the deployment waits for approval. A sole maintainer
should not enable "prevent self-review" if no other reviewer can approve deployments.

See [GitHub's environment documentation](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/manage-environments)
for current plan limitations and protection options.

### 4.2 Add the private-key secret

Under **Environment secrets**, select **Add secret**:

```text
Name: DROPLET_SSH_PRIVATE_KEY
Value: the complete contents of ~/.ssh/ff-bot-github-actions
```

The value must include the multiline header and footer:

```text
-----BEGIN OPENSSH PRIVATE KEY-----
...
-----END OPENSSH PRIVATE KEY-----
```

Do not enter the filename or public key. The secret value is the private-key file's complete contents.

With the GitHub CLI, the same operation can be performed from the trusted workstation without printing the key:

```bash
gh secret set \
  --env production \
  DROPLET_SSH_PRIVATE_KEY \
  < ~/.ssh/ff-bot-github-actions
```

### 4.3 Add the known-hosts secret

Under **Environment secrets**, add:

```text
Name: DROPLET_SSH_KNOWN_HOSTS
Value: the complete contents of /tmp/ff-bot-known-hosts
```

Or use the GitHub CLI:

```bash
gh secret set \
  --env production \
  DROPLET_SSH_KNOWN_HOSTS \
  < /tmp/ff-bot-known-hosts
```

After GitHub has stored the value, remove the temporary workstation file:

```bash
rm /tmp/ff-bot-known-hosts
```

### 4.4 Add environment variables

Under **Environment variables**, add:

```text
DROPLET_HOST=157.230.80.234
DROPLET_SSH_USER=ffbot
```

The workflow has the same values as defaults, but explicitly storing them makes the deployment target visible in the
environment configuration.

With the GitHub CLI:

```bash
gh variable set --env production DROPLET_HOST --body 157.230.80.234
gh variable set --env production DROPLET_SSH_USER --body ffbot
```

Verify the names without revealing secret values:

```bash
gh secret list --env production
gh variable list --env production
```

The finished environment should contain:

```text
Secrets
  DROPLET_SSH_PRIVATE_KEY
  DROPLET_SSH_KNOWN_HOSTS

Variables
  DROPLET_HOST=157.230.80.234
  DROPLET_SSH_USER=ffbot
```

If the repository's GitHub plan does not provide environment secrets, add the same names under
**Settings → Secrets and variables → Actions** as repository secrets and variables. The workflow's `secrets` and `vars`
contexts can read those values too.

## 5. Allow the Droplet to pull private images from GHCR

GitHub Actions publishes with its temporary `GITHUB_TOKEN`; that credential is not available on the Droplet. A private
GHCR package therefore requires a separate read-only credential on the server.

### 5.1 Create a read-only package token

In the GitHub account that can read `rwesterman/ff_bot`:

1. Open **Settings**.
2. Open **Developer settings**.
3. Open **Personal access tokens → Tokens (classic)**.
4. Generate a new classic token dedicated to this Droplet.
5. Select only `read:packages`.
6. Copy the token when GitHub displays it.

GitHub documents `read:packages` as the scope for downloading private container packages. See
[Working with the Container registry](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry).

### 5.2 Log in as the deployment user

On the Droplet, start an `ffbot` login context:

```bash
sudo -iu ffbot
```

Log in to GHCR:

```bash
docker login ghcr.io -u rwesterman
```

Paste the classic token at the password prompt. Do not put the token directly on a shell command line or in `app.env`.
Docker stores the credential under `/home/ffbot/.docker/config.json`, where the deployment user's Docker commands can
use it.

Exit back to the administrative shell:

```bash
exit
```

Alternatively, make the GHCR package public after its first publication. Public images can be pulled without a token,
but the initial workflow may publish successfully and fail its first deployment before package visibility is changed.
Authenticating the Droplet first avoids that bootstrap failure.

## 6. Migrate the SQLite database from Fly

The database uses WAL mode. Do not use a raw file copy of `/data/chat_history.db` while the Fly bot is running because
committed transactions may still be represented in the WAL file. Python's SQLite online backup API creates a consistent
single-file snapshot while the source is active.

The snapshot includes stored Discord messages, chunks, embeddings, and rules indexes, avoiding a full reindex.
See the [SQLite online backup documentation](https://www.sqlite.org/backup.html) for why this is safer than copying an
active WAL database directly.

### 6.1 Create a consistent snapshot on the Fly Machine

From the trusted workstation:

```bash
fly status -a ff-bot
```

If more than one Machine is listed, note the running worker's Machine ID and add `--machine MACHINE_ID` to both Fly SSH
commands below.

Create the snapshot and run SQLite's integrity check:

```bash
fly ssh console -a ff-bot -C \
  'python -c "import sqlite3; src=sqlite3.connect(\"/data/chat_history.db\"); dst=sqlite3.connect(\"/tmp/chat_history-migration.db\"); src.backup(dst, pages=256, sleep=0.05); print(dst.execute(\"PRAGMA integrity_check\").fetchone()[0]); dst.close(); src.close()"'
```

The command should print:

```text
ok
```

### 6.2 Download the snapshot to the workstation

```bash
fly ssh sftp get -a ff-bot \
  /tmp/chat_history-migration.db \
  ./chat_history-migration.db
chmod 600 ./chat_history-migration.db
```

The repository ignores this filename, but it still contains private Discord history. Do not add it to Git or leave it
on a shared workstation.

The current Fly CLI syntax is documented under
[`fly ssh sftp get`](https://fly.io/docs/flyctl/ssh-sftp-get/).

Optionally remove the Fly Machine's temporary snapshot after the download succeeds:

```bash
fly ssh console -a ff-bot -C \
  'rm -f /tmp/chat_history-migration.db'
```

### 6.3 Upload and install the snapshot on the Droplet

From the trusted workstation:

```bash
scp ./chat_history-migration.db \
  YOUR_ADMIN_USER@157.230.80.234:/tmp/chat_history-migration.db
```

On the Droplet:

```bash
sudo install -m 600 -o 10001 -g 10001 \
  /tmp/chat_history-migration.db \
  /opt/ff-bot/data/chat_history.db
rm /tmp/chat_history-migration.db
sudo ls -lh /opt/ff-bot/data/chat_history.db
```

Keep the protected workstation copy and the Fly volume until the DigitalOcean bot is verified and a new backup strategy
is active. Messages created after the snapshot can be retrieved by the bot's next incremental Discord history sync.

## 7. Perform the first deployment and cutover

Only one running process should use `DISCORD_BOT_TOKEN`. Starting the DigitalOcean container before stopping Fly can
cause Discord session conflicts and duplicate bot behavior.

### 7.1 Complete the preflight checklist

Before triggering deployment, confirm:

- Docker and Docker Compose work as `ffbot`.
- `/opt/ff-bot/app.env` exists and contains no placeholder values.
- `/opt/ff-bot/data/chat_history.db` exists with owner UID/GID `10001`.
- The deployment SSH key works from the trusted workstation.
- The GitHub `production` secrets and variables exist.
- `ffbot` has authenticated to GHCR, or the package is public.
- The DigitalOcean firewall permits inbound SSH from GitHub-hosted runners and outbound Internet access.

### 7.2 Stop the Fly bot

From the trusted workstation, get the running Machine ID:

```bash
fly status -a ff-bot
```

Stop that Machine:

```bash
fly machine stop MACHINE_ID -a ff-bot
```

Confirm its state is stopped:

```bash
fly status -a ff-bot
```

Stopping preserves the Fly Machine and attached volume for rollback. Do not destroy either yet.
See Fly's [`machine stop` documentation](https://fly.io/docs/flyctl/machine-stop/) for the current command options.

### 7.3 Trigger GitHub Actions

Pushes to `master` run the complete test, publish, and deploy workflow. Pull requests targeting `master` run tests but
do not publish or deploy.

The first production deployment normally starts when this migration branch is merged into `master`. It can also be
started from **GitHub → Actions → Test, publish, and deploy → Run workflow**, provided the selected branch is permitted
by the `production` environment.

The workflow:

1. Runs non-live pytest and Ruff checks with the locked uv environment.
2. Builds and publishes `ghcr.io/rwesterman/ff_bot:<full-commit-sha>`.
3. Verifies `/opt/ff-bot/app.env` and Docker Compose on the Droplet.
4. Uploads the Compose configuration and immutable image reference.
5. Pulls the image before replacing the active configuration.
6. Starts the container and confirms it remains running without a restart during the initial check.

If the environment has a required reviewer, stop Fly before selecting **Approve and deploy**.

## 8. Verify production

### 8.1 Check the GitHub workflow

In GitHub Actions, all three jobs should be green:

```text
Test
Publish GHCR image
Deploy to DigitalOcean
```

If publishing succeeds but deployment fails, the image is still safely stored in GHCR. Fix the server-side issue and
rerun the failed job or the workflow.

### 8.2 Inspect the container on the Droplet

Connect as `ffbot` with the deployment key or use `sudo -iu ffbot` from the administrative account:

```bash
cd /opt/ff-bot
docker compose --env-file .deploy.env -f compose.production.yaml ps
docker inspect ff-bot --format '{{.Config.Image}} {{.State.Status}} {{.RestartCount}}'
docker compose --env-file .deploy.env -f compose.production.yaml logs --tail=200 bot
```

Expected results:

- The service is `running`.
- The image ends with the deployed full Git commit SHA.
- The restart count is `0`.
- Logs show a successful Discord connection.
- Logs do not show repeated connection, permission, or SQLite errors.

Follow logs during a Discord test:

```bash
docker compose --env-file .deploy.env -f compose.production.yaml logs --follow --tail=100 bot
```

In Discord, test a lightweight league command, `/ask`, and `/rules`. Confirm old chat history is available through
`/ask` before removing any Fly resources.

### 8.3 Confirm persistence

On the Droplet:

```bash
sudo ls -lh /opt/ff-bot/data/chat_history.db
sudo stat -c '%u %g %a %n' /opt/ff-bot/data/chat_history.db
```

The file should remain outside the container and normally be owned by UID/GID `10001`.

## 9. Routine operations

### View status and logs

Run as `ffbot`:

```bash
cd /opt/ff-bot
docker compose --env-file .deploy.env -f compose.production.yaml ps
docker compose --env-file .deploy.env -f compose.production.yaml logs --tail=200 bot
```

### Restart without changing the image

```bash
cd /opt/ff-bot
docker compose --env-file .deploy.env -f compose.production.yaml restart bot
```

### Change application environment variables

Edit as an administrator:

```bash
sudoedit /opt/ff-bot/app.env
```

Then recreate the container as `ffbot`:

```bash
cd /opt/ff-bot
docker compose --env-file .deploy.env -f compose.production.yaml up -d --force-recreate bot
```

### Deploy new code

Merge tested changes into `master`. GitHub Actions publishes a new commit-tagged image and recreates the service.
The persistent database directory and server-managed `app.env` are not replaced.

## 10. Roll back

Every deployment is tagged with the full Git commit SHA. Find a previously successful SHA in GitHub Actions or the GHCR
package versions, then run as `ffbot`:

```bash
cd /opt/ff-bot
printf 'FF_BOT_IMAGE=ghcr.io/rwesterman/ff_bot:OLD_COMMIT_SHA\n' > .deploy.env
docker compose --env-file .deploy.env -f compose.production.yaml pull
docker compose --env-file .deploy.env -f compose.production.yaml up -d
docker compose --env-file .deploy.env -f compose.production.yaml logs --tail=200 bot
```

An image rollback does not reverse changes already made to SQLite. Preserve a database backup before deploying changes
that introduce incompatible schema migrations.

For an emergency return to Fly:

1. Stop the DigitalOcean container.
2. Start the previously stopped Fly Machine.
3. Verify the Fly bot is connected before making further changes.

On the Droplet:

```bash
cd /opt/ff-bot
docker compose --env-file .deploy.env -f compose.production.yaml stop bot
```

From the trusted workstation:

```bash
fly machine start MACHINE_ID -a ff-bot
fly status -a ff-bot
```

Never run both copies simultaneously.

## 11. Backups and final Fly decommissioning

Without `BUCKET_NAME`, the container runs `python main.py` directly and the only current database copy is on the
Droplet's filesystem. The checked-in Litestream configuration still targets Fly's Tigris endpoint and should not be
enabled unchanged on DigitalOcean.

Before destroying the Fly Machine, volume, or Tigris data:

1. Verify the DigitalOcean bot and all three command types.
2. Confirm the migrated history and embeddings are present.
3. Configure and test a new SQLite backup destination, such as an S3-compatible object store through a generalized
   Litestream configuration.
4. Perform and restore-test at least one new backup.
5. Keep a protected offline migration snapshot until the new backup is verified.

Stopping Fly is sufficient during the transition. Deleting Fly resources is a separate, irreversible cleanup step and
should happen only after the new backup path is proven.

## Troubleshooting

### GitHub Actions cannot connect over SSH

Check:

- `DROPLET_SSH_PRIVATE_KEY` contains the private key, including its header and footer.
- `DROPLET_SSH_KNOWN_HOSTS` contains the verified `ssh-keyscan -H` output.
- The public key is present once in `/home/ffbot/.ssh/authorized_keys`.
- `/home/ffbot/.ssh` is mode `700` and `authorized_keys` is mode `600`.
- The DigitalOcean and host firewalls permit TCP 22 from the GitHub-hosted runner.
- The local test with `ssh -i ~/.ssh/ff-bot-github-actions` succeeds.

### GitHub Actions reports that `app.env` is missing

On the Droplet:

```bash
sudo ls -l /opt/ff-bot/app.env
```

Create it from `deploy/app.env.example` using section 2. The workflow intentionally will not create or overwrite it.

### GitHub Actions cannot read `app.env`

The deployment connects as `ffbot`, so that account must be able to traverse `/opt/ff-bot` and read `app.env`. Inspect
the path without printing any secret values:

```bash
sudo namei -l /opt/ff-bot/app.env
sudo stat -c '%U %G %a %n' /opt/ff-bot /opt/ff-bot/app.env
```

Repair only the application directory and environment file:

```bash
sudo chown ffbot:ffbot /opt/ff-bot
sudo chmod 750 /opt/ff-bot
sudo chown ffbot:ffbot /opt/ff-bot/app.env
sudo chmod 600 /opt/ff-bot/app.env
sudo -u ffbot test -r /opt/ff-bot/app.env &&
  echo "ffbot can read app.env"
```

Do not use `chown -R` on `/opt/ff-bot`; `/opt/ff-bot/data` must remain writable by the container's UID/GID `10001`.

### The image pull is unauthorized

The GHCR login must be performed as `ffbot`, not only as the administrative user:

```bash
sudo -iu ffbot docker login ghcr.io -u rwesterman
```

Use a classic token with `read:packages`, or make the package public.

### Docker is denied for `ffbot`

```bash
id ffbot
sudo usermod -aG docker ffbot
sudo -iu ffbot docker version
```

Use a new login context after changing group membership.

### The container repeatedly restarts

```bash
sudo -iu ffbot
cd /opt/ff-bot
docker compose --env-file .deploy.env -f compose.production.yaml ps
docker compose --env-file .deploy.env -f compose.production.yaml logs --tail=300 bot
```

Common causes are an unchanged placeholder in `app.env`, invalid Discord or ESPN credentials, an unwritable data
directory, GHCR authentication failure, or insufficient memory.

### Discord commands do not appear

The bot receives commands over an outbound Discord Gateway WebSocket; no inbound HTTP port or Caddy service is needed.
Confirm outbound HTTPS/DNS access and inspect the bot logs for a successful Discord connection. Also confirm the Fly bot
is stopped so two processes are not competing for the same token.

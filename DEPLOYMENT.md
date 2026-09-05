# Permanent Droplet deployment

The bot runs as a Docker Compose service. Docker starts it when the Droplet
boots, restarts it after an unexpected exit, keeps its SQLite deduplication
state in a named volume, and rotates its container logs. It exposes no network
port; it only makes outbound HTTPS and IMAPS connections.

## Current deployment

The bot is installed on the existing Venvi Droplet at `/opt/find-apartment`.
The checkout tracks `main` from `https://github.com/massmola/find-wg.git`, and
the deployment account is `apartment-deploy`. Venvi's actual Compose directory
is `/root/venvi`.

Connect from the development machine:

```bash
ssh -i ~/.ssh/venvi_droplet root@207.154.250.245
```

On the Droplet, inspect the bot and its update schedule:

```bash
cd /opt/find-apartment
docker compose ps
docker compose logs --tail=50 apartment-bot
systemctl list-timers 'find-apartment-update*'
journalctl -u find-apartment-update@apartment-deploy.service -n 50 --no-pager
```

To release a new version, commit the changes on your development machine and
push to `origin main`. The enabled timer fetches, tests, and deploys the commit
on its next check (about five minutes, plus build time). To check immediately:

```bash
systemctl start find-apartment-update@apartment-deploy.service
```

The initial deployment preserved the 157 existing SQLite deduplication records.
Credentials are stored only in the server's mode-600 `.env`; they are not in Git.
Avoid editing tracked files on the server because the updater rejects a dirty
checkout. If a later release changes the systemd unit files themselves, reinstall
those files and run `systemctl daemon-reload` as described below.

## Sharing the existing Venvi Droplet

Venvi and this bot should remain separate Compose projects on the same server:

```text
/opt/venvi/             # Venvi compose.yaml/docker-compose.yml
/opt/find-apartment/    # This project's compose.yaml
```

This project explicitly uses the Compose project name `find-apartment`, so its
container, network, image, and `apartment_bot_data` volume do not collide with
Venvi's resources. Venvi continues to own host port 80; the apartment bot does
not bind any host port. Do not combine the two Compose files and do not run
`docker compose down` from the wrong directory.

The bot is limited to half a CPU, 256 MB of memory, and 64 processes so that it
cannot consume all resources needed by Venvi. Before deployment, check the
existing Droplet:

```bash
free -h
df -h
docker stats --no-stream
```

If the Droplet is already close to its memory or disk limit, resize it before
adding the bot. After deployment, repeat `docker stats --no-stream` while both
services are running.

## 1. Prepare the Droplet

Use a supported Ubuntu LTS release. Install Docker Engine and the Compose
plugin by following [Docker's official Ubuntu instructions](https://docs.docker.com/engine/install/ubuntu/),
then verify them:

```bash
docker --version
docker compose version
sudo systemctl enable --now docker
```

Create a non-root deployment user, add it to the `docker` group, and reconnect
so that the new group membership takes effect. Be aware that membership in the
`docker` group grants root-level privileges on the server.

No additional inbound application port is required. Keep Venvi's existing port
80 rule (and 443 if it uses HTTPS). Create or update a
[DigitalOcean Cloud Firewall](https://docs.digitalocean.com/products/networking/firewalls/how-to/create/)
with SSH restricted to your IP address when practical. Leave the default
outbound rules enabled so DNS, package updates, HTTPS, Telegram, and IMAPS can
work.

## 2. Clone and configure the application

Clone the Git repository to the Droplet as `/opt/find-apartment` and make the
deployment user its owner. The clone must use the branch that should be
deployed and that branch must track its remote counterpart. Do not copy the
local SQLite database or log file; the Docker volume and Docker logging manage
those on the server.

Before cloning, commit and push all application and deployment files from the
development machine. The updater deliberately refuses to deploy when its
essential files are untracked.

On the Droplet:

```bash
cd /opt/find-apartment
cp .env.example .env
chmod 600 .env
editor .env
```

Fill in the IMAP and Telegram values and review all search filters. If you copy
an existing `.env` instead, transfer it over SSH/SCP and keep mode `600`; never
commit it or paste it into a public terminal/session.

## 3. Verify credentials and start it

Build the same image that will run permanently, execute the synthetic end-to-end
self-test, and then start the service:

```bash
docker compose build
docker compose run --rm apartment-bot --self-test
docker compose up -d
docker compose ps
docker compose logs --tail=100 apartment-bot
```

The self-test reads the configured mailbox and sends one clearly labelled test
message to Telegram. A successful run ends with `SELF-TEST PASSED`.

Because the service has `restart: unless-stopped` and Docker is enabled at boot,
it comes back after process failures and Droplet reboots. Running
`docker compose stop` intentionally keeps it stopped after a reboot; use
`docker compose up -d` to resume it.

## Routine operations

Run these commands from `/opt/find-apartment`:

```bash
# Follow output (Ctrl+C stops following, not the bot)
docker compose logs --follow apartment-bot

# Show service state and restart count
docker compose ps
docker inspect --format '{{.RestartCount}}' "$(docker compose ps -q apartment-bot)"

# Restart after editing .env
docker compose up -d --force-recreate

# Rebuild and replace the container after updating source files
docker compose up -d --build

# Stop and resume intentionally
docker compose stop
docker compose up -d
```

When automatic updates are enabled, disable the timer and let any running update
finish before stopping the bot or restoring its database. Otherwise the next
update check will start it again. Re-enable the timer after maintenance.

## Automatic updates from Git

The included systemd timer checks the current branch's configured upstream
every five minutes. When a new commit is available, it performs a fast-forward
merge, rebuilds, and runs the unit tests plus a CLI smoke test before replacing
only the `find-apartment` Compose service. It does not run commands in
`/opt/venvi`.

The updater refuses to proceed if tracked files were edited directly on the
server or if the local and remote branches diverged. A failed fetch, build, or
test leaves the existing container running. An immediate failure after
replacement rolls back to the previous image. The next timer execution retries
the deployment. The image records its Git commit in the standard
`org.opencontainers.image.revision` label so that a failed deployment is not
mistaken for a successful one.

This requires `/opt/find-apartment` to be a Git clone—not a directory copied by
`rsync` or SCP—and its checked-out branch must track a remote branch. Verify:

```bash
cd /opt/find-apartment
git remote -v
git status --short --branch
git rev-parse --abbrev-ref --symbolic-full-name '@{upstream}'
```

Install the timer for the dedicated deployment user (already done on this
Droplet). Use this instance name even when logged in as root:

```bash
sudo cp deploy/find-apartment-update@.service \
  deploy/find-apartment-update@.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now find-apartment-update@apartment-deploy.timer
sudo systemctl start find-apartment-update@apartment-deploy.service
systemctl list-timers 'find-apartment-update*'
sudo journalctl -u find-apartment-update@apartment-deploy.service -n 100 --no-pager
```

If the Git repository is private, configure a read-only deploy key for the
deployment user and confirm that `git fetch` works non-interactively. Do not
give the Droplet a key with write access unless another workflow truly requires
it.

To disable automatic deployment without stopping the running bot:

```bash
sudo systemctl disable --now find-apartment-update@apartment-deploy.timer
```

Updates to `.env` remain manual because that file is ignored by Git. The timer
does not run the end-to-end self-test, since that would send a Telegram test
message on every update; it runs the offline unit tests and a container CLI
smoke test instead.

Do not run `docker compose down --volumes`: `--volumes` deletes the bot's
deduplication history and causes existing listings to be treated as new.

## Back up and restore the state

The state contains listing fingerprints rather than credentials, but retaining
it prevents duplicate notifications. Create a consistent SQLite backup through
the running Python image:

```bash
mkdir -p backups
docker compose exec -T apartment-bot python -c \
  'import sqlite3; source=sqlite3.connect("/data/notification-bot.sqlite3"); backup=sqlite3.connect("/data/notification-bot.backup.sqlite3"); source.backup(backup); backup.close(); source.close()'
docker compose cp apartment-bot:/data/notification-bot.backup.sqlite3 \
  "backups/notification-bot-$(date +%F).sqlite3"
docker compose exec -T apartment-bot rm /data/notification-bot.backup.sqlite3
```

Copy backups off the Droplet. To restore one, place it in the project directory
as `restore.sqlite3`, then run:

```bash
docker compose stop apartment-bot
docker compose run --rm --no-deps \
  --volume /opt/find-apartment/restore.sqlite3:/restore.sqlite3:ro \
  --entrypoint sh apartment-bot -c \
  'cp /restore.sqlite3 /data/notification-bot.sqlite3'
docker compose up -d
```

Make the backup readable by the container's non-root user during the restore.
The one-off mount leaves the tracked Compose configuration unchanged.

## Troubleshooting

```bash
# Render and validate the Compose configuration (environment values are shown)
docker compose config

# Inspect recent errors
docker compose logs --since=1h apartment-bot

# Re-run the end-to-end test using the configured credentials
docker compose run --rm apartment-bot --self-test
```

Common causes are an expired/revoked mail app password, IMAP being disabled,
the Telegram chat never having messaged the bot, or an incorrect chat ID. Source
website layout changes are logged as `DIRECT SOURCE WARNING`; they do not stop
email polling or the other direct sources.

## Manual completion checklist

- [ ] Commit application and deployment changes and push them to the tracked
  branch of the Git repository.
- [ ] Reuse the Droplet already running Venvi and confirm its IP/SSH access.
- [ ] Check `free -h`, `df -h`, and `docker stats --no-stream`; resize the
  Droplet first if it is already close to a resource limit.
- [ ] Update its DigitalOcean Cloud Firewall: keep Venvi's TCP 80/443 rules,
  restrict SSH to your IP when possible, and keep outbound DNS/HTTPS/IMAPS.
- [ ] Confirm the existing Docker installation includes the Compose plugin and
  that the Docker service is enabled.
- [ ] Create a deployment user and `/opt/find-apartment`, then securely copy the
  Git repository there with `git clone`. Do not copy
  `.notification-bot.sqlite3` unless you intentionally want to migrate the
  current deduplication history.
- [ ] Create `/opt/find-apartment/.env`, fill in IMAP and Telegram secrets, review
  search limits, and set its mode to `600`.
- [ ] Run `docker compose build` followed by
  `docker compose run --rm apartment-bot --self-test`; confirm the PASS output
  and Telegram test message.
- [ ] Run `docker compose up -d`, then check `docker compose ps` and logs.
- [ ] If the repository is private, give the deployment user a read-only Git
  deploy key and confirm that `git fetch` needs no interactive password.
- [ ] Install and enable `find-apartment-update@<deployment-user>.timer`, trigger
  its service once, and inspect its journal output.
- [ ] From the Venvi directory, confirm Venvi is still healthy; then run
  `docker stats --no-stream` to check both containers' resource use.
- [ ] Reboot the Droplet once and confirm the service returns automatically.
- [ ] Schedule an off-Droplet backup of the SQLite volume and periodically
  update Ubuntu, Docker, and the base image (`docker compose up -d --build`).

No additional domain name, DNS record, TLS certificate, reverse proxy, HTTP
port, or HTTPS port is needed for this bot. Venvi's existing networking remains
unchanged.

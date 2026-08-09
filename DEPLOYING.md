# Deploying

The whole app is one directory of Python with no dependencies, and all its state
is one SQLite file. That makes hosting easy, and makes one mistake fatal:
**if the disk isn't persistent, you lose the ladder on every deploy.** Every
option below is really about where that file lives.

---

## 0. Before you make it public

On a club LAN the defaults are fine. On the open internet, do these three things:

1. **Set `admin_password`** in `config.json`. It ships as `changeme` and the
   server warns you on every start.
2. **Serve over HTTPS.** PINs and session cookies cross the network on every
   request. Fly does this for you; on a VPS use Caddy (below).
3. **Set `base_url`** in `config.json` (e.g. `https://ladder.example.edu`), so
   email links and unsubscribe links point somewhere real.

PINs are stored hashed (PBKDF2-SHA256), and sessions live in the database, so a
restart doesn't sign the club out.

---

## 1. Testing on your phone (no hosting)

Run it on your laptop and reach it over the wifi:

```bash
python3 run.py
```

Then browse to `http://<your-computer's-LAN-IP>:8000` from the phone.

### On Windows + WSL2 this will not work without one fix

WSL2 runs in its own virtual network by default, so binding `0.0.0.0` inside WSL
still isn't reachable from your phone. Create `C:\Users\<you>\.wslconfig`:

```ini
[wsl2]
networkingMode=mirrored
```

Run `wsl --shutdown` in PowerShell and reopen. WSL now shares the Windows IP and
the LAN address just works. (Requires Windows 11 22H2+.)

On Windows 10, use an **administrator** PowerShell instead:

```powershell
netsh interface portproxy add v4tov4 listenport=8000 listenaddress=0.0.0.0 `
  connectport=8000 connectaddress=<the IP from `hostname -I` inside WSL>
New-NetFirewallRule -DisplayName "Tennis Ladder" -Direction Inbound `
  -LocalPort 8000 -Protocol TCP -Action Allow
```

The WSL IP changes on reboot, so you'd redo that. Mirrored mode doesn't.

**Campus wifi warning:** many university networks isolate clients from each
other, so phones may not reach a laptop even on the same SSID. If that's your
network, you need real hosting rather than a workaround.

---

## 2. Oracle Cloud Always Free (no ongoing cost)

Oracle's free tier is genuinely free indefinitely rather than a trial, which
makes it the right home for a club ladder with no budget. A card is required at
signup for identity verification but is not charged while you stay on Always
Free resources.

### 2.1 Getting the account open

Signup is the fiddliest part. The three things that actually go wrong:

| Symptom | What's happening |
|---|---|
| Card declined | Prepaid, virtual and many one-time-use cards are rejected. A normal debit or credit card usually clears. The card is verified, not billed. |
| "Account under review" | Manual review. Usually clears in a few hours, sometimes 24. Nothing to fix — don't create a second account, which delays it further. |
| Signup rejects your region | Your **home region is permanent**, so pick the one nearest your campus. If a region won't take you, try the next nearest. |

### 2.2 The one rule that keeps it free

New accounts start with a **30-day trial that includes credits**, on top of
Always Free. During those 30 days the console will happily let you create paid
resources and the credits absorb the cost — then the trial ends and they get
shut down or start billing.

So: **anything you create must carry the "Always Free eligible" tag.** The
console shows that label next to eligible shapes and options. If you don't see
it, you've selected something that will eventually cost money. That single habit
is the whole story on not getting billed.

Your Always Free allowance is 2 micro VMs, 200 GB of block storage total, and 1
VCN. This ladder needs one VM and about 47 GB.

### 2.3 Create the instance — every step of the form

Hamburger menu → **Compute** → **Instances** → **Create instance**. The form is
long, but most of it you leave alone. Going down it in order:

**Name.** Anything — `tennis-ladder`.

**Create in compartment.** Leave the default (it'll be your tenancy name, the
root compartment). Compartments are for organising big estates; you don't need
one.

**Placement / Availability domain.** If your region shows AD-1, AD-2, AD-3, any
is fine — but note which you picked. If instance creation later fails with **"Out
of host capacity"**, come back and try a different AD. That error means Oracle
has no free-tier hardware in that AD right now, not that you did anything wrong.

**Security.** Two toggles here, both of which you **leave off**:
- *Shielded instance* — secure boot / TPM. Not supported on the free micro shape.
- *Confidential computing* — encrypted memory. Paid shapes only.

**Image and shape.** This is the section that matters.
- Click **Change image** → *Canonical Ubuntu* → **24.04** (or 22.04). The default
  is Oracle Linux; the commands in this guide assume Ubuntu.
- Click **Change shape** → **Virtual machine** → category **Specialty and
  previous generation** → **`VM.Standard.E2.1.Micro`**. It's marked *Always Free
  eligible*. 1/8 OCPU, 1 GB RAM.

  Oracle also offers Ampere ARM (`VM.Standard.A1.Flex`, up to 4 cores / 24 GB
  free) under the *Ampere* category. It's a far better machine, but it's very
  often out of capacity, and chasing it is how people spend a whole evening. This
  app is stdlib Python over a SQLite file — 1 GB is genuinely plenty. Take the
  micro; you can always rebuild on Ampere later.

**Primary VNIC / Networking.** First-timer defaults are correct here, but check
one thing:
- *Primary network*: **Create new virtual cloud network** (it'll name it
  something like `vcn-20260809-1234`).
- *Subnet*: **Create new subnet**, and make sure it says **public subnet**. A
  private subnet has no route from the internet and nothing will ever reach it.
- **Public IPv4 address: Assign a public IPv4 address — this must be Yes.**
  It's the easiest thing on the whole form to leave wrong, and without it the
  server simply has no address to visit.
- Leave the private IP and DNS hostname fields alone.

**Add SSH keys.** Choose **Generate a key pair for me**, then click **Save
private key** (grab the public key too, it's harmless to have).

> This download happens **once**. Oracle does not store the private key and
> cannot re-issue it. Lose it and your only option is to destroy the instance and
> start over. Put it somewhere you'll still have it in six months.

**Boot volume.** Leave everything unticked/default. The default is about 47 GB,
which is inside the free 200 GB. Specifically:
- Don't tick *Specify a custom boot volume size* — you don't need more.
- Leave *boot volume performance* on the default **Balanced**. Raising it buys
  performance you won't use and can push you outside the free allowance.

Then **Create**. Provisioning takes a minute or two; wait for the tile to turn
from orange **PROVISIONING** to green **RUNNING**, then copy the **Public IP
address** shown on the instance page. That IP is your server.

### 2.4 Connect to it

From WSL (or any terminal). The username is `ubuntu` for an Ubuntu image
(`opc` if you took Oracle Linux):

```bash
chmod 600 ~/Downloads/ssh-key-*.key        # SSH refuses keys others can read
ssh -i ~/Downloads/ssh-key-*.key ubuntu@<public-ip>
```

If you downloaded the key on Windows, it's reachable from WSL under
`/mnt/c/Users/<you>/Downloads/`. Copy it into WSL first — `chmod` doesn't work
properly on the Windows filesystem:

```bash
cp /mnt/c/Users/<you>/Downloads/ssh-key-*.key ~/oracle.key
chmod 600 ~/oracle.key
ssh -i ~/oracle.key ubuntu@<public-ip>
```

### 2.5 Open the port — there are TWO firewalls

This is the single most common "the server is running but nothing loads"
problem on Oracle, and it catches nearly everyone. Both of these are required.

**Firewall 1 — Oracle's cloud-side Security List.** In the console: open your
instance → click the **subnet** link under Primary VNIC → click the **Security
List** (usually "Default Security List for vcn-…") → **Add Ingress Rules**:

| Field | Value |
|---|---|
| Stateless | leave unticked |
| Source Type | CIDR |
| Source CIDR | `0.0.0.0/0` |
| IP Protocol | TCP |
| Source Port Range | leave blank (means all) |
| Destination Port Range | `80,443` |
| Description | `web` |

Add a second rule for `8000` too while you're testing; you can delete it once
Caddy is running.

**Firewall 2 — the instance's own iptables.** Oracle's Ubuntu images ship with
local rules that drop everything except SSH, so the console rule alone does
nothing. On the server:

```bash
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80 -j ACCEPT
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 8000 -j ACCEPT
sudo netfilter-persistent save
```

(On an Oracle Linux image it's `sudo firewall-cmd --permanent --add-port=80/tcp`
and `--add-port=443/tcp`, then `sudo firewall-cmd --reload`.)

### 2.6 Install and run the ladder

First, get the code onto the server. **You don't need a git repository for
this** — pick whichever suits you.

**Either — copy it straight from your machine.** Run this in WSL, *not* on the
server, and note the trailing slash on the source path:

```bash
ssh -i ~/oracle.key ubuntu@<public-ip> "sudo mkdir -p /opt/tennis-ladder && sudo chown ubuntu /opt/tennis-ladder"

rsync -av -e "ssh -i ~/oracle.key" \
  --exclude data --exclude __pycache__ \
  ~/projects/tennis_ladder/ ubuntu@<public-ip>:/opt/tennis-ladder/
```

Simplest, needs no accounts. Updating later means running the `rsync` again.

**Or — clone from GitHub**, if you'd rather `git pull` to update and keep an
offsite copy of the code. Set the repo up once from WSL:

```bash
cd ~/projects/tennis_ladder
git init -b main && git add . && git commit -m "Tennis ladder"
# create an empty PRIVATE repo at github.com/new, then:
git remote add origin https://github.com/<you>/tennis-ladder.git
git push -u origin main
```

then on the server `git clone https://github.com/<you>/tennis-ladder.git
/opt/tennis-ladder`. A private repo asks for credentials — use a personal access
token (GitHub → Settings → Developer settings → Tokens), not your password.

> The `.gitignore` in this project excludes `data/`. Keep it that way: that
> directory holds the database (player emails, hashed PINs, every match) and
> `config.json` with your admin password. It is the one thing that must never
> reach a repository.

Then, on the server either way:

```bash
sudo apt update && sudo apt install -y python3 sqlite3
sudo mkdir -p /var/lib/tennis-ladder
sudo chown -R ubuntu /opt/tennis-ladder /var/lib/tennis-ladder

# smoke test in the foreground first
python3 /opt/tennis-ladder/run.py --data-dir /var/lib/tennis-ladder --port 8000
```

Visit `http://<public-ip>:8000`. If that loads, both firewalls are right and the
hard part is over. Stop it with Ctrl-C.

Now make it permanent — `/etc/systemd/system/ladder.service`:

```ini
[Unit]
Description=Tennis Ladder
After=network.target

[Service]
ExecStart=/usr/bin/python3 /opt/tennis-ladder/run.py --data-dir /var/lib/tennis-ladder --port 8000
WorkingDirectory=/opt/tennis-ladder
User=ubuntu
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now ladder
sudo systemctl status ladder        # should say "active (running)"
journalctl -u ladder -f             # live logs, Ctrl-C to stop watching
```

### 2.7 HTTPS with a free hostname

Certificates are issued to names, not IP addresses, and Oracle gives you a bare
IP. A free **DuckDNS** subdomain fixes that in about two minutes:

1. Go to duckdns.org and sign in (Google/GitHub — no card, no forms).
2. Type a subdomain, e.g. `riversidetennis`, and click **add domain**.
3. Paste your instance's public IP into its **current ip** box and click
   **update ip**.

You now own `riversidetennis.duckdns.org`. Then on the server:

```bash
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
  | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
  | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install -y caddy
```

Replace `/etc/caddy/Caddyfile` with just this:

```
riversidetennis.duckdns.org {
    reverse_proxy localhost:8000
}
```

```bash
sudo systemctl restart caddy
```

Caddy fetches and renews a real Let's Encrypt certificate on its own — no
further steps. Give it 30 seconds, then open
`https://riversidetennis.duckdns.org`.

Finally, tell the ladder its own address so email links work, and set the admin
password. Edit `/var/lib/tennis-ladder/config.json`:

```json
{
  "club_name": "Riverside College Tennis",
  "admin_password": "something-only-you-know",
  "base_url": "https://riversidetennis.duckdns.org"
}
```

```bash
sudo systemctl restart ladder
```

Then remove the temporary port-8000 ingress rule from the Security List — Caddy
is the only thing that should be reachable from outside now.

### 2.8 When it doesn't work

Work down this list; it's roughly in order of likelihood.

| Symptom | Cause and fix |
|---|---|
| Browser hangs, no error | The port isn't open. Both firewalls — Security List **and** `iptables` (§2.5). Check with `sudo iptables -L INPUT -n --line-numbers`. |
| "Connection refused" | The port is open but nothing is listening. `sudo systemctl status ladder`, then `journalctl -u ladder -n 50`. |
| SSH: "Permissions 0644 are too open" | `chmod 600` your key. If it lives under `/mnt/c`, copy it into the WSL filesystem first — permissions don't stick on the Windows one. |
| SSH: "Permission denied (publickey)" | Wrong username. Ubuntu images use `ubuntu`, Oracle Linux uses `opc`. |
| "Out of host capacity" on create | No free hardware in that availability domain right now. Try another AD, or another shape. Not something you did. |
| Instance created but no public IP | The subnet was private, or *Assign a public IPv4 address* was left off. Easiest fix is to terminate and recreate (§2.3). |
| Caddy won't get a certificate | DNS isn't pointing at the box yet, or port 80 is closed. Let's Encrypt validates over port 80 — it must be open in **both** firewalls even though you only browse on 443. Check with `dig +short yourname.duckdns.org`. |
| Worried about being charged | Billing → *Cost Analysis* in the console. Always Free resources show zero. Anything non-zero, find it under Billing → Subscriptions. |

### 2.9 One thing to know later

Oracle reclaims Always Free compute that sits **idle** for extended periods. A
ladder in active use during a season is fine, but if the club goes quiet over a
long break, take a backup (see "Backups" below) so a reclaimed instance costs
you nothing but an afternoon of setup.

---

## 3. A VPS (recommended once you have Student Pack credit)

Note: GitHub Student Pack requires **current** enrollment — a school email plus
dated proof. If you haven't started yet, that's why it's being rejected; reapply
once you're actually enrolled.

The GitHub Student Developer Pack includes DigitalOcean credit; on the smallest
droplet that's years of hosting. Any Ubuntu box works.

```bash
# on the server
git clone <your repo> /opt/tennis-ladder
cd /opt/tennis-ladder
python3 run.py --data-dir /var/lib/tennis-ladder --port 8000
```

Keep it running with systemd — `/etc/systemd/system/ladder.service`:

```ini
[Unit]
Description=Tennis Ladder
After=network.target

[Service]
ExecStart=/usr/bin/python3 /opt/tennis-ladder/run.py --data-dir /var/lib/tennis-ladder --port 8000
WorkingDirectory=/opt/tennis-ladder
User=www-data
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now ladder
```

Then put Caddy in front for automatic HTTPS — the entire `/etc/caddy/Caddyfile`:

```
ladder.example.edu {
    reverse_proxy localhost:8000
}
```

Caddy obtains and renews the certificate itself. Point your domain's A record at
the server first.

---

## 4. Fly.io

`Dockerfile` and `fly.toml` are ready to go.

```bash
fly launch --no-deploy        # pick a unique app name; keep the existing files
fly volumes create ladder_data --size 1
fly deploy
fly open
```

Notes:

- **The volume is not optional.** `fly.toml` mounts it at `/data`; without it,
  every deploy starts an empty ladder.
- `auto_stop_machines = "suspend"` means the machine sleeps while nobody is
  using it, which for a club ladder is nearly always. First request after a
  quiet spell takes a second or two.
- Fly requires a card on file. Usage for something this idle is small, but
  check their current pricing yourself — it has changed before.

Set config values without editing files on the volume:

```bash
fly ssh console -C "python3 -c \"
from ladder.config import CONFIG
CONFIG.admin_password='...'; CONFIG.base_url='https://yourapp.fly.dev'; CONFIG.save()\""
```

---

## 5. Cloudflare Tunnel (free, from a machine you own)

Good if you have a Raspberry Pi or desktop that stays on. Gives a public HTTPS
URL with no port forwarding and no open inbound ports.

```bash
cloudflared tunnel --url http://localhost:8000
```

For something permanent, create a named tunnel and point a DNS record at it.

---

## 6. WSGI hosts (PythonAnywhere and friends)

`ladder/wsgi.py` exposes a standard `application`, so anything that speaks WSGI
can serve it:

```bash
gunicorn 'ladder.wsgi:application'
```

On PythonAnywhere, point the WSGI configuration file at
`ladder.wsgi.application` and set `LADDER_DATA_DIR` to a directory inside your
home folder.

**Caveat:** the app assumes a single process, because sessions and the rating
cache are shared in memory. Run **one worker**. That's plenty — the ladder
replays in milliseconds and a club generates a handful of requests a minute.

---

## Backups

Everything is in `<data-dir>/ladder.db`. Copy that file and you have the club's
whole history.

```bash
sqlite3 /var/lib/tennis-ladder/ladder.db ".backup '/backup/ladder-$(date +%F).db'"
```

`.backup` is safe to run while the server is live; plain `cp` is not guaranteed
to be. Or export readable CSV, which survives even if SQLite doesn't:

```bash
python3 -m tools.ladderctl --db /var/lib/tennis-ladder/ladder.db export matches > matches.csv
```

---

## Email (optional)

Notifications are off until SMTP is configured. In `config.json`:

```json
{
  "smtp_host": "smtp.gmail.com",
  "smtp_port": 587,
  "smtp_user": "yourclub@gmail.com",
  "smtp_password": "an app password, not your real one",
  "smtp_starttls": true,
  "smtp_from": "yourclub@gmail.com",
  "base_url": "https://ladder.example.edu"
}
```

Each player then chooses which of the four notification types they want, on
their own settings page. Nothing is sent to anyone who hasn't opted in, and
every message carries a working unsubscribe link.

If SMTP breaks, the app keeps working: sending happens on a background thread
and failures are logged, never raised into a result submission.

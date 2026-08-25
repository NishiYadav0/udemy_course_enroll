[← Back to README](../README.md)

# 📦 Chapter 1 — Full Deployment Walkthrough (Oracle Cloud, start to finish)

Everything below assumes you're starting from nothing — no server, no
account, nothing installed. By the end you'll have the bot running 24/7 and
the Admin Panel reachable in a browser. Replace every `<your-ip>` below with
your own VM's real public IP as you go.

**Next up:** once you finish here, continue to
[Chapter 2 — Front-End Setup](FRONTEND_SETUP.md) to log in to Telegram, pick
channels, and add your Udemy account — no more terminal needed after this
chapter.

---

## 1.1 Create a free Oracle Cloud account

Go to [oracle.com/cloud/free](https://www.oracle.com/cloud/free/) and sign up.
The **Always Free** tier includes enough compute (1 OCPU / 1GB RAM, or an
Arm-based Ampere shape with more headroom) to run this project forever at
$0 — no card charge as long as you stay within the Always Free shapes.
Verification (email + a card for identity confirmation, not billing) usually
takes a few minutes.

## 1.2 Create your VM instance

In the OCI Console: **Compute → Instances → Create Instance**.

- **Image**: Ubuntu 22.04 (or 20.04 — both work, see §1.5 for the Python caveat)
- **Shape**: pick one flagged "Always Free eligible"
- **Add SSH keys**: generate a new key pair here and download the private key
  (`.pem` on Mac/Linux, or convert to `.ppk` with PuTTYgen on Windows) — this
  is how you'll log in, there's no password
- Create the instance, then note its **public IP address** on the instance
  detail page — you'll use this constantly from here on

## 1.3 Open the ports you'll need

**Networking → Virtual Cloud Networks →** your VCN **→ Security Lists →** the
list attached to your subnet **→ Add Ingress Rules**:

| Purpose | Source CIDR | Protocol | Port |
|---|---|---|---|
| SSH (usually already there by default) | `0.0.0.0/0` (or your own IP for tighter security) | TCP | 22 |
| Admin Panel HTTPS | `<your-home-ip>/32` | TCP | 443 |

Keep the Admin Panel rule scoped to your own IP specifically — see the
[Admin Panel](../README.md#admin-panel) section of the README for why that
matters and what to do if your home IP changes.

The VM's own firewall (`iptables`) defaults to allowing everything outbound
and SSH inbound, but if you ever find the Admin Panel unreachable despite the
rule above being correct, also check:

```bash
sudo iptables -L INPUT -v -n | grep 443
# if nothing is listed:
sudo iptables -I INPUT -p tcp -s <your-home-ip> --dport 443 -j ACCEPT
sudo netfilter-persistent save
```

## 1.4 Connect over SSH

**Windows** — use [PuTTY](https://www.putty.org/): Host = your VM's IP, Port
22, and under Connection → SSH → Auth → Credentials, point it at your `.ppk`
key. **macOS/Linux**:

```bash
ssh -i /path/to/your-key.pem ubuntu@<your-ip>
```

## 1.5 Install Python 3.10+

Ubuntu 22.04 ships with a new enough Python already — check with
`python3 --version`. If you're on 20.04 (which ships Python 3.8) and the
`deadsnakes` PPA doesn't work for your image, Miniconda is the reliable
fallback:

```bash
wget https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh
bash Miniconda3-latest-Linux-x86_64.sh -b -p $HOME/miniconda3
$HOME/miniconda3/bin/conda create -y -n scholarsync python=3.11
source $HOME/miniconda3/bin/activate scholarsync
```

## 1.6 Get the code onto the VM

```bash
git clone https://github.com/VinaySinghChaudhary1/scholarsync-udemy-bot.git ~/scholarsync
cd ~/scholarsync
```

(Or transfer via [WinSCP](https://winscp.net/) using the same key as PuTTY,
if you'd rather not use git on the server.)

## 1.7 Install the bot's dependencies

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
playwright install-deps        # Linux only — system libraries Chromium needs
sudo apt-get update && sudo apt-get install -y xvfb   # virtual display for headless Chrome
```

## 1.8 Run the bot as a systemd service

Skip the manual Telegram login here entirely — **[Chapter 2](FRONTEND_SETUP.md)
handles logging in to Telegram, picking channels, and adding your Udemy
account through the Admin Panel's setup wizard**, with no terminal needed for
any of it. Just get the service file in place now so it's ready to start
itself the moment the wizard finishes:

Create `/etc/systemd/system/scholarsync.service`:

```ini
[Unit]
Description=ScholarSync - Telegram Udemy Coupon Auto-Enroll Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/scholarsync
Environment="HOME=/home/ubuntu"
ExecStart=/usr/bin/xvfb-run -a /home/ubuntu/scholarsync/venv/bin/python /home/ubuntu/scholarsync/main.py
Restart=always
RestartSec=15
StandardOutput=append:/home/ubuntu/scholarsync/scholarsync.log
StandardError=append:/home/ubuntu/scholarsync/scholarsync.log

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable scholarsync
# Don't start it yet — there's no .env or Telegram session until Chapter 2
# writes them. Starting it now would just crash-loop on missing config.
```

## 1.9 Deploy the Admin Panel

```bash
cd ~/scholarsync
python3 -m venv admin_panel/venv
source admin_panel/venv/bin/activate
pip install -r admin_panel/requirements.txt
deactivate

chmod +x admin_panel/deploy/setup_panel.sh
./admin_panel/deploy/setup_panel.sh
```

The script installs Nginx, generates a self-signed HTTPS certificate,
installs the `scholarsync-panel` systemd service, and installs the sudoers
rule that lets the panel (and only the panel, and only for this one service)
run `systemctl status/restart/enable/start` on the bot — nothing broader.

One manual step it can't do for you — point Nginx at your own IP:

```bash
nano admin_panel/deploy/nginx_scholarsync_panel.conf
# replace YOUR_HOME_IP with your real IP (same one from §1.3), save, exit

sudo cp admin_panel/deploy/nginx_scholarsync_panel.conf /etc/nginx/sites-available/scholarsync-panel
sudo ln -sf /etc/nginx/sites-available/scholarsync-panel /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
```

Open `https://<your-ip>/` in a browser. You'll get a one-time self-signed
certificate warning (expected — click through). You should land on a login
page with a **"Create your admin account"** link, since nothing has been
configured yet. That's your cue to move on to
**[Chapter 2 — Front-End Setup](FRONTEND_SETUP.md)**.

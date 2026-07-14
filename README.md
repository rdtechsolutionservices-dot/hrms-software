# VPL Exhibition Enquiry Form — AWS EC2 Deployment Guide

Flask + SQLite app for capturing exhibition leads on multiple devices at once,
with search, CSV/Excel export, and a password-protected **Master Settings**
page to add/remove dropdown & checkbox options without touching code.

Tested locally with automated checks: valid save, invalid save (missing
required fields), search, pagination, CSV export, Excel export, Master
Settings add/remove/duplicate-block/unauthorized-access, health check,
404/500 handling, and 15 simulated devices writing 300 leads concurrently
with zero lost records.

---

## 1. What's on the form now

**Customer Details:** Customer Name*, Contact No.*, Date, Source
**Product Interested In:** Mono Carton, Corrugated Box, 3 Ply, Shipper Box, Rigid Box
**Product Specification:** Description, Dimensions, Material/GSM, Printing & Finishing, Estimated Quantity, Required By
**Requirement / Remarks**
**Follow-up:** Reference Carton, Action (Rate, Sample, KLD, Option, Visit after Exhibition, E-meet after Exhibition), Remark, **VPL Coordinator** (dropdown)

Removed per your request: Type of Enquiry (entire section), Printed Sheets,
Duplex Carton, Other (from Product Interested In), City, Company, Sales
Executive, Email.

---

## 2. Master Settings — editing dropdown/checkbox options

Go to `/settings` (link in the top nav on every page). You'll see a
full-screen branded login card asking for a username and password —
default is:

```
Username: admin
Password: 123123
```

**Only this one account exists — there's no feature to create additional
logins.** Everyone shares this single admin login.

**Change this before the exhibition** by setting environment variables
(see the systemd config in section 8 below) — these only seed the
database the **very first time** the app runs on a fresh `leads.db`.
After that, credentials live in the database and stay changed across
restarts.

**No session timeout:** once you log in, you stay logged in for 30 days
or until you tap Logout — there's no automatic sign-out mid-exhibition.

### Changing your own password

On the Master Settings page, under **My Account** → enter your current
password, a new password (twice to confirm) → **Change My Password**.

Forgot it? Under the same section, enter your current password and tap
**Reset to Default** to bring the login back to `admin` / `123123`.

### Adding extra Customer Detail fields (optional)

Name, Contact No., Date, and Source always stay on the entry form. If you
need to capture something extra — Designation, GST No., whatever — add it
under **Customer Detail Fields (Optional)** in Master Settings. It shows
up on the entry form immediately, and appears as its own column in both
CSV and Excel exports, right after the Source column. Remove it anytime;
leads already saved keep their data (it just won't show as an export
column once the field itself is removed).

From this page you can add or remove options for:
- **Product Interested In**
- **Action**
- **VPL Coordinator** — empty by default, add your team's names here

Every add/remove is saved directly to the database and shows up on the
entry form immediately — no restart needed, no code changes.

---

## 3. Files in this package

```
vpl_exhibition_app/
├── app.py                     # Flask application (routes, DB logic)
├── requirements.txt           # Python dependencies
├── static/style.css           # Shared styling
└── templates/
    ├── index.html             # Entry form (New Entry tab)
    ├── leads.html              # Saved Leads tab (search + export)
    ├── settings.html           # Master Settings (manage dropdowns)
    ├── settings_login.html     # Password gate for Master Settings
    └── error.html              # Friendly error page
```

The SQLite database file (`leads.db`) is created automatically on first
run, including default options for Product Interested In and Action.

---

## 4. Fix for "works on localhost but not on another laptop"

This was **not** a code bug — the app already listens on `0.0.0.0` (all
network interfaces), which is correct. The usual causes, in order of how
often they actually turn out to be it:

1. **The other device used `localhost` or `127.0.0.1`.** That address
   always means "this same device" — it will never reach another
   computer. Other devices must use the **host laptop's LAN IP**, e.g.
   `http://192.168.1.42:5000`. When you run `python app.py`, the app now
   prints this exact URL for you on startup — copy it from there.

2. **Windows Firewall (or macOS firewall) is blocking incoming
   connections on port 5000.** On Windows: Control Panel → Windows
   Defender Firewall → Allow an app through firewall → allow Python (or
   allow port 5000 for Private networks). On Mac: System Settings →
   Network → Firewall → allow incoming connections for Python.

3. **Devices are on different networks.** Some venue WiFi and most mobile
   hotspots enable "client isolation" / "AP isolation", which deliberately
   blocks devices from seeing each other even on the same WiFi name. If
   step 1 and 2 don't fix it, ask the venue for a WiFi network with client
   isolation disabled, or deploy to EC2 instead (section 5 onward) so
   every device just uses the internet instead of a local network.

**For a mega exhibition, deploying to EC2 (below) sidesteps all three
issues** — every device just needs internet, not a shared local network.

---

## 5. Upload to your EC2 instance

From your laptop:

```bash
scp -i your-key.pem -r vpl_exhibition_app ubuntu@<EC2-PUBLIC-IP>:/home/ubuntu/
```

(Use `ec2-user` instead of `ubuntu` if your AMI is Amazon Linux.)

---

## 6. Install Python and dependencies on EC2

```bash
ssh -i your-key.pem ubuntu@<EC2-PUBLIC-IP>
cd /home/ubuntu/vpl_exhibition_app

sudo apt update
sudo apt install -y python3-pip python3-venv

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

---

## 7. Quick test run

```bash
python3 app.py
```

Visit `http://<EC2-PUBLIC-IP>:5000`. Make sure your EC2 Security Group
allows inbound traffic on port 5000 (or port 80 if using Nginx below).
Stop with `Ctrl+C` once confirmed, then set up the permanent service.

---

## 8. Run permanently with Gunicorn + systemd

```bash
sudo nano /etc/systemd/system/vpl-exhibition.service
```

```ini
[Unit]
Description=VPL Exhibition Enquiry Form
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/vpl_exhibition_app
Environment="VPL_SECRET_KEY=change-this-to-something-random"
Environment="VPL_ADMIN_USERNAME=your-chosen-username"
Environment="VPL_ADMIN_PASSWORD=your-chosen-password"
ExecStart=/home/ubuntu/vpl_exhibition_app/venv/bin/gunicorn \
    --workers 3 --bind 0.0.0.0:5000 --timeout 30 app:app
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

**Set your own `VPL_ADMIN_USERNAME` and `VPL_ADMIN_PASSWORD` here** — this
is what protects Master Settings.

```bash
sudo systemctl daemon-reload
sudo systemctl enable vpl-exhibition
sudo systemctl start vpl-exhibition
sudo systemctl status vpl-exhibition
```

Useful during the exhibition:
```bash
sudo systemctl restart vpl-exhibition   # restart if needed
sudo journalctl -u vpl-exhibition -f    # live logs
```

---

## 9. (Recommended) Put Nginx in front of it

If Nginx is already running for VPL PayRoll on this EC2 instance, add a
new server block:

```nginx
server {
    listen 80;
    server_name exhibition.yourdomain.com;

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
```

---

## 10. Backing up data during the event

```bash
scp -i your-key.pem ubuntu@<EC2-PUBLIC-IP>:/home/ubuntu/vpl_exhibition_app/leads.db ./backup_$(date +%Y%m%d_%H%M).db
```

Or simpler — hit **Export Excel** from the Saved Leads page each evening.

---

## 11. After the exhibition

Pull all data anytime from `http://<your-URL>/export/xlsx`, or SSH in and
copy `leads.db` directly.

To stop the service permanently:
```bash
sudo systemctl stop vpl-exhibition
sudo systemctl disable vpl-exhibition
```


---

## 12. Mobile-first design

Every page (entry form, saved leads, master settings, login) is built
mobile-first, since most people will use this on phones during the
exhibition:

- Inputs and buttons use 48px+ tap targets, and 16px font size on inputs
  (prevents iPhone Safari from auto-zooming when you tap a field)
- The **Saved Leads** page shows a stacked card list on phones instead of
  a cramped horizontal-scrolling table — full data, easy to read with one
  thumb. On tablets/laptops (screens 640px and wider) it automatically
  switches to a proper table view.
- The Save/Clear buttons stay fixed at the bottom of the screen so
  they're always reachable without scrolling, even on a long form.
- Tabs and header text scale down gracefully on very small/older phones
  too (tested down to ~320px width).

Nothing to configure here — it adapts automatically based on the screen
size of whatever device opens the link.

---

## 13. Fixed: "Session expired" error on Reset / other actions

If you upgraded from an earlier version of this app, your browser may
still hold an old login cookie that doesn't match the current session
format. That caused a confusing "Session expired" error on actions like
Reset even though the Master Settings page itself loaded fine.

This is now fixed at the source: any incomplete or stale session is
detected immediately and cleanly redirects to the login page, instead of
letting you halfway into a broken state. If you still see this once after
upgrading, it's a one-time thing — just log in again and it won't
recur.

---

## 14. Login / Logout screens

`/settings/login` and the page you land on after tapping **Logout** are
both standalone, full-screen branded pages (no top navigation bar) —
separate visually from the public exhibition form, so it's always clear
whether you're in the public area or the protected admin area.

- **Logging in**: full-screen card, username + password, matches the
  overall app's color scheme.
- **Logging out**: shows a clear "You've Been Logged Out" confirmation
  with two buttons — **Log In Again** or **Go to Exhibition Form**. It
  does not silently drop you back on the form as if nothing happened.

Important: **only Master Settings is protected.** The New Entry form and
Saved Leads page remain fully public with no login required, on purpose
— exhibition visitors and staff need to use those without any login
step. Logging out only ends your Master Settings session; it has no
effect on the public form.

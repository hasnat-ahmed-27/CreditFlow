# CreditFlow — Demo Guide

## A) Run the CLOUD version (live, shareable link)

Nothing to start — it's **already running 24/7 on AWS**. Just open or share:

```
http://13.60.163.115:3000
```

Your mentor (or anyone) clicks that link and uses the full app — no setup on their end.

**If it ever looks down** (e.g., after the server rebooted):
1. AWS Console → EC2 → Instances → select `creditflow` → **Connect** → **EC2 Instance Connect** → Connect
2. In the terminal run:
   ```bash
   cd CreditFlow && sudo docker compose up -d
   ```

**Rules to keep the link alive:**
- Do **NOT** click "Stop" on the instance (the public IP would change and break the app). Rebooting is fine.
- It runs on your free AWS credits (~$1–2/day).

---

## B) Run the LOCAL version (on your laptop)

1. Open **Docker Desktop** (wait for the whale icon to go steady).
2. Open a terminal:
   ```powershell
   cd C:\PROJECTS\creditflow
   docker compose up -d
   ```
3. Wait ~30 seconds, then open:
   ```
   http://localhost:3000
   ```
4. To stop it:
   ```powershell
   docker compose down
   ```

> Local and cloud are configured separately (local uses `localhost`, cloud uses the AWS IP) — both work. Your LinkedIn app has **both** redirect URLs registered, so LinkedIn connect works in either.

---

## C) Post an image to LinkedIn from CreditFlow (the bonus feature)

The image is supplied as an **image URL** (paste a public image link — no file upload needed).

1. **Connect LinkedIn** (once): **Social** page → **Connect LinkedIn** → approve on LinkedIn.
2. **Create content with an image:**
   - **Content** → **New draft**
   - Write the post text in the body
   - In the **Image URL** field, paste a direct image link, e.g.:
     ```
     https://placehold.co/800x600/6366f1/ffffff.png?text=CreditFlow
     ```
     (or any direct image URL)
   - **Save**
3. **Approve it:** on that draft, change status **draft → approved**.
4. **Publish:**
   - Go to **Social** → in **Compose**, pick your approved content item
   - The **Preview** shows the LinkedIn-style post *with the image*
   - Click **Publish to LinkedIn**
5. **Check your real LinkedIn feed** — the text + image post is live. (Publish History in CreditFlow also shows it with an "Open" link.)

> Behind the scenes this runs LinkedIn's full image flow: register upload → PUT the image binary → get the asset URN → create the UGC post with text + image.

---

## Quick demo script (5 minutes)

1. Open the live link → **sign up** → land in the dashboard
2. **AI Studio** → type a prompt → watch it **stream live** + credits deduct
3. **Content** → save the generated post as a draft + add an image URL → **approve**
4. **Social** → **Publish to LinkedIn** → show the post on your real feed
5. **Calendar** → schedule a post (show a **recurring** one — the bonus)
6. **Billing** → Upgrade to Pro with Stripe test card `4242 4242 4242 4242` → balance updates
7. **Admin** → show the audit log + active sessions

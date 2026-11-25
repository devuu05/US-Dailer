# Twilio Click-to-Call Dialer

Call US businesses from your PC through the browser (WebRTC → Twilio → phone network).
FastAPI backend, a single-page UI, and SQLite for leads, outcomes and call history.

```
Browser (mic/headset) ──WebRTC──▶ Twilio ──▶ Business phone
        │ GET /token                 │ POST /voice, /call-status
        └──────────▶ FastAPI ◀───────┘   (through ngrok)
```

## 1. Twilio trial setup (one time)

1. **Sign up** at twilio.com and claim your free **trial number** (Console → Phone Numbers → Buy a number; trial accounts get one free).
2. **Verify your own cell phone**: Console → Phone Numbers → Manage → **Verified Caller IDs** → Add. ⚠️ **Trial accounts can only call verified numbers.** You can't call real businesses until you upgrade.
3. **Copy the Account SID and Auth Token** from the Console home page.
4. **Create an API key**: Account → API keys & tokens → Create API key (type *Standard*). Copy the **SID (SK…)** and the **Secret**. The secret is shown only once.
5. Leave the TwiML App for step 3 below, because it needs your ngrok URL.

## 2. Run the app

```bash
copy .env.example .env
```
Fill in what you have so far, then run:
```bash
run.bat
```
This creates a `.venv`, installs dependencies and starts the app on <http://localhost:8000>.

## 3. Expose the webhooks with ngrok

Twilio has to reach `/voice` over public HTTPS.

```bash
ngrok http 8000
```
Tip: free ngrok accounts get one **static domain** (dashboard → Domains). Use `ngrok http --url=your-name.ngrok-free.app 8000` so the URL doesn't change on every restart.

Then:
1. Console → Voice → **TwiML Apps** → Create.
   **Voice Request URL** = `https://<your-ngrok-domain>/voice`, method **POST**. Save, then copy the **App SID (AP…)**.
2. In `.env`, set `TWILIO_TWIML_APP_SID`, `TWILIO_CALLER_ID` (your trial number, e.g. `+15551234567`) and `PUBLIC_BASE_URL` (your ngrok `https://…` URL).
3. Restart `run.bat`. The header should say **config OK**.

## 4. First test call

1. Open <http://localhost:8000>. Use the localhost URL, **not** the ngrok one, because the UI and token are blocked over the tunnel on purpose.
2. Type your verified cell number and click **Call**. Allow microphone access.
3. Your cell rings. On a trial account, Twilio plays a short **"this is a trial account" message and asks the callee to press a key** before connecting. That's normal and goes away after you upgrade.

## Using it

- **Import CSV**: headers `name, phone, city, state, category` (also accepts `business`/`company` and `phone number`). Numbers in any US format are normalized; duplicates are skipped.
- Click a lead → **Call** → after the call, click an **Outcome** (No answer, Voicemail, Interested, …) and add notes. **Next lead ▸** moves down the list.
- The **keypad** sends DTMF tones during a call, for phone menus like "press 1 for sales".
- **Do not call** blocks the number in the UI *and* on the server (`/voice` refuses it).
- Data lives in `dialer.db` (SQLite).

## Going live (after testing)

1. **Upgrade** the Twilio account (add a card). This removes the trial message and the verified-numbers-only limit.
2. Buy a local number (an area code near your targets helps answer rates) and update `TWILIO_CALLER_ID`.
3. **Trust Hub**: register a Business Profile and apply for **SHAKEN/STIR** + **CNAM** (caller name). If you skip this, calls often show as "Spam Likely".
4. Stay compliant:
   - Keep calls **click-to-dial** (one human-initiated call at a time). Auto-dialers and prerecorded/AI voice messages bring much stricter TCPA rules.
   - Call between **8am and 9pm in the business's local time**.
   - Honor opt-outs right away (**Do not call**). Some "business" numbers are really cell phones.
   - Leave recording off unless you announce it, because some states require all-party consent.

## Deploy online for free (Render + Neon)

The online version keeps running when your PC is off, so anyone with the secret link can use it.

1. **Database (Neon, free):** sign up at neon.tech → create a project → copy the **connection string**. It looks like `postgresql://…neon.tech/neondb?sslmode=require`.
2. **GitHub:** create an **empty private repo** (no README), then push this folder:
   ```bash
   git remote add origin https://github.com/<you>/twilio-dialer.git
   git push -u origin main
   ```
3. **Render (free):** sign up with GitHub → **New + → Blueprint** → pick the repo. Render reads `render.yaml` and asks for the env vars. Copy them from your local `.env`, and set `DATABASE_URL` to the Neon string. You don't need `PUBLIC_BASE_URL` on Render because it's detected automatically.
4. **Twilio:** set the TwiML App's Voice Request URL to `https://<your-app>.onrender.com/voice`.
5. Open `https://<your-app>.onrender.com/?key=<ACCESS_KEY>` and send that link to your friend. After the first visit the key is remembered and removed from the address bar.

Free-tier notes: Render sleeps after about 15 minutes idle, so the first page load takes about 30–60 seconds. Calls work normally once the page is up. Data lives in Neon, so it survives sleeps and redeploys.

## Security notes

- **The secret link is the password.** Anyone who has it can place calls on your Twilio balance. To revoke access, change `ACCESS_KEY` in Render. Everyone then needs the new link.
- With `ACCESS_KEY` set, the local app needs the link too: `http://localhost:8000/?key=<ACCESS_KEY>`.
- `/voice` and `/call-status` verify Twilio's request signature. The public URL (`PUBLIC_BASE_URL`, or Render's automatic one) must match what Twilio calls exactly, or Twilio's requests get 403.
- Only valid US numbers (+1, NANP format) can be dialed, which limits toll-fraud exposure. Also check Console → Voice → Settings → **Geo permissions** and leave only the US enabled.

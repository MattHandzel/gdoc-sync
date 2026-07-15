# OAuth setup (one-time, ~5 minutes)

gdoc-sync talks to Google's Docs and Drive APIs as *you*. Google requires each
user (or team) to bring their own OAuth client — no app can ship a shared
secret in an open-source tool. You create a free "app" in your own Google
account once, then gdoc-sync uses it forever.

## Steps

1. **Create a Google Cloud project** (or reuse one):
   [console.cloud.google.com/projectcreate](https://console.cloud.google.com/projectcreate).
   Any name works, e.g. `gdoc-sync`.

2. **Enable the two APIs** — with your project selected:
   - [Enable the Google Docs API](https://console.cloud.google.com/apis/library/docs.googleapis.com)
   - [Enable the Google Drive API](https://console.cloud.google.com/apis/library/drive.googleapis.com)

3. **Configure the OAuth consent screen**:
   [console.cloud.google.com/apis/credentials/consent](https://console.cloud.google.com/apis/credentials/consent)
   - User type: **External**, then fill in only the required fields (app name,
     your email twice).
   - You do NOT need to submit for verification. Under **Test users**, add
     your own Google account email. (Apps in "Testing" mode work fine for
     personal use; test-user tokens may expire after 7 days unless you either
     publish the app — fine for personal single-user apps — or re-auth.)

4. **Create the OAuth client**:
   [console.cloud.google.com/apis/credentials](https://console.cloud.google.com/apis/credentials)
   → **Create credentials** → **OAuth client ID** → Application type:
   **Desktop app** → Create → **Download JSON**.

5. **Hand it to gdoc-sync**:

   ```bash
   gdoc-sync auth --client ~/Downloads/client_secret_*.json
   ```

   This copies the file to `~/.config/gdoc-sync/client_secret.json`, opens a
   browser for consent, and caches a token at `~/.config/gdoc-sync/token.json`.
   You won't be asked again; the token refreshes itself.

## Alternatives

- Point `GDOC_SYNC_CLIENT_SECRET` at the JSON instead of installing it.
- CI / headless use: not supported yet (planned: service-account support).

## Scopes requested

- `https://www.googleapis.com/auth/documents` — read/write the docs you sync
- `https://www.googleapis.com/auth/drive` — upload/update files, sharing, comments

Both credential files are secrets. gdoc-sync `chmod 600`s them; don't commit
them (the repo's `.gitignore` already excludes them).

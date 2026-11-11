# Mobile access: Omi app + pendant → local backend

How the Omi phone app (and through it, the pendant) reaches this server, and
how to build the app that can. Steps marked **[you]** need your devices or the
shared `webserver_nginx`, so the repo can't do them for you.

```
pendant ──BLE──▶ phone (our Omi "dev" build) ──HTTP/WS, LAN or VPN──▶ webserver_nginx :80
                                                     vhost 192.168.5.53 (deploy/nginx/)
                    /identitytoolkit.googleapis.com/, /securetoken.googleapis.com/ ─▶ firebase:8104 (Auth emulator)
                    everything else (/v1…, /v4/listen, /_local/storage) ──────────────▶ backend:8080
```

Only the phone needs network access. The pendant talks BLE to the phone only,
and the phone streams its audio to `/v4/listen`.

## What the app supports (upstream `app/`, pinned submodule)

- **A store build can't be used.** The `prod` flavor is pinned to
  `https://api.omi.me/` and Omi's production Firebase project
  (`Env.validateStartupRouting`). The in-app "custom backend URL" override is
  ignored in release builds (`BackendUrlOverride.restore`), and the app never
  sends credentials to it anyway (`_isRequiredAuthCheck`). Firebase ID tokens
  are project-scoped, so production tokens couldn't be verified by our
  `demo-omi-local` emulator either.
- **What we build instead:** the `dev` flavor (`com.friend.ios.dev`, "Omi Dev",
  which installs next to the store app) with the `local_dev` profile. This is
  upstream's supported self-host path (`app/setup.sh android`). Everything is
  set at compile time:
  - `OMI_API_BASE_URL`: REST base. The websocket URL comes from it
    (`http→ws`, plus `v4/listen`), and so does the sign-in endpoint.
  - `OMI_FIREBASE_AUTH_EMULATOR_HOST` / `_PORT` go to
    `FirebaseAuth.useAuthEmulator(host, port)`. The Firebase SDK always uses
    **plain HTTP** to the emulator.
  - Firebase project `demo-omi-local` comes from the fake configs upstream
    ships (`lib/firebase_options_local.dart`,
    `setup/prebuilt/google-services-local.json`). They hold no secrets. The
    app only uses Firebase **Auth**: no Firestore and no Storage from the
    phone.
- **Allowed API hosts:** `local_dev` refuses at startup any API URL that isn't
  loopback, an RFC 1918 or 100.64.0.0/10 (Tailscale) **IP literal**, or a
  `*.ts.net` name (`_isLocalDevelopmentApi`). A public hostname such as
  `https://omi.riuna.com/` makes the app abort, even if your LAN DNS resolves
  it to this host.
- **Sign-in:** a `local_dev` build shows only "Sign in (local dev)". There is
  no Google or Apple sign-in, because those OAuth clients are bound to Omi's
  official bundle IDs. The button:
  1. posts `uid=local-dev-user` to `{API}/v1/auth/local-dev/custom-token`
     (backend `routers/auth.py`; it exists only while
     `FIREBASE_AUTH_EMULATOR_HOST` is set);
  2. probes `http://{emulator host}:{port}/`;
  3. signs in with the custom token against the emulator.

  Every phone signs in as the same user, `local-dev-user`.
- **Cleartext:** the Android manifest sets `usesCleartextTraffic="true"`, so
  plain HTTP works on Android. On iOS, upstream's own physical-iPhone path uses
  LAN IPs over HTTP too.

## Exposure: decision and trade-offs

**Decision:** one plain-HTTP nginx vhost on the host's LAN IP
(`server_name 192.168.5.53`, port 80 of `webserver_nginx`). Nginx routes by
path to the Auth emulator and the backend, admits only `OMI_ALLOWED_CIDRS`
(default `192.168.5.0/24`), and blocks the emulator's admin API (`/emulator/`).

Why it's built this way:
- **No TLS.** The app rejects public hostnames (see above), so the existing
  `*.riuna.com` Let's Encrypt certificate can't be used, and nobody issues
  certificates for a LAN IP. The Auth emulator connection is plain HTTP no
  matter what.
- **One vhost for both upstreams, not two ports.** `webserver_nginx` publishes
  only 80/443, and the emulator only needs its Google API path prefixes. This
  also keeps the emulator's admin endpoints away from the phone network.
- **Nginx, not direct port publishing on the LAN.** It gives one entry point
  with an IP allowlist, path filtering and access logs
  (`logs/nginx/omi-access.log`).
- **How nginx reaches the containers:** through `host.docker.internal`
  (172.17.0.1), like the other vhosts on this host. That's why the Auth
  emulator (and the backend, see below) are also published on
  `172.17.0.1`, which is docker0 and not reachable from the LAN. We don't
  attach `webserver_nginx` to `omi_default`, because that network isn't ours
  to manage.

**Security, so read this:** with the emulator there is **no real
authentication.** Anyone who can reach the vhost can mint a token for any uid
(`/v1/auth/local-dev/custom-token`), and `/_local/storage` serves stored files
without auth. `ADMIN_KEY` is another bypass (`Bearer ADMIN_KEY<uid>`, so keep
it long and secret). Traffic, including audio, crosses the network
unencrypted. So:
- Only expose it on networks you trust. The default allowlist is the home LAN
  `/24`. Shared or guest Wi-Fi on the same subnet can sign in too.
- Don't route it through the Cloudflare tunnel or a public port. The app
  wouldn't accept that anyway, and it would publish your data.
- **Away from home** (recommended): use **Tailscale** (or WireGuard) on the
  host and the phone. WireGuard encrypts the link, and the app accepts
  `100.64.0.0/10` IPs and `*.ts.net` names. Add the tailnet IP as a second
  vhost (render it again with `OMI_PUBLIC_HOST=100.x.y.z`), add
  `OMI_ALLOWED_CIDRS="192.168.5.0/24 100.64.0.0/10"`, and build the app with
  the address it will use. A build has exactly one address baked in, so if the
  phone switches between home and away, build for the Tailscale address only.
  Tailscale isn't installed on this host yet.
- Basic auth isn't an option: the app can't send it (no credentials in the URL
  are allowed, and there's no header hook).
- Without connectivity, the pendant keeps recording to its own storage and the
  app syncs it to the backend later (offline sync uploads, hence the large
  `client_max_body_size`).

## Setup

### 1. Backend and Auth emulator published on docker0

- `firebase`: `172.17.0.1:8104:8104` is in `docker-compose.yml`. Apply it with
  `docker compose up -d firebase`. Recreating it is a clean stop, so the
  emulator data is exported first.
- `backend`: needs `172.17.0.1:8109:8080` next to its `127.0.0.1` port, and
  `OMI_LOCAL_STORAGE_BASE_URL` (phone-facing, from `.env`) wired into its
  environment. See the backend service in `docker-compose.yml`.
- `backend`: set `STT_PRERECORDED_MODEL=parakeet` too. Otherwise pre-recorded
  transcription, which includes the pendant's **offline sync** uploads, goes to
  the cloud provider Modulate and fails without a key. We saw
  `provider=modulate model=velma-2 ... ConnectError` in the backend log on
  2026-11-11.

### 2. Install the nginx vhost **[you]**

`webserver_nginx` is shared and not managed by this repo:

```bash
cd ~/src/omi
# OMI_PUBLIC_HOST / OMI_ALLOWED_CIDRS / OMI_LOCAL_STORAGE_BASE_URL in .env (see .env.example)
scripts/test_nginx_conf.sh            # nginx -t: vhost alone + merged into a temp copy of the webserver config
scripts/render_nginx_conf.sh -o ~/src/webserver/nginx/conf.d/omi.conf
docker exec webserver_nginx nginx -t && docker exec webserver_nginx nginx -s reload
curl -s -o /dev/null -w '%{http_code}\n' http://192.168.5.53/v1/users/profile   # from a LAN machine: 401 = backend reached
```

When the template changes, render and reload again. To remove it, delete
`conf.d/omi.conf` and reload.

### 3. Build the app

```bash
scripts/build_app.sh     # Android APK in Docker → results/app/<timestamp>/omi-dev-<sha>-release.apk
```

- Runs Flutter 3.44.5, JDK 21, Android SDK 36 and NDK 28.2 in the
  `omi-local-app-builder` image (`scripts/build_app/Dockerfile`, ~8 GB). The
  pub and Gradle caches live in `.cache/app-build/`. The first build takes a
  while, later ones are faster.
- It exports `upstream/omi/app` at the submodule's HEAD to
  `.cache/app-build/src/` and configures it there the way `setup.sh android`
  does (`.dev.env`, emulator Firebase configs, prebuilt debug keystore). The
  submodule itself is never changed.
- Settings: `OMI_PUBLIC_HOST` / `OMI_PUBLIC_PORT`, or the explicit
  `OMI_APP_API_BASE_URL`, `OMI_APP_AUTH_EMULATOR_HOST` / `_PORT`,
  `OMI_APP_BUILD_MODE`, `OMI_APP_TARGET_PLATFORMS` (default `android-arm64`).
  Rebuild after changing the address.
- The APK is signed with upstream's **public** debug keystore. That's fine for
  sideloading, but anyone could sign an "update" for it, so only install APKs
  you built yourself.

**iPhone [you, needs a Mac with Xcode 16.4]:** Docker can't build iOS. On the
Mac, in a checkout of the same upstream commit:

```bash
cd app
export OMI_DEV_HOST=192.168.5.53 OMI_LOCAL_API_BASE_URL=http://192.168.5.53/
source setup.sh            # defines the functions; then follow app/README.md "Building and Deploying to iPhone"
setup_app_env local_dev "$OMI_LOCAL_API_BASE_URL" && setup_firebase \
  && bash scripts/generate_ios_dev_info_plist.sh && generate_ios_custom_config Dev omi-dev
flutter pub get && dart run build_runner build --delete-conflicting-outputs
flutter build ios --flavor dev --release \
  --dart-define=OMI_APP_PROFILE=local_dev \
  --dart-define=OMI_API_BASE_URL=http://192.168.5.53/ \
  --dart-define=OMI_FIREBASE_AUTH_EMULATOR_HOST=192.168.5.53 \
  --dart-define=OMI_FIREBASE_AUTH_EMULATOR_PORT=80
```

You also need an Apple developer team for signing. (`setup.sh ios` itself
hardcodes port 8000 for the API and 9099 for the emulator, which doesn't match
our vhost.) This path hasn't been tested here.

### 4. Install, sign in, pair **[you]**

1. Copy the APK to the Android phone and open it (allow "install unknown
   apps"), or use `adb install -r omi-dev-*.apk`.
2. Connect the phone to the home Wi-Fi (or Tailscale), open "Omi Dev" and tap
   **Sign in (local dev)**.
3. Go through onboarding and pair the pendant over Bluetooth as usual.
   Recording streams to `/v4/listen` on our backend.

### Verify

- `scripts/test_nginx_conf.sh --live` starts a throwaway nginx on
  `omi_default` and walks the app's flow through it: custom token → emulator
  sign-in and token refresh → `GET /v1/users/profile` → `/v4/listen` websocket
  upgrade, plus the `/emulator/` block and the allowlist. It needs `backend`
  and `firebase` running, and doesn't touch `webserver_nginx`.
- After installing the vhost, watch `~/src/webserver/logs/nginx/omi-access.log`
  while using the phone.

## Known limits

- **Push notifications don't work.** FCM needs a real Firebase project.
  Anything that relies on pushes (e.g. "conversation ready") won't arrive, so
  open the app to see it.
- Single user: every build signs in as `local-dev-user`.
- No Google or Apple sign-in, and no OAuth integrations
  (`BASE_API_URL`-based callbacks).
- The app embeds one address. If `OMI_PUBLIC_HOST` changes (DHCP), rebuild, or
  reserve the IP on the router.

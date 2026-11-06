# Installing Worbus

Worbus is two apps (Navic for Android, Feishin for the desktop) and one server, the **Worbus Server**,
which runs beside your Navidrome. This page is all you need: nothing here asks for the source code.

Found a bug, or have an idea? [Open an issue](https://github.com/illuminator69/navi-connect/issues/new/choose).
One tracker for every piece.

## 1. What you need

- **Docker** with Compose (`docker compose version` answers).
- **A Navidrome** server with your music, and **an admin account** on it. The Worbus Server signs in with it to
  read the library and the other users.
- **The path to your music folder** on the machine that runs Docker, the one Navidrome reads. The server needs
  it **writable**: lb-bot, the half that knows which albums you are missing, fixes covers and places albums
  there.

## 2. The server

1. Make a folder for it, with a `data` folder inside, **as the user the server will run as**:

   ```sh
   mkdir -p worbus-server/data && cd worbus-server
   curl -fLO https://raw.githubusercontent.com/illuminator69/navi-connect/main/server/compose.yaml
   curl -fL -o .env https://raw.githubusercontent.com/illuminator69/navi-connect/main/server/.env.example
   ```

   Make `data` yourself: a folder Docker creates belongs to root, and the server cannot write to it.

2. In `.env`, set the three required values: `NAVIDROME_URL` (as the server reaches it, for example
   `http://192.168.1.10:4533`), `NAVIDROME_USER` (the admin account) and `NAVIDROME_PASSWORD`. Everything else
   is optional and explained in the file. Keep `.env` to yourself: it holds the password.

3. In `compose.yaml`, change `/path/to/your/music` to your music folder. `user: "1000:1000"` must be a user that
   can write to `data` and the music folder (`id -u` and `id -g` print yours; Unraid uses `"99:100"`).

4. Start it:

   ```sh
   docker compose up -d
   docker compose ps
   ```

   **Healthy** looks like `Up 1 minute (healthy)`; give it up to two minutes on the first start.
   `docker logs worbus-server` starts with `[worbus] Worbus Server 0.4.0 (abc1234)`, and every line after it
   says which half wrote it, `[hub]` or `[lb-bot]`. If a required setting is missing or wrong, the server stops
   and its last line names the setting.

The server listens on port **4790** for the apps, and on **8899** for lb-bot's own page (settings, placement and
review; it asks for your Navidrome login). Remove the 8899 line in `compose.yaml` to keep that page off your
network.

## 3. The apps

- **Navic (Android):** the APK is on [navic-gaps' Releases](https://github.com/illuminator69/navic-gaps/releases).
  If the original Navic (from Obtainium, F-Droid or its own releases) is installed, **uninstall it once** first:
  this build has the same package name and a different signature, so Android refuses to install it over the
  original. Later Worbus builds install over this one.
- **Feishin (desktop):** on [feishin-gaps' Releases](https://github.com/illuminator69/feishin-gaps/releases):
  Linux (AppImage, deb, tar.xz), Windows (a portable zip) and macOS (dmg). The builds are not signed:
  - **Windows** shows "Windows protected your PC" (SmartScreen) on the first start: **More info**, then
    **Run anyway**.
  - **macOS** says the app cannot be checked: right-click it, **Open**, then **Open** again.

Each app's About shows its version, for example `0.4.0 (abc1234)`.

## 4. First run

1. Open the app and sign in to **your Navidrome** with your own account (any Navidrome user can).
2. The app looks for the Worbus Server beside your Navidrome and shows it. Tap it. (Not found? Use
   **Look on this network** at home, or type its address, `http://<the server's address>:4790`.)
3. On Home, a bar shows lb-bot reading your library for the first time. Leave it running until it reaches 0;
   on a large library that takes a while. Then an artist's page shows the albums you don't have.

Each person in the household signs in with their own Navidrome account and gets their own queues and devices.

## 5. Away from home

At home the apps use the server's home address. To use them from outside, put the server's port 4790 behind
your reverse proxy or tunnel **with TLS**, and set its address in `.env`:

```sh
HUB_PUBLIC_URL=wss://worbus.example.org
```

Only `wss://` works (an `https://` value is read as `wss://`): the apps' sign-in travels over it. Then
`docker compose up -d` again. A signed-in app learns the address by itself and switches to it when the home
address does not answer. For lb-bot's page away from home, also set `LBBOT_WEB_URL` to its address behind your
proxy; without it, the apps' **Open in lb-bot** works only at home.

## 6. Sending logs

Attach these to your issue. None of them holds a password, token or other secret: each is cleaned before it
leaves.

- **The server:**

  ```sh
  docker logs worbus-server --since 24h > worbus-server.log
  ```

- **Navic:** About › **copy logs for report** opens the Logs screen; tap **Share logs** and send the file. After
  a crash, the crash screen has **Share** (and **Copy**) for its report.
- **Feishin:** Settings › Advanced › Logger › **Export diagnostics** saves a zip.

The bug form asks for three versions: each app's (in its About) and the server's (on the app's Server screen).

## 7. Removing everything

```sh
cd worbus-server
docker compose down
docker image rm ghcr.io/illuminator69/worbus-server
```

Then delete the `worbus-server` folder (its `data` holds the server's state: devices, queues, lb-bot's index and
review) and uninstall the apps. Your music folder keeps what lb-bot placed or fixed in it; duplicates you deleted
in lb-bot sit in its `.lb-bot-trash` folder there. Navidrome is not changed.

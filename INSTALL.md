# Installing Worbus

Worbus is two apps (**Worbus for Android** and **Worbus for desktop**) and one server, the **Worbus Server**,
which runs beside your Navidrome. This page is all you need: nothing here asks for the source code.

The [latest release](https://github.com/illuminator69/worbus/releases/latest) lists all three pieces with their
checksums and links to the files; the files themselves live on each piece's Releases page, linked below.

Found a bug, or have an idea? [Open an issue](https://github.com/illuminator69/worbus/issues/new/choose).
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
   curl -fLO https://raw.githubusercontent.com/illuminator69/worbus/main/server/compose.yaml
   curl -fL -o .env https://raw.githubusercontent.com/illuminator69/worbus/main/server/.env.example
   chmod 600 .env
   ```

   Make `data` yourself: a folder Docker creates belongs to root, and the server cannot write to it.

2. In `.env`, set the three required values: `NAVIDROME_URL` (as the server reaches it, for example
   `http://192.168.1.10:4533`), `NAVIDROME_USER` (the admin account) and `NAVIDROME_PASSWORD`. Everything else
   is optional and explained in the file. Keep `.env` to yourself: it holds the password (the `chmod` above makes
   it readable by you alone). If Navidrome runs on the same machine, do not write `localhost` or `127.0.0.1`: inside
   the container that is the container itself. Use the machine's LAN address, or Navidrome's container name when
   both share a Docker network.

3. In `compose.yaml`, change `/path/to/your/music` to your music folder. `user: "1000:1000"` must be a user that
   can write to `data` and the music folder (`id -u` and `id -g` print yours; Unraid uses `"99:100"`).

4. Start it:

   ```sh
   docker compose up -d
   docker compose ps
   ```

   **Healthy** looks like `Up 1 minute (healthy)`; give it up to two minutes on the first start.
   `docker logs worbus-server` starts with `[worbus] Worbus Server 0.7.0 (abc1234)`, and every line after it
   says which half wrote it, `[hub]` or `[lb-bot]`. If a required setting is missing, blank or still the example
   value, or the `data` folder is not writable by that `user:`, the server stops and its last line names the
   setting or the folder. A value that is set but wrong (a mistyped address, a wrong password) is not caught at
   the start: the `[hub]` and `[lb-bot]` lines say what failed.

The server listens on port **4790** for the apps, and on **8899** for lb-bot's own page (settings, placement and
review; it asks for your Navidrome login). Remove the 8899 line in `compose.yaml` to keep that page off your
network.

## 3. The apps

- **Worbus for Android:** the APK is on [worbus-android's Releases](https://github.com/illuminator69/worbus-android/releases).
  If the original Navic (from Obtainium, F-Droid or its own releases) is installed, you can leave it: Worbus
  has its own app ID and installs beside it. Later Worbus builds install over this one.
  To be told about updates, add `https://github.com/illuminator69/worbus-android` to Obtainium with the APK
  filter `Worbus-v.*\.apk`.
- **Worbus for desktop:** on [worbus-desktop's Releases](https://github.com/illuminator69/worbus-desktop/releases):
  Linux (AppImage, deb, tar.xz), Windows (a portable zip) and macOS (dmg). The builds are not signed:
  - **Windows** shows "Windows protected your PC" (SmartScreen) on the first start: **More info**, then
    **Run anyway**.
  - **macOS** says the app cannot be checked: right-click it, **Open**, then **Open** again.

If you used Feishin on this computer, Worbus for desktop copies its settings the first time it starts (close
Feishin first; nothing is deleted). The Worbus Server sign-in is not copied: sign in to it once. On Linux, saved
server passwords may not come across either; if the app asks for a password once after the move, enter it again.

The version is in **Settings › About** in Worbus for Android and on the **Version** line of the app menu in Worbus
for desktop, for example `0.7.0 (abc1234)`.

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

Attach these to your issue. Each is cleaned before it leaves, line by line: passwords, tokens, keys and
Navidrome logins in a line are replaced with `[removed]`. Still read the file once before you send it.

- **The server:**

  ```sh
  docker logs worbus-server --since 24h > worbus-server.log
  ```

- **Worbus for Android:** Settings › About › **Copy logs for a bug report** opens the Logs screen; tap **Share logs** and send the file. After
  a crash, the crash screen has **Share** (and **Copy**) for its report.
- **Worbus for desktop:** Settings › Advanced › Logger › **Export diagnostics** saves a zip.

The bug form asks for three versions: each app's (see above) and the server's (on **Settings › Server** in the app).

## 7. Removing everything

```sh
cd worbus-server
docker compose down
docker image rm ghcr.io/illuminator69/worbus-server
```

Then delete the `worbus-server` folder (its `data` holds the server's state: devices, queues, lb-bot's index and
review) and uninstall the apps. Your music folder keeps what lb-bot placed or fixed in it; duplicates you deleted
in lb-bot sit in its `.lb-bot-trash` folder there. Navidrome is not changed.

Worbus Server is free software under the GNU General Public License, version 3 or later (`GPL-3.0-or-later`); see
[LICENSE](LICENSE).

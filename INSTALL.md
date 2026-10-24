# Install Clop Batch

Download the latest DMG from the
[releases page](https://github.com/Monit0123/Clop-2/releases/latest).

The release also carries `SHA256SUMS.txt`. To check the download:

```bash
cd ~/Downloads
shasum -a 256 -c <(grep Clop-Batch SHA256SUMS.txt)
```

## Install

1. Double-click `Clop-Batch.dmg` to mount it.
2. Drag **Clop Batch** onto the **Applications** folder in the same window.
3. Eject the disk image.

The app is signed ad-hoc, not notarised, because there is no Apple Developer
certificate behind the build. The first launch has to be approved:

- Right-click the app, choose **Open**, then **Open** again. Later launches work
  normally.
- Or from Terminal:

  ```bash
  xattr -dr com.apple.quarantine "/Applications/Clop Batch.app"
  ```

macOS may ask for permission to control Clop (used to quit it between batches)
and for access to the folders you choose. Both are expected.

## Before the first run

Clop needs its command line tool installed: menu bar icon, then **Install
command line integration**. Without it the app reports that Clop was not found
and will not start a job.

## Using it

- The app opens `http://127.0.0.1:8765` in your browser. The server runs only
  while the app is open and only on this machine.
- Choose a folder and an output folder, then press **Start**. Optimised copies
  go to the output folder with their subfolders intact. Originals are not
  touched unless you enable *delete originals*.
- Clop is quit and reopened every 5 files, which resets its free-tier counter.

## Uninstall

Quit the app and move `/Applications/Clop Batch.app` to the Bin. Settings and
the processed-file history are in `~/Library/Application Support/clop-gui/`;
delete that folder for a clean removal.

## Rebuilding

The DMG is built by GitHub Actions on a macOS runner. Push a `v*` tag, or run
the workflow manually. On a Mac locally:

```bash
./build_app.sh --dmg
```

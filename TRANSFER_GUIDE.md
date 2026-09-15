# Audio Transfer Guide

This is about getting offline audio from the Windows computer onto the Pi.

## Where Audio Goes

All local audio lives here on the Pi:

```text
/home/pi/audio/
```

The two main folders are:

```text
/home/pi/audio/tracks/
/home/pi/audio/shows/
```

Keep the folder names exactly the same as the paths in `stations.yaml`.

## Example

If `stations.yaml` says:

```yaml
name: "Bob Dylan"
type: dir
path: "shows/BobDylan"
```

the files go here:

```text
/home/pi/audio/shows/BobDylan/
```

If it says:

```yaml
name: "Muji BGM"
type: file
path: "tracks/Muji BGM 1980-2000.mp3"
```

the file goes here:

```text
/home/pi/audio/tracks/Muji BGM 1980-2000.mp3
```

That's basically the whole system.

## Make Sure the Pi Folders Exist

SSH into the Pi:

```bash
ssh pi@radiofall2026
```

Then:

```bash
mkdir -p /home/pi/audio/tracks
mkdir -p /home/pi/audio/shows
```

## Copy Files With PowerShell

To copy one file:

```powershell
scp "C:\path\to\song.mp3" pi@radiofall2026:/home/pi/audio/tracks/
```

To copy one show folder:

```powershell
scp -r "C:\path\to\BobDylan" pi@radiofall2026:/home/pi/audio/shows/
```

For a big transfer, add SSH keepalives:

```powershell
scp -4 -o ServerAliveInterval=30 -o ServerAliveCountMax=6 -r `
  "C:\path\to\audio\*" `
  pi@radiofall2026:/home/pi/audio/
```

If the hostname doesn't work, use the Pi's IP address instead.

## Better Option for Big Libraries: rsync

If you're using WSL, `rsync` is nicer for large transfers because you can run it again and it only copies what is missing or changed.

Example:

```bash
rsync -avh --progress --partial \
  /mnt/c/path/to/audio/ \
  pi@radiofall2026:/home/pi/audio/
```


## After Copying Audio

Tell MPD to rescan:

```bash
mpc update
```

You can check progress with:

```bash
mpc status
```

## Check What's There

Tracks:

```bash
find /home/pi/audio/tracks -maxdepth 1 -type f
```

Shows:

```bash
find /home/pi/audio/shows -maxdepth 2 -type f
```

## Supported Audio Types

The radio accepts:

```text
.mp3
.flac
.ogg
.m4a
.wav
.aac
```

## Common Problems

### Station does nothing

Usually the path in `stations.yaml` doesn't exactly match the real file or directory.

Check:

```bash
ls -lah /home/pi/audio/
```

and compare it to the path in `stations.yaml`.

### Filename has spaces

Just put quotes around it when using shell commands.

Example:

```bash
ls -lah "/home/pi/audio/tracks/Muji BGM 1980-2000.mp3"
```

### Transfer stopped halfway through

Just run the copy again.

For a large library, use `rsync`.


### Audio was copied but MPD doesn't see it

Run:

```bash
mpc update
```

Then give it a moment.

## One Rule That Saves a Lot of Trouble

If `stations.yaml` says:

```text
shows/BobDylan
```

then the Pi needs:

```text
/home/pi/audio/shows/BobDylan
```

Same relative path.


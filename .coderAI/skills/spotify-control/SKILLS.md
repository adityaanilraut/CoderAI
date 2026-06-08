---
name: spotify-control
description: Control Spotify desktop app on macOS via AppleScript
---

# Spotify Control on macOS

## Overview

This skill controls the Spotify desktop app on macOS using AppleScript through `osascript`.

Use this skill when the user asks to play, pause, resume, skip, go back, stop, open Spotify, change volume, or play a specific Spotify track, album, playlist URI, or URL.

This skill is intended for macOS only.

## Requirements

- macOS
- Spotify desktop app installed
- AppleScript support available through `osascript`
- User may need to grant Automation permission when macOS prompts for Terminal, shell, or the calling app to control Spotify

## Safety and Permissions

Do not use GUI scripting unless absolutely necessary.

Prefer Spotify's AppleScript dictionary commands over simulated keyboard clicks.

If macOS blocks automation, tell the user to enable permissions in:

`System Settings > Privacy & Security > Automation`

or, if needed:

`System Settings > Privacy & Security > Accessibility`

## Core Commands

### Launch Spotify

```bash
open -a Spotify
```

### Play or resume Spotify

```bash
osascript -e 'tell application "Spotify" to play'
```

### Pause Spotify

```bash
osascript -e 'tell application "Spotify" to pause'
```

### Toggle play/pause

```bash
osascript -e 'tell application "Spotify" to playpause'
```

### Next track

```bash
osascript -e 'tell application "Spotify" to next track'
```

### Previous track

```bash
osascript -e 'tell application "Spotify" to previous track'
```

### Stop playback

```bash
osascript -e 'tell application "Spotify" to stop'
```

### Set volume

Spotify volume is from `0` to `100`.

```bash
osascript -e 'tell application "Spotify" to set sound volume to 70'
```

### Get current playback status

```bash
osascript -e 'tell application "Spotify"
  if player state is playing then
    set stateText to "playing"
  else if player state is paused then
    set stateText to "paused"
  else
    set stateText to "stopped"
  end if

  if player state is stopped then
    return stateText
  else
    set trackName to name of current track
    set artistName to artist of current track
    return stateText & ": " & trackName & " by " & artistName
  end if
end tell'
```

### Play a Spotify URI

Use this when the user provides a Spotify URI such as:

`spotify:track:...`

```bash
osascript -e 'tell application "Spotify" to play track "spotify:track:TRACK_ID_HERE"'
```

Example:

```bash
osascript -e 'tell application "Spotify" to play track "spotify:track:4cOdK2wGLETKBW3PvgPWqT"'
```

### Open a Spotify URL

Use this when the user provides a Spotify web URL.

```bash
open "https://open.spotify.com/track/TRACK_ID_HERE"
```

For a Spotify URI, this also works:

```bash
open "spotify:track:TRACK_ID_HERE"
```

## Playing Music by Search Query

Spotify AppleScript does not reliably support "search and immediately play the first result" without UI scripting.

When the user asks to play a song by name, prefer one of these approaches:

1. If a Spotify URI or URL is available, play it directly.
2. Otherwise, open Spotify search for the query.
3. Tell the user that Spotify may require them to choose the result manually.

### Open Spotify search

Spaces and special characters must be URL-encoded.

```bash
open "spotify:search:QUERY_HERE"
```

Example:

```bash
open "spotify:search:Daft%20Punk%20One%20More%20Time"
```

## Recommended Helper Script

For more reliable usage, create a temporary AppleScript file and run it with `osascript`.

Example file:

```applescript
tell application "Spotify"
  activate
  play
end tell
```

Run it:

```bash
osascript /tmp/spotify-control.applescript
```

## Task Patterns

### User says: "Play Spotify"

Run:

```bash
open -a Spotify
osascript -e 'tell application "Spotify" to play'
```

### User says: "Pause Spotify"

Run:

```bash
osascript -e 'tell application "Spotify" to pause'
```

### User says: "Skip this song"

Run:

```bash
osascript -e 'tell application "Spotify" to next track'
```

### User says: "Go back"

Run:

```bash
osascript -e 'tell application "Spotify" to previous track'
```

### User says: "What's playing?"

Run:

```bash
osascript -e 'tell application "Spotify"
  if player state is stopped then
    return "Spotify is stopped"
  else
    return (player state as text) & ": " & name of current track & " by " & artist of current track
  end if
end tell'
```

### User says: "Set Spotify volume to 50"

Run:

```bash
osascript -e 'tell application "Spotify" to set sound volume to 50'
```

### User says: "Play this Spotify track"

If given a URI:

```bash
osascript -e 'tell application "Spotify" to play track "spotify:track:TRACK_ID_HERE"'
```

If given a URL:

```bash
open "https://open.spotify.com/track/TRACK_ID_HERE"
```

## Error Handling

If Spotify is not installed, tell the user:

"Spotify does not appear to be installed. Install the Spotify desktop app for macOS first."

If automation is blocked, tell the user:

"macOS blocked automation access. Open System Settings > Privacy & Security > Automation and allow the calling app to control Spotify."

If playback does not start, possible causes include:

- Spotify is not logged in
- No active Spotify session
- The provided URI is invalid
- The item is unavailable in the user's region
- Spotify requires user interaction for search results

## Notes

- Use `play track` only for Spotify URIs.
- Use `open` for Spotify URLs, playlists, albums, artists, or search.
- Avoid UI scripting unless the user explicitly asks for full automation of search result selection.
- Do not assume Apple Music commands work with Spotify.
